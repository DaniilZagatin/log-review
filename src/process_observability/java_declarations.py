"""Conservative lexical Java declarations for the business declaration index (declaration_index.py).

extract(text) -> {"status": "indexed" | "partial" | "parse_failed", "error": None | {"type", "line"},
                  "declarations": [...], "diagnostics": [{"code", "line"}], "diagnostics_omitted": int,
                  "diagnostic_counts": {code: int}, "folded_bodies": int}

The original text is only lexed: nothing is compiled, imported or run. Comments, string, char and text-block
literals are masked, and every (, [ and { must be balanced before any declaration is reported. Declarations are
read from the balanced groups of type bodies only: classes, interfaces, enums, records and annotation types
(nested ones too), their methods, constructors (compact record constructors included) and initializer blocks, each
spanning the lines from its first annotation or modifier to its closing brace or semicolon. Method, constructor and
initializer bodies and field initializers are never searched for declarations: lambdas, calls, control blocks and
local or anonymous classes stay part of their member and are only counted (folded_bodies), as are enum constant
bodies.

An unrecognised construct is a bounded diagnostic (a code and a line, never source text) and makes the file
"partial"; an unterminated comment or literal, an unbalanced delimiter or a unicode escape outside a literal makes
it "parse_failed" with no declarations. This is a navigation index, not a Java compiler: names, types, overloads,
inheritance and calls are not resolved.

Unicode escapes are not translated. Java translates them before comments and literals are recognised (JLS 3.3), so
before lexing every eligible escape (a backslash after an even number of backslashes, one or more u) is checked:
a malformed one, or one that decodes to a line terminator, a quote, an apostrophe, a backslash, "*" or "/", could
end or open a comment or literal and fails the whole file. Any other escape inside a comment or literal cannot
change where they end; outside them the lexer still fails the file.
"""

import bisect
import re

EXTRACTOR = "java_lexical/1"
MAX_DIAGNOSTICS = 20
TYPE_KINDS = ("class", "interface", "enum", "record", "annotation_type")
_MODIFIERS = frozenset({"public", "protected", "private", "static", "final", "abstract", "native", "synchronized",
                        "transient", "volatile", "strictfp", "default", "sealed"})
_ACCESS = ("public", "protected", "private")
# Statement words that can precede "(" but never name a method.
_NOT_NAMES = frozenset({"if", "for", "while", "switch", "catch", "return", "new", "throw", "assert", "this", "super",
                        "synchronized", "try", "do", "else", "case"})
_PAIRS = {")": "(", "]": "[", "}": "{"}
_OPEN = ("(", "[", "{")

# One token per match; the "open_*" alternatives catch a comment or literal that never ends.
_TOKEN = re.compile(r'''
    (?P<space>[\s﻿]+)
  | (?P<comment>//[^\r\n]*|/\*.*?\*/)
  | (?P<open_comment>/\*)
  | (?P<text_block>"""[ \t\f]*(?:\r\n|\r|\n)(?:[^"\\]|\\.|"(?!""))*""")
  | (?P<open_text_block>""")
  | (?P<string>"(?:[^"\\\r\n]|\\[^\r\n])*")
  | (?P<open_string>")
  | (?P<char>'(?:[^'\\\r\n]|\\[^\r\n])+')
  | (?P<open_char>')
  | (?P<name>(?:[^\W\d]|\$)[\w$]*)
  | (?P<number>\.?\d(?:[eEpP][+-]|[\w.])*)
  | (?P<punct>->|::|\.\.\.|[{}()\[\];,.@<>=?:!~&|^%*/+\-])
  | (?P<other>.)
''', re.VERBOSE | re.DOTALL)
_TEXT_BLOCK_OPENING = re.compile(r'"""[ \t\f]*(?:\r|\n)')
_UNTERMINATED = {"open_comment": "UnterminatedComment", "open_string": "UnterminatedString",
                 "open_char": "UnterminatedChar"}
UNICODE_GUARD = "jls_3_3_structural_or_malformed_escape_fails/1"
_ESCAPES = re.compile(r"(\\+)(u+)")  # a whole backslash run and its u's: eligibility needs the run length
_HEX4 = re.compile(r"[0-9A-Fa-f]{4}")
# Characters whose translation can end or open a comment or a string, char or text-block literal.
_STRUCTURAL = frozenset("\n\r\"'\\*/")


