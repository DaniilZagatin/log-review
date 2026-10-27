"""Names and dependency hints from delivered text only; never execute sources.

Java declarations are deliberately conservative lexical hints, not a call graph.
Unrecognized syntax remains a file fragment instead of receiving an invented name.
"""
from __future__ import annotations

import ast
import re


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
    for number, code in cleaned_lines:
        for match in re.finditer(r'\.(systemPrompt|addChild)\(\s*([A-Z]\w*)\.(\w+)', code):
            kind = "prompt" if match[1] == "systemPrompt" else "executor"
            found.append({"kind": kind, "target": match[2] + "." + match[3],
                          "evidence": [{"path": path, "line_start": number, "line_end": number}]})
        for match in re.finditer(r'\breturn\s+([A-Z]\w*)\.(createAgent)\s*\(', code):
            found.append({"kind": "executor", "target": match[1]+'.'+match[2],
                          "evidence": [{"path": path, "line_start": number, "line_end": number}]})
    return found
