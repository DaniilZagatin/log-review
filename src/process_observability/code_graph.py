"""Deterministic static code graph over redacted frozen source text; never imports target code.

The graph is navigation evidence for review planning, not a process map. A call
target is a syntactic candidate bound to one snapshot: it does not establish
runtime dispatch, reachability or execution. Name-match hints (``not_edges``),
callback references and messaging literals are separate records, never edges.

Schema (``CodeGraph.to_dict()``):
- nodes: id, path, qualname, name, kind, parent_id, start_line, end_line, signature,
  extractor, log_sites (inventory site ids), wrapper_candidate (inventory wrapper).
- calls: id, path, start_line, end_line, column, owner_id, callee, status, target_id,
  target_path, basis; optional not_edges/not_edges_total and wrapper_hint.
- imports, references (kind reference_arg), literals (relation literal_only), files,
  source_roots. Columns are Python AST UTF-8 byte offsets.
"""

import ast
import builtins
from collections import namedtuple
import copy
import hashlib
import json
from pathlib import PurePosixPath
import re

from .log_discovery import _ODD_LINES, _callee, discover_logging

TOOL = "code_graph/1"
CALL_STATUSES = ("local_def", "resolved_import", "module_only", "same_class_lexical", "ambiguous_binding",
                 "unresolved_receiver", "dynamic", "external_or_builtin")
LIMITATIONS = (
    "Static navigation graph over redacted frozen text; analyzed code is never imported or executed",
    "Call targets are syntactic candidates: runtime dispatch, reachability and execution are not established",
    "Imports resolve only to exact module paths under the listed source roots; re-exports, rebinding and receiver types stay unresolved or ambiguous",
    "not_edges, reference_arg records and literals are hints, never call, execution or delivery edges",
    "Wrapper hints mark candidate logging wrappers from the inventory; they do not confirm emission",
    "Non-Python declarations are heuristic boundaries without call resolution; other files expose module/window navigation only",
)

_WINDOW = 80
_HINT_CAP = 20
_SIGNATURE = 240
_BUILTINS = frozenset(dir(builtins))
_CALLABLE = frozenset({"class", "function", "async_function", "method"})
_MESSAGING = frozenset({"publish", "publisher", "send", "sender", "emit", "subscribe", "subscriber", "consume",
                        "consumer", "produce", "producer", "listen", "listener", "enqueue", "dispatch", "topic", "queue"})
_TOPIC_KEYWORDS = frozenset({"topic", "topics", "queue", "queues", "channel", "channels", "subject", "destination",
                             "routing_key", "exchange", "stream", "event", "event_type", "event_name"})
_HEURISTIC = {".java": ("java", "c"), ".cs": ("csharp", "c"), ".kt": ("kotlin", "fun"), ".scala": ("scala", "fun"),
              ".go": ("go", "go"), ".js": ("javascript", "js"), ".jsx": ("javascript", "js"),
              ".ts": ("typescript", "js"), ".tsx": ("typescript", "js")}
_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85  ")
_MATCH_NAMES = tuple(getattr(ast, name) for name in ("MatchAs", "MatchStar") if hasattr(ast, name))
_MATCH_MAPPING = getattr(ast, "MatchMapping", ())
_KEYWORDS = frozenset({"if", "for", "while", "switch", "catch", "return", "new", "throw", "else", "case", "do", "try",
                       "yield", "await", "goto", "package", "import", "using", "namespace", "lock", "foreach", "when",
                       "function", "synchronized", "typeof", "sizeof", "delete"})

_TYPE = re.compile(r"^\s*(?:(?:@[\w.]+(?:\([^)]*\))?|\[[^\]]*\]|[a-z]+)\s+)*"
                   r"(?:class|interface|enum|record|object|struct|trait)\s+(?P<name>[A-Za-z_$][\w$]*)")
_C_METHOD = re.compile(r"^\s*(?:(?:@[\w.]+(?:\([^)]*\))?|\[[^\]]*\])\s*)*"
                       r"(?:(?:public|protected|private|internal|static|final|abstract|synchronized|native|default|async"
                       r"|override|virtual|sealed|extern|unsafe|partial|new|readonly|strictfp)\s+)*(?:<[^>]*>\s*)?"
                       r"(?P<type>[\w.$]+(?:\s*<[^()]*?>)?(?:\s*\[\s*\])*\??)\s+(?P<name>[A-Za-z_$][\w$]*)\s*\(")
_C_CTOR = re.compile(r"^\s*(?:(?:public|protected|private|internal)\s+)+(?P<name>[A-Z][\w$]*)\s*\(")
_FUN = re.compile(r"^\s*(?:(?:@[\w.]+(?:\([^)]*\))?|[a-z]+)\s+)*(?:fun|def)\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?"
                  r"(?P<name>[A-Za-z_$][\w$]*)\s*[\[(<:=]")
