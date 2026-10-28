"""Names and dependency hints from delivered text only; never execute sources.

Declarations are deliberately conservative lexical hints, not a call graph.
Unrecognized syntax remains a file fragment instead of receiving an invented name.
"""
from __future__ import annotations

import ast
import re

# Keyword declarations: Kotlin/Scala/Swift fun/def/func, Go func with an
# optional receiver, JavaScript/TypeScript/PHP function, Rust fn, Ruby def.
_MODIFIERS = (r'(?:(?:public|private|protected|internal|static|final|open|override|abstract|suspend|inline|'
              r'async|export|default|pub(?:\([^)]*\))?|unsafe|extern|private\s*\[\w+\])\s+)*')
_KEYWORD_DECLARATION = re.compile(
    r'(?m)^\s*' + _MODIFIERS + r'(?:fun|func|function\*?|fn|def)\s+(?:\([^)]*\)\s*)?(?:<[^>]*>\s*)?'
    r'(?:[A-Za-z_][\w.]*(?:<[^>]*>)?\??\.)?([A-Za-z_]\w*[!?]?)\s*(?=[(<\[:=]|$)')
# JavaScript/TypeScript functions bound to a name and class methods with a body.
_ASSIGNED_FUNCTION = re.compile(
    r'(?m)^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s+)?'
    r'(?:function\b|\([^)]*\)\s*(?::\s*[^=]+?)?\s*=>|\w+\s*=>)')
_METHOD = re.compile(
    r'(?m)^\s*(?:(?:public|private|protected|static|async|override|readonly|get|set)\s+)*'
    r'(\w+)\s*\([^)]*\)\s*(?::\s*[^{;=]+)?\{')
_NOT_METHODS = frozenset({"if", "for", "while", "switch", "catch", "with", "return", "function", "else",
                          "do", "try", "new", "await", "typeof", "match", "when", "foreach", "using", "lock"})


def code_only(value: str) -> str:
    return re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*[\s\S]*?\*/|//[^\n]*|\#[^\n]*',
                  lambda m: ''.join('\n' if c == '\n' else ' ' for c in m[0]), value)


def symbols(text: str) -> set[str]:
    result = set()
    try:
        tree = ast.parse(text)
        result.update(n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    except (SyntaxError, ValueError):
        pass
    # Require a declaration modifier/type, not a call or a word in a comment.
    cleaned = code_only(text)
    result.update(re.findall(
        r'(?m)^\s*(?:(?:public|private|protected|static|final|synchronized|abstract|override)\s+)+'
        r'(?:[\w.$<>?,\[\]]+\s+)+(\w+)\s*\(', cleaned))
    result.update(re.findall(r'(?m)^\s*(?:async\s+)?def\s+(\w+)\s*\(', cleaned))
    result.update(_KEYWORD_DECLARATION.findall(cleaned))
    result.update(_ASSIGNED_FUNCTION.findall(cleaned))
    result.update(name for name in _METHOD.findall(cleaned) if name not in _NOT_METHODS)
    return result


def symbol_valid(name: str, kind: str, text: str) -> bool:
    if kind == "function":
        return name in symbols(text)
    if kind == "field":
        return bool(name) and bool(re.search(r'(?m)^\s*(?:(?:public|private|protected|static|final)\s+)*'
                              r'(?:[\w.$<>?,\[\]]+\s+)?' + re.escape(name) + r'\s*=', code_only(text)))
    if kind == "lambda":
        return not name and bool(re.search(r'->|=>|\blambda\b', code_only(text)))
    return kind == "fragment" and not name


BUILDER_METHODS = frozenset({"builder", "newBuilder"})


def dependencies(path: str, lines: dict[int, str]) -> list[dict]:
    """Explicit orchestration/prompt references are work, not proof of execution."""
    found = []
    # Mask strings/comments across contiguous read windows, never filling unread
    # gaps with source text. The output is a conservative discovery hint.
    numbers = sorted(lines)
    windows = []
    for number in numbers:
        if not windows or number != windows[-1][-1] + 1:
            windows.append([])
        windows[-1].append(number)
    cleaned_lines = [(number, code) for window in windows
                     for number, code in zip(window, code_only('\n'.join(lines[n] for n in window)).split('\n'))]
    # Resolve explicit local aliases only from delivered declarations. A bare
    # parameter such as runAgent(agent) cannot be bound across call sites by this
    # lexical helper; the investigator must inspect its callers.
    bindings = {}
    window_for = {number: i for i, window in enumerate(windows) for number in window}
    scopes = {}
    current_scope = {}
    for number, code in cleaned_lines:
        window = window_for[number]
        if symbols(code):
            current_scope[window] = number
        scopes[number] = (window, current_scope.get(window, 0))
    code_at = dict(cleaned_lines)

    def statement_end(start):
        """Last line of the statement starting at start, within its read window."""
        depth, window = 0, window_for[start]
        for number in windows[window]:
            if number < start:
                continue
            code = code_at[number]
            depth += code.count('(') + code.count('{') - code.count(')') - code.count('}')
            if depth <= 0 and ';' in code:
                return number
        return windows[window][-1]

    def inline(match_class, match_method, number):
        # A builder call defines the executor here; its handler is local code,
        # while the library builder itself is never the implementation to read.
        if match_method not in BUILDER_METHODS:
            return None
        return [{"path": path, "line_start": number, "line_end": statement_end(number)}]

    for number, code in cleaned_lines:
        for match in re.finditer(r'\b(?:Agent|AgentNode|Role|var|final\s+var)\s+(\w+)\s*=\s*([A-Z]\w*)\.(\w+)\s*\(', code):
            bindings.setdefault((scopes[number], match[1]), []).append((match[2]+'.'+match[3], number, match[1]))
    seen = set()
    def add(kind, target, number, binding_line=None, implementation=None):
        key = (kind, target, number)
        if key in seen:
            return
        seen.add(key)
        evidence = [{"path": path, "line_start": number, "line_end": number}]
        if binding_line is not None and binding_line != number:
            evidence.append({"path": path, "line_start": binding_line, "line_end": binding_line})
        item = {"kind": kind, "target": target, "evidence": evidence}
        if implementation:
            item["implementation"] = implementation
        found.append(item)
    for number, code in cleaned_lines:
        for match in re.finditer(r'\.(systemPrompt|addChild|runAgent)\(\s*([A-Z]\w*)\.(\w+)', code):
            kind = "prompt" if match[1] == "systemPrompt" else "executor"
            local = inline(match[2], match[3], number) if kind == "executor" else None
            add(kind, f"встроенная роль в строке {number} ({match[2]}.{match[3]})" if local else match[2]+'.'+match[3],
                number, implementation=local)
        for match in re.finditer(r'\.(addChild|runAgent)\(\s*([a-zA-Z_]\w*)\s*[,)]', code):
            candidates = [(target, line, name) for target, line, name in bindings.get((scopes[number], match[2]), []) if line < number]
            # Reused variable names or declarations in multiple methods are
            # ambiguous, not proof of a global binding.
            if len(candidates) == 1:
                target, line, name = candidates[0]
                local = inline(*target.split('.', 1), line)
                add('executor', f"{name} ({target})" if local else target, number, line, implementation=local)
        for match in re.finditer(r'\breturn\s+([A-Z]\w*)\.(createAgent)\s*\(', code):
            add('executor', match[1]+'.'+match[2], number)
    return found
