"""Unicode escapes in Java source (JLS 3.2/3.3) and the conservative declaration index.

Tests-first, synthetic Java only, nothing compiled or run. Java translates every eligible \\uXXXX escape (a backslash
preceded by an even number of backslashes, one or more u, four hex digits) BEFORE comments and literals are
recognised, so an escape inside a comment, string, char or text block can end or open one and hide or reveal
declarations. The index does not translate escapes: an eligible escape that decodes to a line terminator, a quote,
a backslash, "*" or "/" (or a malformed one, which javac rejects) makes the whole file parse_failed with no
declarations. An escape that decodes to any other character inside a literal or comment cannot change where
comments and literals end and keeps the file indexed; outside literals the existing failure stays.
"""

import hashlib
import json
import unittest

from process_observability.redaction import redact_text
from process_observability.source_tools import RepoSnapshot, SourceFile

BACKSLASH = "\\"


def api():
    from process_observability import declaration_index
    return declaration_index


def extract(text):
    from process_observability.java_declarations import extract as run
    return run(text)


def snapshot(files):
    return RepoSnapshot({path: SourceFile(path, hashlib.sha256(text.encode("utf-8")).hexdigest(), redact_text(text),
                                          text) for path, text in files.items()}, {})


def java(*lines, end="\n"):
    return end.join(lines) + end


def escape(digits, us="u"):
    """A raw source escape: one backslash, us, four hex digits (never translated by this test)."""
    return BACKSLASH + us + digits


