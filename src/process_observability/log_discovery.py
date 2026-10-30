"""Deterministic logging inventory over redacted frozen text; never imports target code.

Sites are navigation hints. Literal message text is only a candidate event key;
call-site fields are not proof of fields in a delivered record. Wrapper candidates
are deliberately not promoted into direct emission evidence.
"""

import ast
from collections import Counter
import hashlib
import re

_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_FIELD = re.compile(r"[A-Za-z_][\w.-]{0,63}")
_LEVELS = {"trace", "debug", "info", "warn", "warning", "error", "exception", "critical", "fatal", "log",
           "loginformation", "logdebug", "logwarning", "logerror", "logcritical", "verbose", "information"}
_FACTORIES = {"getLogger", "get_logger", "createLogger", "pino", "logger"}
_CALL = re.compile(r"(?<![\w.])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(")
_STRING = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`''', re.S)
_HINT = re.compile(r"\b(?:appender|handler|exporter|logstash|fluent.?bit|otel|FileHandler|ConsoleHandler)\b", re.I)
_DELIVERY_HINT = re.compile(r"\b(?:KafkaProducer|bootstrap\.servers|OTLP\w*Exporter|Logstash\w*Appender)\b",re.I)
_ODD_LINES = re.compile(r"[\v\f\x1c-\x1e\x85\u2028\u2029]")


def _callee(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _callee(node.value)
        return parent + "." + node.attr if parent else ""
    return ""


def _kind(name, receivers, custom):
    if name in custom:
        return "configured_call"
    if name in {"emit", "publish"}:
        return "generic_emit"
    receiver, _, method = name.rpartition(".")
    leaf = receiver.split(".")[-1].lower()
    if method.lower() in _LEVELS and (leaf in {"log", "logger", "logging", "_log", "_logger"} or receiver in receivers):
        return "log_call"
    return None


def _literal(node, constants):
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return node.value, "literal"
    if isinstance(node, ast.Name):
        return (constants[node.id], "resolved_constant") if node.id in constants else (None, "symbolic")
    return None, "dynamic" if node is not None else "unparsed"


def _record(path, first, last, callee, kind, key, status, fields, symbol, extractor, partial=False, column=0):
    key = key if isinstance(key, str) and key.strip() and len(key) <= 200 else None
    if key is None and status in {"literal", "resolved_constant"}:
        status = "unparsed"
    field_names = sorted({name for name in fields if isinstance(name, str) and _FIELD.fullmatch(name)})
    signature = f"{path}:{first}:{last}:{column}:{callee}:{key}"
    return {"id":"log-" + hashlib.sha256(signature.encode()).hexdigest()[:16], "path":path,
            "start_line":first, "end_line":last, "line":first, "symbol":symbol, "callee":callee,
            "kind":kind, "level":callee.rsplit(".", 1)[-1] if kind == "log_call" else None,
            "event_key":key, "key_status":status, "key_basis":"Candidate from call text; event semantics unverified",
            "fields":field_names[:32], "fields_status":"partial" if partial or len(field_names) > 32 else "syntactic",
            "extractor":extractor, "column":column}


def _python(path, text, custom, max_lines):
    if _ODD_LINES.search(text):
        raise SyntaxError("Source uses nonphysical splitlines separators")
    tree = ast.parse(text)
    receivers, assignments, values = set(), Counter(), {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = _callee(target)
                assignments[name] += 1
                if isinstance(node.value, ast.Call) and _callee(node.value.func).split(".")[-1] in _FACTORIES:
                    receivers.add(name)
        elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
            assignments[_callee(node.target)] += 1
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant) and type(node.value.value) is str:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and assignments[target.id] == 1:
                    values[target.id] = node.value.value
    # Enclosing declarations are labels only; AST parsing does not establish execution.
    parents = {}
    def visit(node, names):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names = [*names, node.name]
        if isinstance(node, ast.Call):
            parents[id(node)] = ".".join(names) or "<module>"
        for child in ast.iter_child_nodes(node):
            visit(child, names)
    visit(tree, [])
    wrappers = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parameters = {arg.arg for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]}
        direct = [call for call in ast.walk(node) if isinstance(call, ast.Call)
                  and _kind(_callee(call.func), receivers, custom) in {"log_call", "generic_emit"}
                  and call.args and isinstance(call.args[0], ast.Name) and call.args[0].id in parameters
                  and parents.get(id(call)) == node.name]
        if direct:
            wrappers.append({"name":node.name, "path":path, "start_line":node.lineno,
                             "end_line":min(node.end_lineno, node.lineno + 79),
                             "basis":"Same-file function contains a candidate call forwarding a parameter; execution unverified"})
    wrapper_names = {item["name"] for item in wrappers}
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee(node.func)
        kind = _kind(name, receivers, custom) or ("wrapper_call" if name in wrapper_names else None)
        if not kind:
            continue
        # Prefer an explicitly named key over message text; both remain semantic candidates.
        keyed = next((kw.value for kw in node.keywords if kw.arg in {"event_key", "event", "event_name"}), None)
        key, status = _literal(keyed if keyed is not None else (node.args[0] if node.args else None), values)
        fields, partial = [], False
        for kw in node.keywords:
            if kw.arg is None:
                partial = True
            else:
                fields.append(kw.arg)
            if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
                fields += [key.value for key in kw.value.keys if isinstance(key, ast.Constant) and type(key.value) is str]
                partial = partial or any(key is None for key in kw.value.keys)
        end = node.end_lineno
        if end - node.lineno >= max_lines:
            end, key, status, partial = node.lineno + max_lines - 1, None, "unparsed", True
        sites.append(_record(path, node.lineno, end, name, kind, key, status, fields,
                             parents[id(node)], "python_ast", partial, node.col_offset))
    return sites, wrappers


# Opt-in (exclude_declarations=True) Java declaration-header filter; the default discovery never consults it.
DECLARATION_FILTER = "c_like_declarations/1"
_DECLARATION_SUFFIXES = (".java",)  # Kotlin/Scala infix calls and C# query keywords make this rule unsafe there
_CALL_WORDS = frozenset({"return", "throw", "new", "else", "case", "yield", "assert", "do", "instanceof", "this",
                         "super"})
_TYPE_END = re.compile(r"([A-Za-z_$][\w$]*|>|\])\s*$")
_TYPED_PARAMETER = re.compile(r"(?:@[\w.]+(?:\([^()]*\))?\s*)*(?:final\s+)?(?:@[\w.]+(?:\([^()]*\))?\s*)*"
                              r"[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*(?:\s*<.*>)?(?:\s*\[\s*\])*"
                              r"(?:\s*\.\.\.)?\s+[A-Za-z_$][\w$]*(?:\s*\[\s*\])*")
_ANNOTATION_LINE = re.compile(r"\s*@[A-Za-z_$][\w$.]*(?:\s*\(.*\))?\s*")


def _generic_close(prefix):
    """Whether the ">" ending prefix closes type arguments written after a type name (List<String> name()."""
    depth = 0
    for index in range(len(prefix) - 1, -1, -1):
        char = prefix[index]
        if char == ">":
            depth += 1
        elif char == "<":
            depth -= 1
            if depth == 0:
                return re.search(r"[A-Za-z_$][\w$]*\s*$", prefix[:index]) is not None
        elif not (char.isalnum() or char in "_$.,?[]@ \t"):
            return False
    return False


def _type_like_end(text):
    """Whether text ends with a result type or modifier: an identifier that is no call keyword, List<T> or T[]."""
    token = _TYPE_END.search(text)
    if token is None:
        return False
    last = token.group(1)
    if last == "]":
        return re.search(r"\[\s*\]\s*$", text) is not None
    if last == ">":
        return _generic_close(text)
    return last not in _CALL_WORDS


def _typed_parameters(params):
    if "->" in params or not params.strip():
        return False
    parts, depth, start = [], 0, 0
    for index, char in enumerate(params):
        if char in "(<[":
            depth += 1
        elif char in ")>]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(params[start:index])
            start = index + 1
    parts.append(params[start:])
    return all(_TYPED_PARAMETER.fullmatch(part.strip()) for part in parts)


def _previous_code_line(lines, number):
    for index in range(number - 2, max(-1, number - 7), -1):
        text = _STRING.sub(lambda m: " " * len(m.group()), lines[index])
        text = re.sub(r"//.*$|/\*.*?\*/", " ", text).rstrip()
        if text.strip() and not text.lstrip().startswith(("*", "/*")):
            return text
    return None


def _declaration_header(lines, masked, number, match, max_lines):
    """Whether a heuristic call match is a Java method/constructor declaration header, not an invocation."""
    if "." in match.group(1):
        return False
    fragment = "\n".join(lines[number - 1:number - 1 + max_lines])[match.end():]
    clean = _STRING.sub(lambda m: " " * len(m.group()), fragment)
    clean = re.sub(r"//[^\n]*|/\*.*?\*/", lambda m: "".join("\n" if c == "\n" else " " for c in m.group()), clean, flags=re.S)
    depth, close = 1, None
    for offset, char in enumerate(clean):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close = offset
                break
    if close is None:
        return False
    after = clean[close + 1:].lstrip()
    if not (after.startswith(("{", ";")) or re.match(r"(?:throws|default)\b", after)):
        return False
    if _typed_parameters(clean[:close]):
        return True
    prefix = re.sub(r"/\*.*?\*/", " ", masked[:match.start()])
    if prefix.strip():
        return _type_like_end(prefix)
    previous = _previous_code_line(lines, number)
    return previous is not None and (_ANNOTATION_LINE.fullmatch(previous) is not None or _type_like_end(previous))


def _heuristic(path, text, custom, max_lines, exclude_declarations=False):
    lines, receivers, sites = text.splitlines(), set(), []
    declarations = exclude_declarations and path.lower().endswith(_DECLARATION_SUFFIXES)
    for line in lines:
        factory = re.search(r"([A-Za-z_]\w*)\s*=.*(?:getLogger|get_logger|createLogger|pino|KotlinLogging\.logger)", line)
        if factory:
            receivers.add(factory.group(1))
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith(("#", "//", "/*", "*")):
            continue
        # Mask strings before looking for calls, preserving offsets.
        masked = _STRING.sub(lambda match: " " * len(match.group()), line)
        for match in _CALL.finditer(masked):
            name = match.group(1)
            kind = _kind(name, receivers, custom)
            if not kind or re.search(r"\b(?:def|function|fun)\s*$", line[:match.start()]):
                continue
            if declarations and _declaration_header(lines, masked, number, match, max_lines):
                continue
            fragment = "\n".join(lines[number - 1:number - 1 + max_lines])[match.end():]
            # Bounded bracket balance with quoted strings and comments masked.
            clean = _STRING.sub(lambda m: " " * len(m.group()), fragment)
            clean = re.sub(r"//[^\n]*|#[^\n]*|/\*.*?\*/", lambda m: "".join("\n" if c == "\n" else " " for c in m.group()), clean, flags=re.S)
            depth, end_offset = 1, None
            for offset, char in enumerate(clean):
                if char == "(":depth += 1
                elif char == ")":depth -= 1
                if depth == 0:
                    end_offset = offset
                    break
            end = number + (fragment[:end_offset + 1].count("\n") if end_offset is not None else 0)
            span = fragment[:end_offset] if end_offset is not None else fragment.splitlines()[0]
            first = _STRING.match(span.lstrip())
            key, status = None, "unparsed" if end_offset is None else "dynamic"
            if first and status != "unparsed":
                token = first.group()
                if token[0] == "`" and "${" in token:
                    status = "dynamic"
                else:
                    try:
                        key = ast.literal_eval(token) if token[0] != "`" else token[1:-1]
                        status = "literal"
                    except (ValueError, SyntaxError):
                        status = "unparsed"
            fields = re.findall(r'''["']([A-Za-z_][\w.-]{0,63})["']\s*:|\b([A-Za-z_]\w*)\s*[:=](?!=)''', span)
            sites.append(_record(path, number, min(end, len(lines)), name, kind, key, status,
                                 [a or b for a,b in fields], "<unresolved>", "heuristic", True, match.start()))
    return sites, []


def discover_logging(snapshot, call_names=(), max_sites=300, max_per_file=40, max_call_lines=12, max_hints=100,
                     include_columns=False, exclude_declarations=False):
    """Return an inventory of static candidates, with omissions and no trust flags.

    The defaults keep the legacy bounded output. Whole-scope review passes None for
    max_sites, max_per_file and max_hints (configuration and wrapper hints) to keep all.
    exclude_declarations=True (map-first review only) drops Java method/constructor declaration
    headers from the heuristic candidates and records scope.declaration_filter; the default output,
    request and cache identities are unchanged.
    """
    if (type(include_columns) is not bool or type(exclude_declarations) is not bool
            or any(value is not None and (type(value) is not int or value < 1) for value in (max_sites, max_per_file, max_hints))
            or type(max_call_lines) is not int or not 1 <= max_call_lines <= 80):
        raise ValueError("Discovery limits must be positive; call ranges cannot exceed 80 lines")
    if not isinstance(call_names, (tuple, list)) or any(type(name) is not str or not _NAME.fullmatch(name) for name in call_names):
        raise ValueError("Configured log calls must be dotted identifiers")
    sites, wrappers, hints, omitted, extractors, fallback = [], [], [], {}, Counter(), []
    total = 0
    for path, source in sorted(snapshot.files.items()):
        mode = "python_ast" if path.endswith(".py") else "heuristic"
        try:
            found, declared = _python(path, source.text, call_names, max_call_lines) if mode == "python_ast" else _heuristic(path, source.text, call_names, max_call_lines, exclude_declarations)
        except (SyntaxError, RecursionError, ValueError):
            mode = "ast_fallback"
            fallback.append(path)
            found, declared = _heuristic(path, source.text, call_names, max_call_lines)
        extractors[mode] += 1
        found.sort(key=lambda item:(item["start_line"], item["end_line"], item["callee"]))
        if not include_columns:
            # Preserve legacy request/cache serialization; the new map consumer opts in.
            for site in found: site.pop('column', None)
        total += len(found)
        keep = len(found)
        if max_per_file is not None:
            keep = min(keep, max_per_file)
        if max_sites is not None:
            keep = min(keep, max(0, max_sites - len(sites)))
        sites.extend(found[:keep])
        if keep < len(found):omitted[path] = len(found) - keep
        wrappers.extend(declared)
        for number, line in enumerate(source.text.splitlines(), 1):
            config_file=path.endswith((".xml",".conf",".ini",".cfg",".config",".yaml",".yml",".properties",".toml")) or bool(re.search(r"(?:logging|logback|log4j|fluent|otel)",path,re.I))
            if (config_file and _HINT.search(line)) or _DELIVERY_HINT.search(line):
                hints.append({"path":path, "start_line":number, "end_line":number,
                              "kind":"configuration_hint" if config_file else "delivery_hint", "text":line[:240]})
    limitations = ["Static candidates only: enabled logging, runtime reachability, commit, destinations and delivery are not established",
                   "Message literals are candidate event keys; field names do not prove business identity or delivered schema",
                   "Unselected files and unsupported idioms are outside this search; no site does not mean no logging",
                   "Wrapper/configured-call candidates are not direct emission evidence; only same-file wrapper hints are discovered"]
    if omitted:limitations.append(f"Logging discovery truncated: {sum(omitted.values())} sites omitted")
    if fallback:limitations.append("Python AST unavailable or line numbering incompatible; heuristic fallback: " + ", ".join(fallback))
    if max_hints is None:
        hints_omitted = wrappers_omitted = 0
    else:
        hints_omitted, wrappers_omitted = max(0,len(hints)-max_hints), max(0,len(wrappers)-max_hints)
        hints, wrappers = hints[:max_hints], wrappers[:max_hints]
    if hints_omitted or wrappers_omitted:limitations.append("Configuration/wrapper hints truncated by explicit caps")
    scope = {"files_scanned":len(snapshot.files), "partial_input":snapshot.scope["partial_input"],
             "call_names":list(call_names), "extractors":dict(extractors), "sites_found":total,
             "sites_returned":len(sites), "truncated":bool(omitted or hints_omitted or wrappers_omitted),
             "omitted_by_path":omitted, "hints_omitted":hints_omitted, "wrappers_omitted":wrappers_omitted}
    if exclude_declarations:
        scope["declaration_filter"] = DECLARATION_FILTER
    return {"tool":"discover_logging/1", "snapshot_id":snapshot.snapshot_id,
            "scope":scope, "sites":sites, "wrappers":wrappers, "config_hints":hints, "limitations":limitations}


def _emits(anchor, event_key, candidates):
    """A static emit candidate inside a cited range: text-level support, never runtime proof."""
    return any(item["path"] == anchor.path and item.get("start_line", item.get("line")) <= anchor.end_line
               and item.get("end_line", item.get("line")) >= anchor.start_line
               and item.get("event_key") == event_key
               and item.get("key_status", "literal") in {"literal", "resolved_constant"}
               and item.get("kind") not in {"wrapper_call", "configured_call"} for item in candidates)