class _Failure(Exception):
    def __init__(self, kind, line):
        super().__init__(kind)
        self.kind, self.line = kind, line


def _line_starts(text):
    """Offsets of line starts with the snapshot's line numbering (str.splitlines)."""
    starts, offset = [], 0
    for line in text.splitlines(keepends=True):
        starts.append(offset)
        offset += len(line)
    return starts


def _unicode_guard(text, starts):
    """Fail on an eligible unicode escape that could change comment or literal boundaries (JLS 3.3)."""
    for match in _ESCAPES.finditer(text):
        if len(match.group(1)) % 2 == 0:
            continue  # every backslash is escaped by the one before it: no unicode escape
        line = bisect.bisect_right(starts, match.end(1) - 1)
        digits = text[match.end():match.end() + 4]
        if not _HEX4.fullmatch(digits):
            raise _Failure("MalformedUnicodeEscape", line)
        if chr(int(digits, 16)) in _STRUCTURAL:
            raise _Failure("StructuralUnicodeEscape", line)


def _lex(text, starts):
    """(tokens, lexical notes). A token is (kind, value, line); literals keep no value."""
    tokens, notes, position, size = [], [], 0, len(text)
    while position < size:
        match = _TOKEN.match(text, position)
        kind, line = match.lastgroup, bisect.bisect_right(starts, position)
        if kind in _UNTERMINATED:
            raise _Failure(_UNTERMINATED[kind], line)
        if kind == "open_text_block":
            raise _Failure("UnterminatedTextBlock" if _TEXT_BLOCK_OPENING.match(text, position) else "InvalidTextBlock",
                           line)
        if kind in ("name", "punct"):
            tokens.append((kind, match.group(), line))
        elif kind in ("string", "char", "text_block", "number"):
            tokens.append(("literal", None, line))
        elif kind == "other":
            if match.group() == "\\":
                raise _Failure("UnicodeEscapeOutsideLiteral", line)
            notes.append(("unexpected_character", line))
            tokens.append(("other", None, line))
        position = match.end()
    return tokens, notes


def _balance(tokens):
    """Index of the partner of every (, [, { and ), ], }; any mismatch fails the whole file."""
    match, stack = {}, []
    for index, (kind, value, line) in enumerate(tokens):
        if kind != "punct":
            continue
        if value in _OPEN:
            stack.append(index)
        elif value in _PAIRS:
            if not stack or tokens[stack[-1]][1] != _PAIRS[value]:
                raise _Failure("UnbalancedDelimiter", line)
            opener = stack.pop()
            match[opener], match[index] = index, opener
    if stack:
        raise _Failure("UnbalancedDelimiter", tokens[stack[-1]][2])
    return match


def _visibility(modifiers, kind, owner_kind):
    access = next((item for item in modifiers if item in _ACCESS), None)
    if access is not None:
        return access
    if owner_kind in ("interface", "annotation_type"):
        return "public"  # members of interfaces and annotation types are implicitly public
    if kind == "constructor" and owner_kind == "enum":
        return "private"
    return "package"