class UnicodeGuardTests(unittest.TestCase):
    def test_the_reproduced_comment_escape_cannot_hide_a_method_behind_an_indexed_status(self):
        text = "class Box { // " + escape("000a") + " public void missed() {}\n}\n"
        self.assertEqual(text.count(BACKSLASH), 1)  # precondition: one raw backslash, no translated line break
        result = extract(text)
        self.assertEqual((result["status"], result["error"], result["declarations"]),
                         ("parse_failed", {"type": "StructuralUnicodeEscape", "line": 1}, []))
        index = api().build_declaration_index(snapshot({"box/Box.java": text}))
        entry = index["files"]["box/Box.java"]
        self.assertEqual((entry["status"], entry["parse"], entry["declarations"], entry["reason"]),
                         ("parse_failed", "failed", [], "StructuralUnicodeEscape"))
        self.assertEqual(index["declarations"], [])
        self.assertNotIn("missed", json.dumps(index, ensure_ascii=False))

    def test_structural_escapes_in_comments_literals_and_text_blocks_fail_the_file(self):
        cases = {
            "repeated u": ("class A { // " + escape("000a", "uuuu") + " void hidden() {}\n}", 1),
            "carriage return, upper hex": ("class A { // " + escape("000D") + " void hidden() {}\n}", 1),
            "quote in a string": ('class A {\n    String s = "' + escape("0022") + ' + x;\n}', 2),
            "apostrophe in a char": ("class A {\n    char c = '" + escape("0027") + "';\n}", 2),
            "backslash in a string": ('class A {\n    String s = "' + escape("005c") + '";\n}', 2),
            "triple quote in a text block": ('class A {\n    String t = """\n        ' + escape("0022") * 3
                                             + ' void hidden() {}\n        """;\n}', 3),
            "comment end": ("class A {\n    /* " + escape("002a") + "/ void hidden() {} /* */\n}", 2),
            "slash after star": ("class A {\n    /* *" + escape("002F") + " void hidden() {} /* */\n}", 2),
        }
        for label, (text, line) in cases.items():
            with self.subTest(case=label):
                result = extract(text + "\n")
                self.assertEqual((result["status"], result["declarations"]), ("parse_failed", []))
                self.assertEqual(result["error"], {"type": "StructuralUnicodeEscape", "line": line})

    def test_malformed_escapes_fail_like_javac_and_line_numbers_follow_the_snapshot(self):
        for label, text, line in (
                ("not hex in a comment", java("class A {", "    // path " + BACKSLASH + "users here", "}"), 2),
                ("cut at the end", "class A {}\n// " + BACKSLASH + "u12", 2),
                ("crlf lines", java("class A {", "    void a() {}", "    // " + escape("000a"), "}", end="\r\n"), 3)):
            with self.subTest(case=label):
                result = extract(text)
                self.assertEqual(result["status"], "parse_failed")
                expected = "StructuralUnicodeEscape" if label == "crlf lines" else "MalformedUnicodeEscape"
                self.assertEqual(result["error"], {"type": expected, "line": line})

    def test_escaped_backslashes_and_harmless_escapes_keep_the_inventory(self):
        text = java(
            "class Labels {",                                                       # 1
            '    String raw = "' + BACKSLASH * 2 + 'u000a";',                       # 2 escaped backslash, then text u000a
            '    String odd = "' + BACKSLASH * 3 + 'u0041";',                       # 3 \\ then \u0041 (A): harmless
            '    String word = "caf' + escape("00e9") + '";',                       # 4 a letter inside a string
            "    // caf" + escape("00e9") + " " + escape("2014") + " stays a comment",  # 5 letter and dash in a comment
            "    char letter = '" + escape("0041") + "';",                          # 6
            "    String path() {",                                                  # 7
            '        return "C:' + BACKSLASH * 2 + 'users";',                       # 8 even backslashes: no escape
            "    }",                                                                # 9
            "}")                                                                    # 10
        result = extract(text)
        self.assertEqual((result["status"], result["error"]), ("indexed", None))
        self.assertEqual([(item["qualname"], item["start_line"], item["end_line"]) for item in result["declarations"]],
                         [("Labels", 1, 10), ("Labels.path", 7, 9)])

    def test_an_escape_outside_literals_keeps_its_explicit_failure(self):
        text = java("class Caf" + escape("00e9") + " {", "    void a() {}", "}")
        result = extract(text)
        self.assertEqual((result["status"], result["error"]),
                         ("parse_failed", {"type": "UnicodeEscapeOutsideLiteral", "line": 1}))

    def test_a_guarded_file_keeps_hash_safety_and_its_anchors_stay_tentative(self):
        text = java("class Vault {", '    String key = "RAW-SECRET-UNICODE-1";', "    // " + escape("000a")
                    + " void hidden() {}", "    void open() {", "    }", "}")
        built = snapshot({"vault/Vault.java": text, "tool.py": "def helper():\n    return '\\u000a'\n"})
        index = api().build_declaration_index(built)
        self.assertEqual(index["files"]["vault/Vault.java"]["error"], {"type": "StructuralUnicodeEscape", "line": 3})
        self.assertEqual(index["files"]["tool.py"]["status"], "indexed")  # Python escapes are Python's own business
        self.assertNotIn("RAW-SECRET-UNICODE-1", json.dumps(index, ensure_ascii=False))
        self.assertEqual(api().check_index(index, built), index)
        source = built.files["vault/Vault.java"]
        catalog = {"snapshot_id": built.snapshot_id, "candidates": [
            {"id": "c-open", "path": "vault/Vault.java", "start_line": 4, "end_line": 5, "symbol": "Vault.open",
             "file_sha256": source.sha256, "finding_ids": ["f-open"], "findings": [{"id": "f-open"}]}],
            "context_anchors": []}
        [unit] = api().build_command_units(catalog, index)["units"]
        self.assertEqual((unit["status"], unit["reason"]), ("tentative", "parse_failed"))
        coverage = api().declaration_coverage(catalog, index)
        [row] = [item for item in coverage["open_scope"]["files"] if item["path"] == "vault/Vault.java"]
        self.assertEqual((row["language"], row["status"], row["blocking"]), ("java", "parse_failed", True))


if __name__ == "__main__":
    unittest.main()