_GO_FUNC = re.compile(r"^func\s*(?:\(\s*(?:[A-Za-z_]\w*\s+)?\*?\s*(?P<receiver>[A-Za-z_]\w*)[^)]*\)\s*)?"
                      r"(?P<name>[A-Za-z_]\w*)\s*[\[(]")
_GO_TYPE = re.compile(r"^type\s+(?P<name>[A-Za-z_]\w*)(?:\[[^\]]*\])?\s+(?:struct|interface)\b")
_JS_FUNC = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)")
_JS_ARROW = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?"
                       r"(?:function\b|(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=>)")
_JS_METHOD = re.compile(r"^\s*(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set)\s+)*\*?"
                        r"(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*[^{;=]+)?\{")
_DECL_START = re.compile(r"(?:^|\s)(?:fun|def|func|function|class|interface|object|struct|enum|record|trait|type)\s")
_HCALL = re.compile(r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(")
_HSTRING = re.compile(r'''\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''')
_HKEYWORD = re.compile(r'''\b(topics?|queues?|channels?|destination|subject|routing[_-]?key|exchange)\s*[=:]\s*\{?\s*'''
                       r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''')


def _digest(prefix, *parts):
    text = "\x1f".join(str(part) for part in parts)
    return prefix + hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:24]


def _canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _prefix(root):
    return root + "/" if root else ""


def _join(stem, name):
    return f"{stem}/{name}" if stem else name


def _dotted(node):
    """Name/attribute chain as identifiers, or None for computed callees."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return base + [node.attr] if base else None
    return None


def _render(node):
    """Identifier-only callee label; string arguments are never copied into it."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _render(node.value) + "." + node.attr
    if isinstance(node, ast.Call):
        return _render(node.func) + "(...)"
    if isinstance(node, ast.Subscript):
        return _render(node.value) + "[...]"
    return "<expr>"


def _messaging(callee):
    last = callee.rsplit(".", 1)[-1]
    tokens = {token.lower() for token in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", last)}
    return last in {"on", "once"} or bool(tokens & _MESSAGING)


def _roots(source_roots):
    if isinstance(source_roots, str) or not isinstance(source_roots, (tuple, list)) or not source_roots:
        raise ValueError("source_roots must be a nonempty list of relative directories")
    roots = []
    for root in source_roots:
        if type(root) is not str or not root.strip() or root.startswith("/") or "\\" in root:
            raise ValueError("source_roots must be relative POSIX directories inside the snapshot")
        parts = PurePosixPath(root).parts
        if ".." in parts:
            raise ValueError("source_roots cannot leave the snapshot")
        norm = "/".join(parts)
        if norm not in roots:
            roots.append(norm)
    return tuple(roots)


def _mask(text):
    """Blank strings and // or /* */ comments, preserving offsets and splitlines() boundaries."""
    out, index, size, mode = [], 0, len(text), None
    while index < size:
        char = text[index]
        if char in _BREAKS:
            out.append(char)
            mode = "block" if mode == "block" else "`" if mode == "`" else None
            index += 1
            continue
        pair = text[index:index + 2]
        if mode is None:
            if pair in {"//", "/*"}:
                mode = "line" if pair == "//" else "block"
                out.append("  ")
                index += 2
                continue
            if char in "\"'`":
                mode = char
                char = " "
            out.append(char)
            index += 1
            continue
        if mode == "block" and pair == "*/":
            mode = None
            out.append("  ")
            index += 2
            continue
        if mode not in {"line", "block"}:
            if char == "\\" and index + 1 < size and text[index + 1] not in _BREAKS:
                out.append("  ")
                index += 2
                continue
            if char == mode:
                mode = None
        out.append(" ")
        index += 1
    return "".join(out).splitlines()


def _closing(masked, number, column):
    depth = 0
    for current in range(number, len(masked)):
        for char in masked[current][column if current == number else 0:]:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return current
    return len(masked) - 1


def _block_end(masked, index):
    """Heuristic end line: matching brace, terminating semicolon, else the header line."""
    for number in range(index, min(len(masked), index + 4)):
        line = masked[number]
        if number > index and (_DECL_START.search(line) or line.lstrip().startswith("}")):
            break
        brace, semicolon = line.find("{"), line.find(";")
        if brace >= 0 and (semicolon < 0 or brace < semicolon):
            return _closing(masked, number, brace)
        if semicolon >= 0:
            return number
    return index


def _declaration(family, line, inside_type):
    """Return (name, is_type, receiver) for a heuristic declaration header, else None."""
    if family == "go":
        match = _GO_TYPE.match(line)
        if match:
            return match.group("name"), True, None
        match = _GO_FUNC.match(line)
        return (match.group("name"), False, match.group("receiver")) if match else None
    match = _TYPE.match(line)
    if match and match.group("name") not in _KEYWORDS:
        return match.group("name"), True, None
    if family == "fun":
        match = _FUN.match(line)
    elif family == "js":
        match = _JS_FUNC.match(line) or _JS_ARROW.match(line)
        if not match and inside_type:
            match = _JS_METHOD.match(line)
    else:
        match = (_C_METHOD.match(line) or _C_CTOR.match(line)) if inside_type else None
    if not match or match.group("name") in _KEYWORDS or match.groupdict().get("type") in _KEYWORDS:
        return None
    return match.group("name"), False, None


class CodeGraph:
    """Immutable view over canonical graph data with small navigation indexes."""

    def __init__(self, data):
        self._text = _canonical(data)
        self._sha256 = hashlib.sha256(self._text.encode()).hexdigest()
        data = json.loads(self._text)
        self._by_path, self._out, self._in, self._literals = {}, {}, {}, {}
        for node in data["nodes"]:
            self._by_path.setdefault(node["path"], []).append(node)
        for call in data["calls"]:
            self._out.setdefault(call["owner_id"], []).append(call)
            if call["target_id"] is not None:
                self._in.setdefault(call["target_id"], []).append(call)
        for literal in data["literals"]:
            self._literals.setdefault(literal["value"], []).append(literal)

    @property
    def sha256(self):
        return self._sha256

    def to_dict(self):
        return json.loads(self._text)

    def decls_in(self, path, start_line, end_line):
        """Nodes of path whose span intersects the range, including module/window navigation nodes."""
        if type(start_line) is not int or type(end_line) is not int or start_line > end_line:
            raise ValueError("Invalid line range")
        return copy.deepcopy([node for node in self._by_path.get(path, ())
                              if node["start_line"] <= end_line and node["end_line"] >= start_line])

    def outgoing(self, symbol_id):
        """Call records owned by the symbol's immediate lexical scope."""
        return copy.deepcopy(self._out.get(symbol_id, []))

    def incoming(self, symbol_id):
        """Call records whose candidate target_id is the symbol; references and literals are excluded."""
        return copy.deepcopy(self._in.get(symbol_id, []))

    def literal_sites(self, text):
        """Exact literal matches; equal strings are not call, execution or delivery edges."""
        if type(text) is not str:
            raise ValueError("Literal text must be a string")
        return copy.deepcopy(self._literals.get(text, []))


_Val = namedtuple("_Val", "kind node stems path basis imported", defaults=(None, (), None, "", False))
_File = namedtuple("_File", "path sha text lines")


class _Scope:
    """Lexical declaration scope with flow-insensitive name bindings."""

    def __init__(self, node_id, kind, parent, qualname):
        self.node_id, self.kind, self.parent, self.qualname = node_id, kind, parent, qualname
        self.root = parent.root if parent else self
        self.bindings, self.globals, self.nonlocals = {}, set(), set()
        self.star, self.self_name = False, None

    def bind(self, name, binding):
        target = self
        if name in self.globals:
            target = self.root
        elif name in self.nonlocals and self.parent:
            target = self.parent
            while target.kind == "class":
                target = target.parent
        target.bindings.setdefault(name, []).append(binding)


class _Builder:
    def __init__(self, snapshot, roots):
        self.sources = sorted(snapshot.files.items())
        self.roots = roots
        self.python = {path for path, _ in self.sources if path.endswith(".py")}
        self.nodes, self.ids, self.def_lines = {}, set(), {}
        self.module_scopes, self.class_scopes = {}, {}
        self.files, self.imports, self.pending = [], [], []
        self.calls, self.references, self.literals = [], [], []
        self.by_name, self._added = {}, []

    def _unique(self, identifier):
        candidate, ordinal = identifier, 1
        while candidate in self.ids:
            ordinal += 1
            candidate = f"{identifier}-{ordinal}"
        self.ids.add(candidate)
        self._added.append(candidate)
        return candidate

    def _node(self, ctx, qualname, name, kind, parent_id, start, end, signature, extractor, column=0):
        node_id = self._unique(_digest("n-", ctx.path, ctx.sha, qualname, kind, start, end, column))
        self.nodes[node_id] = {"id": node_id, "path": ctx.path, "qualname": qualname, "name": name, "kind": kind,
                               "parent_id": parent_id, "start_line": start, "end_line": end,
                               "signature": signature[:_SIGNATURE], "extractor": extractor,
                               "log_sites": [], "wrapper_candidate": False}
        return node_id

    def _module(self, ctx, extractor):
        return self._node(ctx, "<module>", PurePosixPath(ctx.path).name, "module", None, 1,
                          max(1, len(ctx.lines)), "", extractor)

    def _windows(self, ctx, module_id):
        for start in range(1, len(ctx.lines) + 1, _WINDOW):
            end = min(len(ctx.lines), start + _WINDOW - 1)
            label = f"<window:{start}-{end}>"
            self._node(ctx, label, label, "window", module_id, start, end, "", "text_window")

    def _file_meta(self, ctx, language, extractor, fallback=None):
        self.files.append({"path": ctx.path, "sha256": ctx.sha, "language": language, "extractor": extractor,
                           "line_count": len(ctx.lines), "fallback": fallback, "modules": self._module_names(ctx.path)})

    def _module_names(self, path):
        names = set()
        for root in self.roots if path.endswith(".py") else ():
            if not path.startswith(_prefix(root)):
                continue
            parts = path[len(_prefix(root)):-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts and all(part.isidentifier() for part in parts):
                names.add(".".join(parts))
        return sorted(names)

    def _files_for(self, stems):
        """Exact module files for path stems; never suffix-matches other directories."""
        found = set()
        for stem in stems or ():
            if stem:
                found.update(path for path in (stem + ".py", stem + "/__init__.py") if path in self.python)
        return sorted(found)

    def _absolute(self, module):
        parts = module.split(".")
        if not all(part.isidentifier() for part in parts):
            return None
        return tuple(_prefix(root) + "/".join(parts) for root in self.roots)

    def _relative(self, path, level, module):
        """Path-based relative import stem; the base package must lie inside a listed root."""
        directory = path.split("/")[:-1]
        if level - 1 > len(directory):
            return None
        base = "/".join(directory[:len(directory) - (level - 1)])
        if not any(path.startswith(_prefix(root)) and (base + "/").startswith(_prefix(root)) and len(base) > len(root)
                   for root in self.roots):
            return None
        if module and not all(part.isidentifier() for part in module.split(".")):
            return None
        return (_join(base, module.replace(".", "/")) if module else base,)

    def _python(self, ctx):
        reason, tree = None, None
        if _ODD_LINES.search(ctx.text):
            reason = "Nonphysical line separators: AST lines would disagree with evidence lines"
        else:
            try:
                tree = ast.parse(ctx.text)
            except (SyntaxError, ValueError, RecursionError) as exc:
                reason = f"{type(exc).__name__} at line {getattr(exc, 'lineno', None)}: Python AST unavailable"
        if tree is not None:
            self._added, marks = [], (len(self.pending), len(self.imports))
            try:
                scope = _Scope(self._module(ctx, "python_ast"), "module", None, "")
                self.module_scopes[ctx.path] = scope
                for statement in tree.body:
                    self._visit(statement, scope, ctx)
                self._file_meta(ctx, "python", "python_ast")
                return
            except RecursionError:
                # Roll back partial records so a failed file contributes no AST edges.
                del self.pending[marks[0]:], self.imports[marks[1]:]
                for identifier in self._added:
                    self.ids.discard(identifier)
                    for table in (self.nodes, self.class_scopes, self.def_lines):
                        table.pop(identifier, None)
                self.module_scopes.pop(ctx.path, None)
                reason = "RecursionError: nesting too deep for the graph visitor"
        self._windows(ctx, self._module(ctx, "python_fallback"))
        self._file_meta(ctx, "python", "python_fallback", reason)

    def _visit(self, node, scope, ctx, decorates=None):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return self._declare(node, scope, ctx)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return self._import(node, scope, ctx)
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            (scope.globals if isinstance(node, ast.Global) else scope.nonlocals).update(node.names)
            return None
        if isinstance(node, ast.Call):
            self.pending.append((node, scope, ctx, decorates))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            scope.bind(node.id, ("assign",))
        elif isinstance(node, ast.arg):
            scope.bind(node.arg, ("param",))  # lambda parameters; def parameters are bound by _declare
        elif isinstance(node, ast.ExceptHandler) and node.name:
            scope.bind(node.name, ("assign",))
        elif isinstance(node, _MATCH_NAMES) and node.name:
            scope.bind(node.name, ("assign",))
        elif isinstance(node, _MATCH_MAPPING) and node.rest:
            scope.bind(node.rest, ("assign",))
        for child in ast.iter_child_nodes(node):
            self._visit(child, scope, ctx)
        return None

    def _declare(self, node, scope, ctx):
        is_class = isinstance(node, ast.ClassDef)
        if is_class:
            kind = "class"
        elif scope.kind == "class":
            kind = "method"
        else:
            kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
        start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
        qualname = f"{scope.qualname}.{node.name}" if scope.qualname else node.name
        last = max(node.lineno, node.body[0].lineno - 1) if node.body else node.lineno
        signature = " ".join(" ".join(ctx.lines[node.lineno - 1:last]).split())
        decl_id = self._node(ctx, qualname, node.name, kind, scope.node_id, start, node.end_lineno, signature,
                             "python_ast", node.col_offset)
        self.def_lines[decl_id] = node.lineno
        # Decorators, defaults, annotations and bases evaluate in the enclosing scope.
        for decorator in node.decorator_list:
            self._visit(decorator, scope, ctx, decorates=decl_id)
        child = _Scope(decl_id, "class" if is_class else "function", scope, qualname)
        if is_class:
            for expression in [*node.bases, *(keyword.value for keyword in node.keywords)]:
                self._visit(expression, scope, ctx)
            self.class_scopes[decl_id] = child
        else:
            args = node.args
            params = [*args.posonlyargs, *args.args]
            every = [*params, *args.kwonlyargs, *(arg for arg in (args.vararg, args.kwarg) if arg)]
            outer = [*args.defaults, *(value for value in args.kw_defaults if value is not None),
                     *(arg.annotation for arg in every if arg.annotation), *([node.returns] if node.returns else [])]
            for expression in outer:
                self._visit(expression, scope, ctx)
            for arg in every:
                child.bind(arg.arg, ("param",))
            if kind == "method" and params and not any(_callee(item) == "staticmethod" for item in node.decorator_list):
                child.self_name = params[0].arg
        scope.bind(node.name, ("decl", decl_id))
        for statement in node.body:
            self._visit(statement, child, ctx)

    def _import(self, node, scope, ctx):
        if isinstance(node, ast.Import):
            for index, alias in enumerate(node.names):
                bound = alias.asname or alias.name.split(".")[0]
                stems = self._absolute(alias.name)
                self._import_record(node, scope, ctx, index, "import", alias.name, 0, None, alias.asname, bound, stems, ())
                # `import a.b` binds `a`; `import a.b as x` binds the full module.
                scope.bind(bound, ("import", {"stems": stems if alias.asname else self._absolute(bound), "name": None}))
            return
        stems = self._absolute(node.module) if node.level == 0 else self._relative(ctx.path, node.level, node.module)
        for index, alias in enumerate(node.names):
            if alias.name == "*":
                scope.star = True
                self._import_record(node, scope, ctx, index, "from", node.module, node.level, "*", None, None, stems, ())
                continue
            bound = alias.asname or alias.name
            member = tuple(_join(stem, alias.name) for stem in stems) if stems else ()
            self._import_record(node, scope, ctx, index, "from", node.module, node.level, alias.name, alias.asname,
                                bound, stems, member)
            scope.bind(bound, ("import", {"stems": stems, "name": alias.name}))

    def _import_record(self, node, scope, ctx, index, statement, module, level, name, asname, bound, stems, member):
        files, members = self._files_for(stems), self._files_for(member)
        candidates = files or members
        status = ("outside_source_roots" if stems is None else "ambiguous" if len(candidates) > 1
                  else "resolved" if candidates else "not_in_snapshot")
        import_id = self._unique(_digest("i-", ctx.path, ctx.sha, node.lineno, node.col_offset, index, bound or "*"))
        self.imports.append({"id": import_id, "path": ctx.path, "owner_id": scope.node_id, "start_line": node.lineno,
                             "end_line": node.end_lineno, "column": node.col_offset, "statement": statement,
                             "module": module, "level": level, "name": name, "asname": asname, "bound_name": bound,
                             "status": status, "module_paths": files, "member_paths": members})

    def _lookup(self, scope, name):
        current, first = (scope.root if name in scope.globals else scope), True
        while current is not None:
            # Enclosing class bodies are not visible from nested function scopes.
            if first or current.kind != "class":
                if current.star:
                    # Unknown exports may replace an existing declaration or
                    # builtin as well as introduce an otherwise unbound name.
                    return [("wildcard",), ("wildcard",)]
                if name in current.bindings:
                    return current.bindings[name]
            current, first = current.parent, False
        return None

    def _name(self, scope, name):
        bindings = self._lookup(scope, name)
        if bindings is None:
            if scope.root.star and name not in _BUILTINS:
                return _Val("ambiguous", basis="Name may come from a star import")
            return _Val("external", basis="Builtin name" if name in _BUILTINS else "Name is not bound in the snapshot module")
        if len(bindings) > 1:
            return _Val("ambiguous", basis="Name has several bindings in its scope; rebinding/shadowing is not followed")
        kind = bindings[0][0]
        if kind == "decl":
            return _Val("decl", node=bindings[0][1])
        if kind == "import":
            info = bindings[0][1]
            if info["stems"] is None:
                return _Val("external", basis="Relative import outside the listed source roots")
            module = _Val("module", stems=info["stems"], imported=True)
            return module if info["name"] is None else self._member(module, info["name"])
        return _Val("variable", basis="Name is bound to a parameter or assignment")

    def _member(self, value, attr):
        if value.kind == "module":
            files = self._files_for(value.stems)
            sub = tuple(_join(stem, attr) for stem in value.stems)
            subs = self._files_for(sub)
            if len(files) > 1 or len(subs) > 1:
                return _Val("ambiguous", basis="Module path matches several files under the listed source roots")
            if files:
                scope = self.module_scopes.get(files[0])
                if scope is None and not subs:
                    return _Val("module_only", path=files[0], basis="Target module has no AST declarations (fallback extraction)")
                if scope is not None and scope.star:
                    return _Val("ambiguous", basis="Star import may replace the target module member")
                bindings = scope.bindings.get(attr, []) if scope else []
                if len(bindings) > 1:
                    return _Val("ambiguous", basis="Target module binds the name several times")
                if bindings and bindings[0][0] == "decl":
                    if subs:
                        return _Val("ambiguous", basis="Name matches both a declaration and a submodule")
                    return _Val("decl", node=bindings[0][1], imported=value.imported)
                if bindings:
                    basis = ("Name is re-exported by import; re-exports are not followed" if bindings[0][0] == "import"
                             else "Name is bound by assignment, not a declaration")
                    return _Val("module_only", path=files[0], basis=basis)
                if not subs:
                    basis = ("Name may come from a star import in the target module" if scope.star
                             else "Name is not declared in the target module")
                    return _Val("module_only", path=files[0], basis=basis)
            if subs:
                return _Val("module", stems=sub, imported=value.imported)
            return _Val("external", basis="Module is not in the snapshot under the listed source roots")
        if value.kind == "decl":
            if self.nodes[value.node]["kind"] != "class":
                return _Val("unknown", basis="Attribute of a function object")
            bindings = self.class_scopes[value.node].bindings.get(attr, [])
            if len(bindings) > 1:
                return _Val("ambiguous", basis="Class body binds the name several times; candidates are not joined")
            if bindings and bindings[0][0] == "decl":
                return _Val("decl", node=bindings[0][1], imported=value.imported)
            return _Val("unknown", basis="Attribute is not declared in the class body; inheritance and instance state are not followed")
        if value.kind == "variable":
            return _Val("unknown", basis="Receiver type of a variable or parameter is not tracked")
        return value

    def _status(self, value):
        if value.kind == "decl":
            node = self.nodes[value.node]
            return ("resolved_import" if value.imported else "local_def", node["id"], node["path"],
                    "Syntactic candidate; runtime dispatch and reachability are unverified")
        if value.kind == "module":
            files = self._files_for(value.stems)
            if len(files) == 1:
                return "module_only", None, files[0], "Callee resolves to a module, not a declaration"
            if files:
                return "ambiguous_binding", None, None, "Module path matches several files under the listed source roots"
            return "external_or_builtin", None, None, "Module is not in the snapshot under the listed source roots"
        status = {"module_only": "module_only", "external": "external_or_builtin",
                  "ambiguous": "ambiguous_binding"}.get(value.kind, "unresolved_receiver")
        return status, None, value.path, value.basis

    def _resolve(self, parts, scope):
        """Return (status, target_id, target_path, basis) for a dotted callee or reference."""
        if parts is None:
            return "dynamic", None, None, "Callee is a call result, subscript or other computed expression"
        if len(parts) > 1 and parts[0] == scope.self_name and scope.bindings.get(parts[0]) == [("param",)]:
            if len(parts) > 2:
                return "unresolved_receiver", None, None, "Instance attribute types are not tracked"
            bindings = scope.parent.bindings.get(parts[1], [])
            if len(bindings) > 1:
                return "ambiguous_binding", None, None, "Class body binds the name several times; candidates are not joined"
            if bindings and bindings[0][0] == "decl":
                node = self.nodes[bindings[0][1]]
                return ("same_class_lexical", node["id"], node["path"],
                        "Lexical candidate in the enclosing class; overrides and runtime dispatch are not followed")
            return ("unresolved_receiver", None, None,
                    "Attribute is not a declaration in the enclosing class body; inheritance is not followed")
        value = self._name(scope, parts[0])
        if len(parts) == 1 and value.kind == "variable":
            return "dynamic", None, None, "Call through a variable or parameter"
        for attr in parts[1:]:
            value = self._member(value, attr)
        return self._status(value)

    def _resolve_calls(self):
        owners = {}
        for node, scope, ctx, decorates in self.pending:
            parts, callee = _dotted(node.func), _render(node.func)
            status, target_id, target_path, basis = self._resolve(parts, scope)
            call_id = self._unique(_digest("c-", ctx.path, ctx.sha, node.lineno, node.col_offset, node.end_lineno,
                                           node.end_col_offset, callee))
            record = {"id": call_id, "path": ctx.path, "start_line": node.lineno, "end_line": node.end_lineno,
                      "column": node.col_offset, "owner_id": scope.node_id, "callee": callee, "status": status,
                      "target_id": target_id, "target_path": target_path, "basis": basis}
            if parts and status in {"ambiguous_binding", "unresolved_receiver"}:
                hints = self.by_name.get(parts[-1], [])
                if hints:
                    record["not_edges"] = hints[:_HINT_CAP]
                    record["not_edges_total"] = len(hints)
            if target_id and self.nodes[target_id]["wrapper_candidate"]:
                record["wrapper_hint"] = {"wrapper_id": target_id, "relation": "candidate_logging_wrapper",
                                          "basis": "Target forwards a parameter to an inventoried logging/emit call; emission is not confirmed"}
            self.calls.append(record)
            owners.setdefault((ctx.path, node.lineno, _callee(node.func)), set()).add(scope.node_id)
            self._arguments(node, scope, ctx, call_id)
            self._python_literals(node, scope, ctx, call_id, callee, decorates)
        return owners

    def _arguments(self, node, scope, ctx, call_id):
        items = [(index, None, arg) for index, arg in enumerate(node.args)]
        items += [(None, keyword.arg, keyword.value) for keyword in node.keywords if keyword.arg]
        for index, keyword, value in items:
            parts = _dotted(value)
            if parts is None:
                continue
            status, target_id, target_path, _ = self._resolve(parts, scope)
            if target_id is None or self.nodes[target_id]["kind"] not in _CALLABLE:
                continue
            reference_id = self._unique(_digest("r-", ctx.path, ctx.sha, value.lineno, value.col_offset,
                                                value.end_lineno, value.end_col_offset, call_id))
            self.references.append({"id": reference_id, "kind": "reference_arg", "path": ctx.path,
                                    "start_line": value.lineno, "end_line": value.end_lineno, "column": value.col_offset,
                                    "owner_id": scope.node_id, "call_id": call_id, "name": ".".join(parts),
                                    "arg_index": index, "keyword": keyword, "status": status, "target_id": target_id,
                                    "target_path": target_path, "relation": "reference_only"})

    def _python_literals(self, node, scope, ctx, call_id, callee, decorates):
        if not _messaging(callee):
            return
        found = [("first_arg", node.args[0])] if node.args else []
        found += [(f"keyword:{keyword.arg}", keyword.value) for keyword in node.keywords
                  if keyword.arg and keyword.arg.lower() in _TOPIC_KEYWORDS]
        for role, value in found:
            for item in value.elts if isinstance(value, (ast.List, ast.Tuple, ast.Set)) else [value]:
                if isinstance(item, ast.Constant) and type(item.value) is str:
                    self._literal(ctx, scope.node_id, item.lineno, item.end_lineno, item.col_offset, item.value, role,
                                  callee, call_id, decorates, "python_ast")

    def _literal(self, ctx, owner_id, start, end, column, value, role, callee, call_id, decorated_id, extractor):
        literal_id = self._unique(_digest("l-", ctx.path, ctx.sha, start, column, role, value))
        self.literals.append({"id": literal_id, "path": ctx.path, "start_line": start, "end_line": end,
                              "column": column, "owner_id": owner_id, "call_id": call_id, "callee": callee,
                              "role": role, "value": value, "decorated_id": decorated_id, "extractor": extractor,
                              "relation": "literal_only"})

    def _heuristic(self, ctx, language, family):
        """Marked heuristic declaration boundaries; no call records are fabricated."""
        mark = len(self._added)
        module_id = self._module(ctx, "heuristic")
        masked = _mask(ctx.text)
        prefixes = ("@", "[") if language == "csharp" else ("@",)
        stack = []  # (end index, qualname, node id, is type)
        for index, line in enumerate(masked):
            while stack and stack[-1][0] < index:
                stack.pop()
            found = _declaration(family, line, bool(stack) and stack[-1][3])
            if not found:
                continue
            name, is_type, receiver = found
            end, start = _block_end(masked, index), index
            while start > 0 and masked[start - 1].strip().startswith(prefixes):
                start -= 1
            parent = stack[-1] if stack else None
            qualname = f"{parent[1]}.{name}" if parent else f"{receiver}.{name}" if receiver else name
            node_id = self._node(ctx, qualname, name, "heuristic_type" if is_type else "heuristic_function",
                                 parent[2] if parent else module_id, start + 1, end + 1,
                                 " ".join(ctx.lines[index].split()), "heuristic")
            stack.append((end, qualname, node_id, is_type))
        nodes = [self.nodes[identifier] for identifier in self._added[mark:]]
        for index, (line, clean) in enumerate(zip(ctx.lines, masked)):
            for match in _HCALL.finditer(clean):
                if not _messaging(match.group(1)):
                    continue
                first = _HSTRING.match(line, match.end())
                found = [("first_arg", first)] if first else []
                if not found:
                    keyword = _HKEYWORD.search(line, match.end())
                    found = [(f"keyword:{keyword.group(1)}", keyword)] if keyword else []
                for role, item in found:
                    token, column = item.group(item.lastindex), item.start(item.lastindex)
                    if clean[column] != " ":
                        continue
                    self._literal(ctx, self._innermost(nodes, index + 1, index + 1), index + 1, index + 1, column,
                                  token[1:-1], role, match.group(1), None, None, "heuristic")
        self._file_meta(ctx, language, "heuristic")

    def _text(self, ctx):
        self._windows(ctx, self._module(ctx, "text_window"))
        self._file_meta(ctx, PurePosixPath(ctx.path).suffix.lstrip(".").lower() or "text", "text_window")

    def _depth(self, node):
        depth = 0
        while node["parent_id"]:
            node, depth = self.nodes[node["parent_id"]], depth + 1
        return depth

    def _innermost(self, nodes, first, last):
        best = None
        for node in nodes:
            if node["kind"] != "window" and node["start_line"] <= first and node["end_line"] >= last:
                key = (node["start_line"], -node["end_line"], node["kind"] != "module", self._depth(node))
                if best is None or key > best[0]:
                    best = (key, node["id"])
        return best[1] if best else None

    def _mark_wrappers(self, inventory, by_path):
        # Reuse the inventory's same-file wrapper candidates (top-level functions keyed by def line).
        for wrapper in inventory.get("wrappers") or []:
            for node in by_path.get(wrapper.get("path"), []):
                if (node["kind"] in {"function", "async_function"} and node["qualname"] == wrapper.get("name")
                        and self.def_lines.get(node["id"]) == wrapper.get("start_line")):
                    node["wrapper_candidate"] = True

    def _attach_sites(self, inventory, owners, by_path):
        for site in inventory.get("sites") or []:
            path, first, last = site.get("path"), site.get("start_line"), site.get("end_line")
            if path not in by_path or type(first) is not int or type(last) is not int:
                continue
            candidates = owners.get((path, first, site.get("callee")), set())
            owner = next(iter(candidates)) if len(candidates) == 1 else self._innermost(by_path[path], first, last)
            if owner:
                self.nodes[owner]["log_sites"].append(site["id"])
        for node in self.nodes.values():
            node["log_sites"].sort()

    def build(self, inventory):
        for path, source in self.sources:
            ctx = _File(path, source.sha256, source.text, source.text.splitlines())
            suffix = PurePosixPath(path).suffix.lower()
            if suffix == ".py":
                self._python(ctx)
            elif suffix in _HEURISTIC:
                self._heuristic(ctx, *_HEURISTIC[suffix])
            else:
                self._text(ctx)
        by_path = {}
        for node in self.nodes.values():
            by_path.setdefault(node["path"], []).append(node)
        self._mark_wrappers(inventory, by_path)
        for node in sorted(self.nodes.values(), key=lambda item: item["id"]):
            if node["extractor"] == "python_ast" and node["kind"] in {"function", "async_function", "method"}:
                self.by_name.setdefault(node["name"], []).append(
                    {"target_id": node["id"], "target_path": node["path"], "qualname": node["qualname"],
                     "relation": "name_match_only"})
        owners = self._resolve_calls()
        self._attach_sites(inventory, owners, by_path)
        nodes = sorted(self.nodes.values(), key=lambda item: (item["path"], item["start_line"], -item["end_line"],
                                                              item["qualname"], item["id"]))
        def position(item):
            return item["path"], item["start_line"], item["column"], item["end_line"], item["id"]
        return CodeGraph({"source_roots": [root or "." for root in self.roots], "files": self.files, "nodes": nodes,
                          "calls": sorted(self.calls, key=position), "imports": sorted(self.imports, key=position),
                          "references": sorted(self.references, key=position),
                          "literals": sorted(self.literals, key=position)})


def build_graph(snapshot, inventory=None, *, source_roots=(".", "src")):
    """Build the full deterministic graph from frozen redacted ``.text`` only; no node cutoff.

    ``inventory`` is the deterministic ``discover_logging`` result for the same snapshot;
    when omitted it is computed with the snapshot's configured logging calls.
    """
    roots = _roots(source_roots)
    if inventory is None:
        inventory = discover_logging(snapshot, call_names=tuple(getattr(snapshot, "logging_calls", ())))
    if (not isinstance(inventory, dict) or not isinstance(inventory.get("sites", []), list)
            or not isinstance(inventory.get("wrappers", []), list)):
        raise ValueError("Logging inventory must be a discover_logging result")
    if inventory.get("snapshot_id") not in (None, getattr(snapshot, "snapshot_id", None)):
        raise ValueError("Logging inventory belongs to a different snapshot")
    return _Builder(snapshot, roots).build(inventory)