class _Parser:
    def __init__(self, tokens, match):
        self.kinds = [token[0] for token in tokens]
        self.values = [token[1] for token in tokens]
        self.lines = [token[2] for token in tokens]
        self.size, self.match = len(tokens), match
        self.declarations, self.notes, self.folded = [], [], 0

    def value(self, index):
        return self.values[index] if 0 <= index < self.size else None

    def name(self, index):
        return self.values[index] if 0 <= index < self.size and self.kinds[index] == "name" else None

    def note(self, code, index):
        self.notes.append((code, self.lines[min(index, self.size - 1)]))

    def add(self, kind, name, start, name_at, close, owner, modifiers, annotations, body, count, signature):
        parent = None if owner is None else self.declarations[owner]
        self.declarations.append({
            "name": name, "qualname": name if parent is None else parent["qualname"] + "." + name, "kind": kind,
            "start_line": self.lines[start], "definition_line": self.lines[name_at], "end_line": self.lines[close],
            "parent": owner, "visibility": _visibility(modifiers, kind, None if parent is None else parent["kind"]),
            "modifiers": list(modifiers), "annotations": list(annotations), "body": body, "parameter_count": count,
            "signature": signature})
        return len(self.declarations) - 1

    # Skipping.

    def semicolon(self, index, end):
        """Index of the next ";" of this level, skipping balanced groups; None when there is none."""
        while index < end:
            value = self.values[index]
            if value == ";":
                return index
            index = self.match[index] + 1 if value in _OPEN else index + 1
        return None

    def recover(self, index, end):
        """Past the end of an unrecognised construct: its ";" or its first balanced {...} group."""
        while index < end:
            value = self.values[index]
            if value == ";":
                return index + 1
            if value == "{":
                return self.match[index] + 1
            index = self.match[index] + 1 if value in _OPEN else index + 1
        return end

    def annotation(self, index, end):
        """(qualified name, index after it) of the annotation at "@"; its arguments are skipped, never kept."""
        parts, index = [], index + 1
        while index < end and self.name(index) is not None:
            parts.append(self.values[index])
            index += 1
            if self.value(index) == "." and self.name(index + 1) is not None:
                index += 1
            else:
                break
        if index < end and self.values[index] == "(":
            index = self.match[index] + 1
        return ".".join(parts), index

    def angles(self, index, end):
        """Index after the ">" closing the type parameters at index, or None."""
        depth = 0
        while index < end:
            value = self.values[index]
            if value == "@" and self.name(index + 1) is not None:
                index = self.annotation(index, end)[1]
                continue
            if value == "<":
                depth += 1
            elif value == ">":
                depth -= 1
                if depth == 0:
                    return index + 1
            elif value in ("{", ";", "=") or self.kinds[index] == "other":
                return None
            elif value in ("(", "["):
                index = self.match[index] + 1
                continue
            index += 1
        return None

    def modifiers(self, index, end):
        modifiers, annotations = [], []
        while index < end:
            value = self.values[index]
            if value == "@" and self.name(index + 1) is not None and self.values[index + 1] != "interface":
                name, index = self.annotation(index, end)
                annotations.append(name)
            elif self.kinds[index] == "name" and value in _MODIFIERS:
                modifiers.append(value)
                index += 1
            elif value == "non" and self.value(index + 1) == "-" and self.value(index + 2) == "sealed":
                modifiers.append("non-sealed")
                index += 3
            else:
                break
        return index, modifiers, annotations

    # Bodies that are never searched for declarations.

    def anonymous(self, index, end):
        """Whether "new" before index starts an anonymous class: new Type<...>(...) {."""
        depth = 0
        while index < end:
            value = self.values[index]
            if value == "@" and self.name(index + 1) is not None:
                index = self.annotation(index, end)[1]
                continue
            if value == "<":
                depth += 1
            elif value == ">":
                depth -= 1
            elif value == "(" and depth == 0:
                return self.value(self.match[index] + 1) == "{"
            elif value in _OPEN or value in (")", "]", "}", ";") or self.kinds[index] == "other":
                return False
            elif depth == 0 and self.kinds[index] != "name" and value != ".":
                return False
            index += 1
        return False

    def fold(self, index, end):
        """Count local and anonymous class bodies inside one member; they are never indexed on their own."""
        for position in range(index, end):
            if self.kinds[position] != "name":
                continue
            value, previous = self.values[position], self.value(position - 1)
            if previous in (".", "::"):
                continue
            if value in ("class", "interface", "enum") and self.name(position + 1) is not None:
                self.folded += 1
            elif value == "record" and self.name(position + 1) is not None and self.value(position + 2) in ("(", "<"):
                self.folded += 1
            elif value == "new" and self.anonymous(position + 1, end):
                self.folded += 1

    def enum_constants(self, index, end):
        """Past the constants of an enum body; constant bodies are anonymous classes and only counted."""
        while index < end:
            value = self.values[index]
            if value == ";":
                return index + 1
            if value in _OPEN:
                close = self.match[index]
                self.folded += value == "{"
                self.fold(index + 1, close)
                index = close + 1
                continue
            index += 1
        return end

    # Declarations.

    def type_start(self, index, end):
        if index >= end:
            return False
        value = self.values[index]
        if self.kinds[index] == "name" and value in ("class", "interface", "enum"):
            return self.name(index + 1) is not None
        if value == "@":
            return self.value(index + 1) == "interface" and self.name(index + 2) is not None
        return (value == "record" and self.kinds[index] == "name" and self.name(index + 1) is not None
                and self.value(index + 2) in ("(", "<"))

    def type_declaration(self, start, index, end, modifiers, annotations, owner):
        if self.values[index] == "@":
            kind, name_at = "annotation_type", index + 2
        else:
            kind, name_at = self.values[index], index + 1
        body = name_at + 1
        while body < end and self.values[body] != "{":
            if self.values[body] == ";" or self.kinds[body] == "other":
                self.note("unrecognized_member" if owner is not None else "unrecognized_top_level", start)
                return self.recover(start, end)
            body = self.match[body] + 1 if self.values[body] in ("(", "[") else body + 1
        if body >= end:
            self.note("unterminated_member", start)
            return end
        close = self.match[body]
        position = self.add(kind, self.values[name_at], start, name_at, close, owner, modifiers, annotations,
                            "block", None, None)
        self.type_body(body + 1, close, position)
        return close + 1

    def unit(self):
        index, end = 0, self.size
        while index < end:
            if self.values[index] == ";":
                index += 1
                continue
            start = index
            index, modifiers, annotations = self.modifiers(index, end)
            word = self.name(index)
            if word in ("package", "import"):
                close = self.semicolon(index, end)
                if close is None:
                    self.note("unterminated_member", start)
                    return
                index = close + 1
            elif self.type_start(index, end):
                index = self.type_declaration(start, index, end, modifiers, annotations, None)
            elif word == "module" or (word == "open" and self.name(index + 1) == "module"):
                index = self.recover(index, end)  # module-info directives declare no types or methods
            else:
                self.note("unrecognized_top_level", start)
                index = self.recover(start, end)

    def type_body(self, index, end, owner):
        owner_kind, owner_name = self.declarations[owner]["kind"], self.declarations[owner]["name"]
        if owner_kind == "enum":
            index = self.enum_constants(index, end)
        while index < end:
            if self.values[index] == ";":
                index += 1
                continue
            start = index
            index, modifiers, annotations = self.modifiers(index, end)
            if index >= end:
                self.note("unterminated_member", start)
                return
            if self.type_start(index, end):
                index = self.type_declaration(start, index, end, modifiers, annotations, owner)
            elif self.values[index] == "{":
                close = self.match[index]
                name = "<static-initializer>" if "static" in modifiers else "<initializer>"
                self.add("initializer", name, start, index, close, owner, modifiers, annotations, "block", None, None)
                self.fold(index + 1, close)
                index = close + 1
            else:
                index = self.member(start, index, end, modifiers, annotations, owner, owner_kind, owner_name)

    def member(self, start, index, end, modifiers, annotations, owner, owner_kind, owner_name):
        """A field, method or constructor of a type body; returns the index after it."""
        if self.values[index] == "<":
            index = self.angles(index, end)
            if index is None:
                self.note("unrecognized_member", start)
                return self.recover(start, end)
        header, depth = index, 0
        while index < end:
            kind, value = self.kinds[index], self.values[index]
            if kind == "other":
                break
            if value == "@" and self.name(index + 1) is not None:
                index = self.annotation(index, end)[1]
                continue
            if value == "<":
                depth += 1
            elif value == ">":
                depth -= 1
            elif value == "(":
                previous = self.name(index - 1)
                if depth == 0 and index > header and previous is not None and previous not in _NOT_NAMES:
                    return self.method(start, header, index - 1, index, end, modifiers, annotations, owner,
                                       owner_name)
                break
            elif value == "[":
                index = self.match[index] + 1
                continue
            elif value in ("=", ";"):
                close = index if value == ";" else self.semicolon(index, end)
                if close is None:
                    self.note("unterminated_member", start)
                    return end
                self.fold(index + 1, close)  # a field initializer: lambdas and anonymous classes stay inside
                return close + 1
            elif value == "{":
                if owner_kind == "record" and index == header + 1 and self.name(header) == owner_name:
                    close = self.match[index]  # compact canonical constructor: Name { ... }
                    self.add("constructor", owner_name, start, header, close, owner, modifiers, annotations,
                             "block", None, "compact")
                    self.fold(index + 1, close)
                    return close + 1
                break
            index += 1
        else:
            self.note("unterminated_member", start)
            return end
        self.note("unrecognized_member", start)
        return self.recover(start, end)

    def method(self, start, header, name_at, paren, end, modifiers, annotations, owner, owner_name):
        name = self.values[name_at]
        constructor = name_at == header  # no result type between the modifiers and the name
        if constructor and name != owner_name:
            self.note("unrecognized_member", start)
            return self.recover(start, end)
        close = self.match[paren]
        count, signature = self.parameters(paren + 1, close)
        index = close + 1
        while index < end:
            value = self.values[index]
            if value == "{":
                last, body = self.match[index], "block"
                self.fold(index + 1, last)
                break
            if value == ";":
                last, body = index, "none"
                break
            if value == "default":  # an annotation element value, braces included, is no body
                last, body = self.semicolon(index, end), "none"
                if last is None:
                    self.note("unterminated_member", start)
                    return end
                break
            if value == "@" and self.name(index + 1) is not None:
                index = self.annotation(index, end)[1]
                continue
            if value in ("(", "["):
                index = self.match[index] + 1
                continue
            if value in ("=", "->") or self.kinds[index] == "other":
                self.note("unrecognized_member", start)
                return self.recover(start, end)
            index += 1  # throws clause and old-style array dimensions
        else:
            self.note("unterminated_member", start)
            return end
        self.add("constructor" if constructor else "method", name, start, name_at, last, owner, modifiers,
                 annotations, body, count, signature)
        return last + 1

    def parameters(self, index, end):
        """(count, type text for a signature hash) of a formal parameter list; names and annotations dropped."""
        groups, current, depth = [], [], 0
        while index < end:
            value = self.values[index]
            if value == "@" and self.name(index + 1) is not None:
                index = self.annotation(index, end)[1]
                continue
            if value == "[":
                current.append("[]")
                index = self.match[index] + 1
                continue
            if value in _OPEN:
                index = self.match[index] + 1
                continue
            if value == "<":
                depth += 1
            elif value == ">":
                depth -= 1
            elif value == "," and depth == 0:
                groups.append(current)
                current = []
                index += 1
                continue
            if value is not None and value != "final":
                current.append(value)
            index += 1
        if current:
            groups.append(current)
        types = []
        for tokens in groups:
            if tokens and tokens[-1] == "this":
                continue  # a receiver parameter is not a formal parameter
            dimensions = []
            while tokens and tokens[-1] == "[]":
                dimensions.append(tokens.pop())  # int values[] is int[] values
            types.append(" ".join(tokens[:-1] if len(tokens) > 1 else tokens) + "".join(dimensions))
        return len(types), ",".join(types)


