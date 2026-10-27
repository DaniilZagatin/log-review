"""Deterministic logging inventory over masked snapshot text; never imports target code.

Adapted from ``process_observability/log_discovery.py`` of the R07 delivery
(process-observability-mvp-r07-full.zip, SHA-256 CC020B77...1F60). The R07
reuse notes leave the licence of its PKO origin unestablished.

Sites are navigation hints. Literal message text is only a candidate event key;
call-site fields are not proof of fields in a delivered record. Wrapper candidates
are deliberately not promoted into direct emission evidence.
"""
from __future__ import annotations

import ast
import hashlib
import re
from collections import Counter

_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_FIELD = re.compile(r"[A-Za-z_][\w.-]{0,63}")
_LEVELS = {"trace", "debug", "info", "warn", "warning", "error", "exception", "critical", "fatal", "log",
           "loginformation", "logdebug", "logwarning", "logerror", "logcritical", "verbose", "information"}
_FACTORIES = {"getLogger", "get_logger", "createLogger", "pino", "logger"}
_CALL = re.compile(r"(?<![\w.])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(")
_STRING = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`''', re.S)
_HINT = re.compile(r"\b(?:appender|handler|exporter|logstash|fluent.?bit|otel|FileHandler|ConsoleHandler)\b", re.I)
_DELIVERY_HINT = re.compile(r"\b(?:KafkaProducer|bootstrap\.servers|OTLP\w*Exporter|Logstash\w*Appender)\b", re.I)
_CONFIG_SUFFIXES = (".xml", ".conf", ".ini", ".cfg", ".config", ".yaml", ".yml", ".properties", ".toml")
_CONFIG_NAME = re.compile(r"(?:logging|logback|log4j|fluent|otel)", re.I)
_ODD_LINES = re.compile(r"[\v\f\x1c-\x1e\x85  ]")

LIMITATIONS = [
    "Найдены только статические кандидаты: включённость логирования, достижимость, момент фиксации и доставка не установлены.",
    "Текст сообщения — кандидат ключа события; имена полей в вызове не доказывают состав доставленной записи.",
    "Файлы вне области поиска и неподдержанные конструкции не рассматривались: отсутствие места не означает отсутствие логирования.",
    "Обёртки и настроенные вызовы — не прямые места записи; обёртки ищутся только в пределах одного файла.",
]


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
    return {"id": "log-" + hashlib.sha256(signature.encode()).hexdigest()[:16], "path": path,
            "line_start": first, "line_end": last, "symbol": symbol, "callee": callee,
            "kind": kind, "level": callee.rsplit(".", 1)[-1] if kind == "log_call" else None,
            "event_key": key, "key_status": status,
            "fields": field_names[:32], "fields_status": "partial" if partial or len(field_names) > 32 else "syntactic",
            "extractor": extractor}


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
            wrappers.append({"name": node.name, "path": path, "line_start": node.lineno,
                             "line_end": min(node.end_lineno, node.lineno + 79)})
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


def _heuristic(path, text, custom, max_lines):
    lines, receivers, sites = text.splitlines(), set(), []
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
            fragment = "\n".join(lines[number - 1:number - 1 + max_lines])[match.end():]
            # Bounded bracket balance with quoted strings and comments masked.
            clean = _STRING.sub(lambda m: " " * len(m.group()), fragment)
            clean = re.sub(r"//[^\n]*|#[^\n]*|/\*.*?\*/", lambda m: "".join("\n" if c == "\n" else " " for c in m.group()), clean, flags=re.S)
            depth, end_offset = 1, None
            for offset, char in enumerate(clean):
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                if depth == 0:
                    end_offset = offset
                    break
            end = number + (fragment[:end_offset + 1].count("\n") if end_offset is not None else 0)
            span = fragment[:end_offset] if end_offset is not None else (fragment.splitlines() or [""])[0]
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
                                 [a or b for a, b in fields], None, "heuristic", True, match.start()))
    return sites, []


def _config_hints(path, text):
    config_file = path.endswith(_CONFIG_SUFFIXES) or bool(_CONFIG_NAME.search(path.rsplit("/", 1)[-1]))
    return [{"path": path, "line_start": number, "line_end": number,
             "kind": "configuration_hint" if config_file else "delivery_hint", "text": line.strip()[:240]}
            for number, line in enumerate(text.splitlines(), 1)
            if (config_file and _HINT.search(line)) or _DELIVERY_HINT.search(line)]


def logging_config_paths(tree) -> list[str]:
    """Logging configuration files anywhere in the snapshot; hints only."""
    return sorted(path for path in tree.files
                  if path.endswith(_CONFIG_SUFFIXES) and _CONFIG_NAME.search(path.rsplit("/", 1)[-1]))


def discover_logging(tree, paths, *, config_paths=(), call_names=(), max_sites=300, max_per_file=40,
                     max_call_lines=12, max_hints=100) -> dict:
    """Return static candidates for ``paths`` of ``tree`` with explicit omissions.

    Unreadable paths and truncation are reported, never silently dropped.
    """
    if (any(type(value) is not int or value < 1 for value in (max_sites, max_per_file, max_hints))
            or type(max_call_lines) is not int or not 1 <= max_call_lines <= 80):
        raise ValueError("Discovery limits must be positive; call ranges cannot exceed 80 lines")
    if any(type(name) is not str or not _NAME.fullmatch(name) for name in call_names):
        raise ValueError("Configured log calls must be dotted identifiers")
    sites, wrappers, hints, omitted, extractors, fallback, unreadable = [], [], [], {}, Counter(), [], []
    total = 0
    for path in sorted(set(paths)):
        text = tree.read(path)
        if text is None:
            unreadable.append(path)
            continue
        mode = "python_ast" if path.endswith(".py") else "heuristic"
        try:
            found, declared = (_python if mode == "python_ast" else _heuristic)(path, text, set(call_names), max_call_lines)
        except (SyntaxError, RecursionError, ValueError):
            mode = "ast_fallback"
            fallback.append(path)
            found, declared = _heuristic(path, text, set(call_names), max_call_lines)
        extractors[mode] += 1
        found.sort(key=lambda item: (item["line_start"], item["line_end"], item["callee"]))
        total += len(found)
        keep = min(len(found), max_per_file, max(0, max_sites - len(sites)))
        sites.extend(found[:keep])
        if keep < len(found):
            omitted[path] = len(found) - keep
        wrappers.extend(declared)
        hints.extend(_config_hints(path, text))
    for path in sorted(set(config_paths) - set(paths)):
        text = tree.read(path)
        if text is None:
            unreadable.append(path)
        else:
            hints.extend(_config_hints(path, text))
    limitations = list(LIMITATIONS)
    if omitted:
        limitations.append(f"Каталог усечён лимитами: не показано мест — {sum(omitted.values())}.")
    if fallback:
        limitations.append("Разбор Python через AST недоступен, использована эвристика: " + ", ".join(fallback))
    if unreadable:
        limitations.append("Не удалось прочитать файлы: " + ", ".join(sorted(unreadable)))
    hints_omitted, wrappers_omitted = max(0, len(hints) - max_hints), max(0, len(wrappers) - max_hints)
    hints, wrappers = hints[:max_hints], wrappers[:max_hints]
    if hints_omitted or wrappers_omitted:
        limitations.append("Подсказки конфигурации и обёрток усечены лимитами.")
    return {"tool": "discover_logging/1",
            "scope": {"files_scanned": sum(extractors.values()), "files_unreadable": sorted(unreadable),
                      "config_files": sorted(set(config_paths)), "call_names": list(call_names),
                      "extractors": dict(extractors), "sites_found": total, "sites_returned": len(sites),
                      "truncated": bool(omitted or hints_omitted or wrappers_omitted),
                      "omitted_by_path": omitted, "hints_omitted": hints_omitted, "wrappers_omitted": wrappers_omitted},
            "sites": sites, "wrappers": wrappers, "config_hints": hints, "limitations": limitations}