def _result(status, error, declarations, notes, folded):
    counts = {}
    for code, _ in notes:
        counts[code] = counts.get(code, 0) + 1
    ordered = sorted(notes, key=lambda item: (item[1], item[0]))
    return {"status": status, "error": error, "declarations": declarations,
            "diagnostics": [{"code": code, "line": line} for code, line in ordered[:MAX_DIAGNOSTICS]],
            "diagnostics_omitted": max(0, len(ordered) - MAX_DIAGNOSTICS),
            "diagnostic_counts": dict(sorted(counts.items())), "folded_bodies": folded}


def extract(text):
    """Declarations of one Java source text; see the module docstring for what is and is not claimed."""
    if type(text) is not str:
        raise ValueError("Java source must be text")
    try:
        starts = _line_starts(text)
        _unicode_guard(text, starts)
        tokens, notes = _lex(text, starts)
        parser = _Parser(tokens, _balance(tokens))
        parser.unit()
    except _Failure as failure:
        return _result("parse_failed", {"type": failure.kind, "line": failure.line}, [], [], 0)
    except RecursionError:
        return _result("parse_failed", {"type": "NestingTooDeep", "line": None}, [], [], 0)
    except (IndexError, KeyError):  # an unforeseen token layout: an explicit failure, never a partial guess
        return _result("parse_failed", {"type": "ExtractorError", "line": None}, [], [], 0)
    notes = notes + parser.notes
    return _result("partial" if notes else "indexed", None, parser.declarations, notes, parser.folded)
