"""Static Java declarations in the business declaration index, and the declaration-coverage inventory.

Tests-first. Synthetic Java sources only, written here line by line so that every expected span is visible. The
index lexes the ORIGINAL frozen text (comments, strings, chars and text blocks masked, delimiters balanced); it never
compiles, imports or runs anything and stores no source text, literal or parser message. A Java declaration is a
navigation entry: neither the index nor the coverage inventory makes it a business operation, and a complete
declaration inventory is not complete business-candidate coverage. Python output stays exactly as before.
"""

import copy
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest

from process_observability.redaction import redact_text
from process_observability.source_tools import RepoSnapshot, SourceFile

ROOT = Path(__file__).resolve().parents[1]
LEGACY_FILE_KEYS = {"sha256", "lines", "status", "parse", "redacted_parse", "error", "declarations"}
LEGACY_DECLARATION_KEYS = {"id", "path", "file_sha256", "symbol", "qualname", "kind", "start_line", "end_line",
                           "definition_line", "parent_id", "span_sha256"}
INDEX_KEYS = {"schema", "snapshot_id", "files", "declarations", "calls", "basis", "index_sha256"}


def api():
    from process_observability import declaration_index
    return declaration_index


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def java(*lines):
    """One source text; line N of the file is lines[N - 1]."""
    return "\n".join(lines) + "\n"


def snapshot(files):
    return RepoSnapshot({path: SourceFile(path, sha(text), redact_text(text), text) for path, text in files.items()}, {})


def rows(index, path, *keys):
    keys = keys or ("qualname", "kind", "start_line", "definition_line", "end_line", "visibility")
    return [tuple(item[key] for key in keys) for item in index["files"][path]["declarations"]]


def by_name(index, path, qualname):
    return [item for item in index["files"][path]["declarations"] if item["qualname"] == qualname]


WIDGET_PATH = "sample/core/WidgetService.java"
WIDGET = java(
    "package sample.core;",                                                  # 1
    "",
    "import java.util.List;",                                                # 3
    "import java.util.function.Function;",
    "",
    "/** Service { brace in javadoc. void fakeDoc() {} */",                  # 6
    "@Deprecated",                                                           # 7
    '@SuppressWarnings({"unchecked",',
    '        "rawtypes"})',                                                  # 9
    "public final class WidgetService<T extends Comparable<T>>",             # 10
    "        implements Runnable {",
    '    private static final String LABEL = "{ not a block }";',            # 12
    "    private final List<T> items;",
    "",
    "    public WidgetService(List<T> items) {",                             # 15
    "        this.items = items;",
    "    }",                                                                 # 17
    "",
    "    WidgetService() {",                                                 # 19
    "        this(List.of());",
    "    }",                                                                 # 21
    "",
    "    @Override",                                                         # 23
    "    public void run() {",                                               # 24
    "        process(items.size());",
    "    }",                                                                 # 26
    "",
    "    protected int process(int count) {",                                # 28
    "        return normalize(count);",
    "    }",                                                                 # 30
    "",
    "    protected int process(String label) {",                             # 32
    "        return label.length();",
    "    }",                                                                 # 34
    "",
    "    private int normalize(int value) {",                                # 36
    "        return Math.max(0, value);",
    "    }",                                                                 # 38
    "",
    "    public static <R, E extends Exception> List<R> convert(",           # 40
    "            final List<? extends Number> source,",
    "            @Deprecated Function<? super Number, ? extends R> mapper)",
    "            throws E, IllegalStateException {",                         # 43
    "        return source.stream().map(mapper).toList();",
    "    }",                                                                 # 45
    "",
    "    int[] totals(int... values) {",                                     # 47
    "        return values;",
    "    }",                                                                 # 49
    "}")                                                                     # 50

SHAPES_PATH = "sample/core/Shape.java"
SHAPES = java(
    "package sample.core;",                                                  # 1
    "",
    "public interface Shape {",                                              # 3
    "    double area();",                                                    # 4
    "",
    "    default String describe() {",                                       # 6
    '        return "shape:" + area();',
    "    }",                                                                 # 8
    "",
    "    static Shape unit() {",                                             # 10
    "        return () -> 1.0;",
    "    }",                                                                 # 12
    "",
    "    private void check() {",                                            # 14
    "    }",                                                                 # 15
    "",
    "    enum Kind {",                                                       # 17
    '        ROUND("r") {',                                                  # 18
    "            @Override",
    "            String code() {",                                           # 20
    '                return "round";',
    "            }",
    "        },",                                                            # 23
    '        SQUARE("s");',                                                  # 24
    "",
    "        private final String tag;",                                     # 26
    "",
    "        Kind(String tag) {",                                            # 28
    "            this.tag = tag;",
    "        }",                                                             # 30
    "",
    "        String code() {",                                               # 32
    "            return tag;",
    "        }",                                                             # 34
    "    }",                                                                 # 35
    "",
    "    record Box(double width, double height) implements Shape {",        # 37
    "        public Box {",                                                  # 38
    "            if (width < 0) {",
    '                throw new IllegalArgumentException("width");',
    "            }",
    "        }",                                                             # 42
    "",
    "        @Override",                                                     # 44
    "        public double area() {",                                        # 45
    "            return width * height;",
    "        }",                                                             # 47
    "    }",                                                                 # 48
    "",
    "    @interface Marker {",                                               # 50
    '        String[] value() default {"a", "b"};',                          # 51
    "        int level() default 1;",                                        # 52
    "    }",                                                                 # 53
    "",
    "    class Holder {",                                                    # 55
    "        static class Inner {",                                          # 56
    "            void touch() {",                                            # 57
    "            }",                                                         # 58
    "        }",                                                             # 59
    "    }",                                                                 # 60
    "}")                                                                     # 61

TEMPLATES_PATH = "sample/text/Templates.java"
TEMPLATES = java(
    "package sample.text;",                                                  # 1
    "",
    "class Templates {",                                                     # 3
    "    // void commented() { }",                                           # 4
    "    /* class Hidden { void ghost() { } } */",                           # 5
    '    private static final String BLOCK = """',                           # 6
    "        public void fromTextBlock() {",                                 # 7
    '            "quoted \\""" still inside" }',                             # 8
    '        """;',                                                          # 9
    "    private static final char OPEN = '{';",                             # 10
    "    private static final char QUOTE = '\\'';",                          # 11
    '    private static final String S = "void fake() { \\" } {";',          # 12
    "",
    "    String render(String name) {",                                      # 14
    '        String local = "} }";  /* } */',
    "        return \"<\" + name + \"/>\" + '}' + local;",
    "    }",                                                                 # 17
    "",
    "    /**",                                                               # 19
    "     * Multi-line comment with code: int hidden() { return 1; }",
    "     */",
    "    int after() {",                                                     # 22
    "        return 2; // }",
    "    }",                                                                 # 24
    "}")                                                                     # 25

PIPELINE_PATH = "sample/flow/Pipeline.java"
PIPELINE = java(
    "package sample.flow;",                                                  # 1
    "",
    "import java.util.List;",
    "",
    "public class Pipeline {",                                               # 5
    "    private final Runnable hook = new Runnable() {",                    # 6
    "        @Override",
    "        public void run() {",                                           # 8
    "            helper(1);",
    "        }",
    "    };",                                                                # 11
    "    private final java.util.function.IntUnaryOperator twice = x -> {",  # 12
    "        return x * 2;",
    "    };",                                                                # 14
    "",
    "    static {",                                                          # 16
    '        System.setProperty("mode", "static");',
    "    }",                                                                 # 18
    "",
    "    {",                                                                 # 20
    "        helper(0);",
    "    }",                                                                 # 22
    "",
    "    void execute(List<String> items) {",                                # 24
    "        if (items.isEmpty()) {",
    "            return;",
    "        }",
    "        for (String item : items) {",                                   # 28
    "            while (item.isBlank()) {",
    "                item = item.trim();",
    "            }",
    "        }",
    "        try {",                                                         # 33
    "            items.forEach(item -> {",
    "                helper(item.length());",
    "            });",
    "        } catch (RuntimeException error) {",                            # 37
    "            helper(-1);",
    "        } finally {",
    "            synchronized (this) {",                                     # 40
    "                helper(2);",
    "            }",
    "        }",
    "        switch (items.size()) {",                                       # 44
    "            case 0: { helper(3); break; }",
    "            default: helper(4);",
    "        }",
    "        class Local {",                                                 # 48
    "            void inside() {",                                           # 49
    "                helper(5);",
    "            }",
    "        }",
    "        new Local().inside();",                                         # 53
    "        Runnable task = new Runnable() {",                              # 54
    "            public void run() {",                                       # 55
    "                helper(6);",
    "            }",
    "        };",
    "        task.run();",
    "    }",                                                                 # 60
    "",
    "    private void helper(int value) {",                                  # 62
    "        System.out.println(value);",
    "    }",                                                                 # 64
    "}")                                                                     # 65

BATCH_PATH = "sample/units/Batch.java"
BATCH = java(
    "package sample.units;",                                                 # 1
    "",
    "public class Batch {",                                                  # 3
    "    public void submit(int amount) {",                                  # 4
    "        check(amount);",                                                # 5
    "        store(amount);",                                                # 6
    "    }",                                                                 # 7
    "",
    "    private void check(int amount) {",                                  # 9
    "        if (amount < 0) {",                                             # 10
    '            throw new IllegalStateException("negative");',
    "        }",                                                             # 12
    "    }",                                                                 # 13
    "",
    "    private void store(int amount) {",                                  # 15
    "        System.out.println(amount);",
    "    }",                                                                 # 17
    "",
    "    void audit() {",                                                    # 19
    "    }",                                                                 # 20
    "",
    "    interface Hook {",                                                  # 22
    "        void fire();",                                                  # 23
    "    }",                                                                 # 24
    "}")                                                                     # 25


def anchor(built, identifier, path, first, last, symbol, role="candidate"):
    """A catalog anchor as build_command_units reads it (synthetic: no model, no review)."""
    return {"id": identifier, "role": role, "path": path, "start_line": first, "end_line": last, "symbol": symbol,
            "file_sha256": built.files[path].sha256, "finding_ids": [identifier + "-f"],
            "findings": [{"id": identifier + "-f", "description": "synthetic finding"}]}


class JavaDeclarationTests(unittest.TestCase):
    def test_exact_spans_overloads_constructors_and_private_helpers_are_indexed(self):
        built = snapshot({WIDGET_PATH: WIDGET})
        index = api().build_declaration_index(built)
        entry = index["files"][WIDGET_PATH]
        self.assertEqual((entry["status"], entry["parse"], entry["error"], entry["language"], entry["extractor"]),
                         ("indexed", "ok", None, "java", "java_lexical/1"))
        self.assertEqual((entry["diagnostics"], entry["calls"]), ([], "not_extracted"))
        self.assertEqual(entry["lines"], 50)
        self.assertEqual(rows(index, WIDGET_PATH, "qualname", "kind", "start_line", "definition_line", "end_line",
                              "visibility", "parameter_count"), [
            ("WidgetService", "class", 7, 10, 50, "public", None),
            ("WidgetService.WidgetService", "constructor", 15, 15, 17, "public", 1),
            ("WidgetService.WidgetService", "constructor", 19, 19, 21, "package", 0),
            ("WidgetService.run", "method", 23, 24, 26, "public", 0),
            ("WidgetService.process", "method", 28, 28, 30, "protected", 1),
            ("WidgetService.process", "method", 32, 32, 34, "protected", 1),
            ("WidgetService.normalize", "method", 36, 36, 38, "private", 1),   # a private helper without findings
            ("WidgetService.convert", "method", 40, 40, 45, "public", 2),      # generics, multiline, throws
            ("WidgetService.totals", "method", 47, 47, 49, "package", 1)])
        declarations = entry["declarations"]
        self.assertEqual(index["declarations"], declarations)
        lines = WIDGET.splitlines(keepends=True)
        [owner] = by_name(index, WIDGET_PATH, "WidgetService")
        for item in declarations:
            self.assertEqual((item["path"], item["file_sha256"], item["language"], item["body"]),
                             (WIDGET_PATH, built.files[WIDGET_PATH].sha256, "java", "block"))
            self.assertEqual(item["span_sha256"], sha("".join(lines[item["start_line"] - 1:item["end_line"]])))
            self.assertEqual(item["symbol"], item["qualname"])
            self.assertEqual(item["parent_id"], None if item is owner else owner["id"])
            self.assertTrue(LEGACY_DECLARATION_KEYS <= set(item))
        self.assertEqual(owner["annotations"], ["Deprecated", "SuppressWarnings"])
        self.assertEqual(by_name(index, WIDGET_PATH, "WidgetService.convert")[0]["modifiers"], ["public", "static"])
        self.assertEqual(by_name(index, WIDGET_PATH, "WidgetService.run")[0]["annotations"], ["Override"])
        # Overloads and both constructors are distinct entries with distinct IDs and signature hashes.
        for name in ("WidgetService.process", "WidgetService.WidgetService"):
            pair = by_name(index, WIDGET_PATH, name)
            self.assertEqual(len({item["id"] for item in pair}), 2)
            self.assertEqual(len({item["signature_sha256"] for item in pair}), 2)
        self.assertEqual(len({item["id"] for item in declarations}), len(declarations))

    def test_interfaces_enums_records_annotation_types_and_nested_classes(self):
        index = api().build_declaration_index(snapshot({SHAPES_PATH: SHAPES}))
        entry = index["files"][SHAPES_PATH]
        self.assertEqual((entry["status"], entry["parse"], entry["diagnostics"]), ("indexed", "ok", []))
        self.assertEqual(rows(index, SHAPES_PATH, "qualname", "kind", "start_line", "definition_line", "end_line",
                              "visibility", "body"), [
            ("Shape", "interface", 3, 3, 61, "public", "block"),
            ("Shape.area", "method", 4, 4, 4, "public", "none"),                  # implicitly public, abstract
            ("Shape.describe", "method", 6, 6, 8, "public", "block"),
            ("Shape.unit", "method", 10, 10, 12, "public", "block"),
            ("Shape.check", "method", 14, 14, 15, "private", "block"),
            ("Shape.Kind", "enum", 17, 17, 35, "public", "block"),
            ("Shape.Kind.Kind", "constructor", 28, 28, 30, "private", "block"),  # enum constructors are private
            ("Shape.Kind.code", "method", 32, 32, 34, "package", "block"),
            ("Shape.Box", "record", 37, 37, 48, "public", "block"),
            ("Shape.Box.Box", "constructor", 38, 38, 42, "public", "block"),      # compact canonical constructor
            ("Shape.Box.area", "method", 44, 45, 47, "public", "block"),
            ("Shape.Marker", "annotation_type", 50, 50, 53, "public", "block"),
            ("Shape.Marker.value", "method", 51, 51, 51, "public", "none"),       # element default {...} is no body
            ("Shape.Marker.level", "method", 52, 52, 52, "public", "none"),
            ("Shape.Holder", "class", 55, 55, 60, "public", "block"),
            ("Shape.Holder.Inner", "class", 56, 56, 59, "package", "block"),
            ("Shape.Holder.Inner.touch", "method", 57, 57, 58, "package", "block")])
        ids = {item["qualname"]: item["id"] for item in entry["declarations"]
               if item["qualname"] != "Shape.Kind.code" or item["start_line"] == 32}
        parents = {item["qualname"]: item["parent_id"] for item in entry["declarations"]}
        self.assertEqual((parents["Shape.Holder.Inner.touch"], parents["Shape.Holder.Inner"], parents["Shape.Holder"],
                          parents["Shape"]), (ids["Shape.Holder.Inner"], ids["Shape.Holder"], ids["Shape"], None))
        self.assertEqual(parents["Shape.Box.Box"], ids["Shape.Box"])
        # The overriding method in the body of the constant ROUND (line 20) belongs to that constant, not to Kind.
        self.assertEqual([item["start_line"] for item in by_name(index, SHAPES_PATH, "Shape.Kind.code")], [32])
        self.assertEqual(entry["folded_bodies"], 1)
        self.assertIsNone(by_name(index, SHAPES_PATH, "Shape.Box.Box")[0]["parameter_count"])

    def test_comments_strings_chars_and_text_blocks_hide_braces_and_fake_methods(self):
        index = api().build_declaration_index(snapshot({TEMPLATES_PATH: TEMPLATES}))
        entry = index["files"][TEMPLATES_PATH]
        self.assertEqual((entry["status"], entry["parse"], entry["diagnostics"]), ("indexed", "ok", []))
        self.assertEqual(rows(index, TEMPLATES_PATH), [("Templates", "class", 3, 3, 25, "package"),
                                                       ("Templates.render", "method", 14, 14, 17, "package"),
                                                       ("Templates.after", "method", 22, 22, 24, "package")])
        text = json.dumps(index, ensure_ascii=False)
        for hidden in ("commented", "Hidden", "ghost", "fromTextBlock", "quoted", "fake", "hidden", "not a block"):
            self.assertNotIn(hidden, text)

    def test_lambdas_calls_control_blocks_local_and_anonymous_classes_are_never_declarations(self):
        index = api().build_declaration_index(snapshot({PIPELINE_PATH: PIPELINE}))
        entry = index["files"][PIPELINE_PATH]
        self.assertEqual((entry["status"], entry["parse"], entry["diagnostics"]), ("indexed", "ok", []))
        self.assertEqual(rows(index, PIPELINE_PATH, "qualname", "kind", "start_line", "end_line", "body"), [
            ("Pipeline", "class", 5, 65, "block"),
            ("Pipeline.<static-initializer>", "initializer", 16, 18, "block"),
            ("Pipeline.<initializer>", "initializer", 20, 22, "block"),
            ("Pipeline.execute", "method", 24, 60, "block"),
            ("Pipeline.helper", "method", 62, 64, "block")])
        # Anonymous classes (lines 6 and 54) and the local class (line 48) stay inside their member's span; they
        # are counted, never indexed as members of Pipeline.
        self.assertEqual(entry["folded_bodies"], 3)
        self.assertEqual([call for call in index["calls"] if call["path"].endswith(".java")], [])
        for name in ("run", "inside", "Local", "if", "for", "while", "switch", "synchronized", "forEach", "println"):
            self.assertFalse([item for item in entry["declarations"] if item["qualname"].split(".")[-1] == name], name)


class JavaStatusTests(unittest.TestCase):
    def test_unbalanced_or_unterminated_input_is_an_explicit_failure_without_source_text(self):
        cases = {
            "unclosed brace": (java("class Broken {", "    void a() {", "        if (ready) {", "    }"),
                               "UnbalancedDelimiter", 2),
            "mismatched closer": (java("class Odd {", "    void a( {", "    }", "}"), "UnbalancedDelimiter", 4),
            "comment": (java("class Open {", "    /* never closed", "    void a() { }", "}"), "UnterminatedComment", 2),
            "string": (java("class Leak {", '    String key = "RAW-SECRET-JAVA-9;', "}"), "UnterminatedString", 2),
            "text block": (java("class Block {", '    String t = """', "        never closed", "}"),
                           "UnterminatedTextBlock", 2),
            "char": (java("class Ch {", "    char c = 'ab", "}"), "UnterminatedChar", 2),
            "unicode escape": (java("class U {", "    void a() \\u007B", "    }", "}"), "UnicodeEscapeOutsideLiteral", 2),
        }
        for label, (text, kind, line) in cases.items():
            with self.subTest(case=label):
                index = api().build_declaration_index(snapshot({"sample/bad/Bad.java": text}))
                entry = index["files"]["sample/bad/Bad.java"]
                self.assertEqual((entry["status"], entry["parse"], entry["declarations"], index["declarations"]),
                                 ("parse_failed", "failed", [], []))
                self.assertEqual((entry["error"], entry["reason"]), ({"type": kind, "line": line}, kind))
                saved = json.dumps(index, ensure_ascii=False)
                for secret in ("RAW-SECRET-JAVA-9", "never closed", "ready"):
                    self.assertNotIn(secret, saved)

    def test_unsupported_constructs_give_a_partial_status_with_bounded_diagnostics(self):
        top = java("void main() {", "    run();", "}", "", "class Valid {", "    void ok() {", "    }", "}")
        member = java("class Member {", "    helper();", "    void ok() {", "    }", "}")
        odd = java("class Hash {", "    # not java", "    void ok() {", "    }", "}")
        index = api().build_declaration_index(snapshot({"a/Top.java": top, "a/Member.java": member, "a/Hash.java": odd}))
        entry = index["files"]["a/Top.java"]
        self.assertEqual((entry["status"], entry["parse"], entry["error"]), ("partial", "partial", None))
        self.assertEqual(entry["diagnostics"], [{"code": "unrecognized_top_level", "line": 1}])
        self.assertEqual(entry["diagnostic_counts"], {"unrecognized_top_level": 1})
        self.assertEqual(rows(index, "a/Top.java", "qualname", "start_line", "end_line"),
                         [("Valid", 5, 8), ("Valid.ok", 6, 7)])
        entry = index["files"]["a/Member.java"]
        self.assertEqual((entry["status"], entry["diagnostics"]),
                         ("partial", [{"code": "unrecognized_member", "line": 2}]))
        self.assertEqual(rows(index, "a/Member.java", "qualname", "start_line", "end_line"),
                         [("Member", 1, 5), ("Member.ok", 3, 4)])
        entry = index["files"]["a/Hash.java"]
        self.assertEqual(entry["status"], "partial")
        self.assertIn("unexpected_character", entry["diagnostic_counts"])
        self.assertNotIn(2, [item["start_line"] for item in entry["declarations"]])  # no span begins at the bad line
        self.assertIn(("Hash", 1, 5), rows(index, "a/Hash.java", "qualname", "start_line", "end_line"))
        # Diagnostics are bounded and carry codes and lines only.
        many = java("class Many {", *["    stray();" for _ in range(30)], "}")
        entry = api().build_declaration_index(snapshot({"a/Many.java": many}))["files"]["a/Many.java"]
        self.assertEqual(entry["diagnostic_counts"], {"unrecognized_member": 30})
        self.assertEqual(len(entry["diagnostics"]), 20)
        self.assertEqual(entry["diagnostics_omitted"], 10)
        for item in entry["diagnostics"]:
            self.assertEqual(set(item), {"code", "line"})

    def test_anchors_in_failed_or_partial_java_stay_tentative_with_their_reason(self):
        top = java("void main() {", "    run();", "}", "", "class Valid {", "    void ok() {", "    }", "}")
        broken = java("class Broken {", "    void a() {", "}")
        built = snapshot({"a/Top.java": top, "a/Broken.java": broken})
        index = api().build_declaration_index(built)
        catalog = {"snapshot_id": built.snapshot_id, "candidates": [
            anchor(built, "outside", "a/Top.java", 2, 2, "main"), anchor(built, "inside", "a/Top.java", 6, 7, "Valid.ok"),
            anchor(built, "failed", "a/Broken.java", 2, 3, "Broken.a")], "context_anchors": []}
        units = {unit["candidate_ids"][0]: unit for unit in api().build_command_units(catalog, index)["units"]}
        self.assertEqual((units["outside"]["status"], units["outside"]["reason"]), ("tentative", "parse_partial"))
        self.assertEqual((units["inside"]["status"], units["inside"]["declaration"]["qualname"]), ("verified", "Valid.ok"))
        self.assertEqual((units["failed"]["status"], units["failed"]["reason"]), ("tentative", "parse_failed"))

    def test_every_java_type_kind_is_a_container_for_cross_member_anchors(self):
        text = java("enum Mode {", "    ON, OFF;", "    void first() {", "    }", "    void second() {", "    }", "}")
        built = snapshot({"a/Mode.java": text})
        index = api().build_declaration_index(built)
        catalog = {"snapshot_id": built.snapshot_id, "candidates": [anchor(built, "cross", "a/Mode.java", 3, 6, "Mode")],
                   "context_anchors": []}
        [unit] = api().build_command_units(catalog, index)["units"]
        self.assertEqual((unit["status"], unit["reason"], unit["declaration"]), ("tentative", "spans_declarations", None))


class JavaGuardTests(unittest.TestCase):
    def test_secrets_and_literals_are_not_serialized_and_guards_still_hold(self):
        text = java("class Settings {", '    private static final String PASSWORD = "RAW-SECRET-JAVA-1";',
                    '    String header = "Bearer RAW-SECRET-JAVA-2";', "    String password() {",
                    '        return "RAW-SECRET-JAVA-3";', "    }", "}")
        built = snapshot({"app/Settings.java": text})
        index = api().build_declaration_index(built)
        saved = json.dumps(index, ensure_ascii=False)
        for secret in ("RAW-SECRET-JAVA-1", "RAW-SECRET-JAVA-2", "RAW-SECRET-JAVA-3", "Bearer"):
            self.assertNotIn(secret, saved)
        self.assertEqual(rows(index, "app/Settings.java", "qualname", "start_line", "end_line"),
                         [("Settings", 1, 7), ("Settings.password", 4, 6)])
        module = api()
        # A changed original text behind an unchanged hash, an unsafe or a blocked path abort the whole index.
        source = built.files["app/Settings.java"]
        built.files["app/Settings.java"] = SourceFile(source.path, "0" * 64, source.text, source._raw_text)
        with self.assertRaises(module.DeclarationError):
            module.build_declaration_index(built)
        for path in ("../Escape.java", "/abs/Escape.java", "config/secrets/Keys.java"):
            with self.subTest(path=path), self.assertRaises(module.DeclarationError):
                module.build_declaration_index(snapshot({path: text}))
        # A forged Java span or file revision is refused even with a recomputed document hash.
        built = snapshot({WIDGET_PATH: WIDGET})
        index = module.build_index(built)
        forged = copy.deepcopy(index)
        forged["files"][WIDGET_PATH]["declarations"][3]["end_line"] = 25
        with self.assertRaises(module.DeclarationIndexError) as caught:
            module.check_index(module.seal(forged), built)
        self.assertEqual(caught.exception.code, "declaration_span_mismatch")
        forged = copy.deepcopy(index)
        forged["files"][WIDGET_PATH]["sha256"] = "1" * 64
        with self.assertRaises(module.DeclarationIndexError) as caught:
            module.check_index(module.seal(forged), built)
        self.assertEqual(caught.exception.code, "file_revision_mismatch")
        self.assertEqual(module.check_index(index, built), index)

    def test_the_extractor_only_lexes_text_and_never_imports_or_runs_anything(self):
        source = (ROOT / "src/process_observability/java_declarations.py").read_text(encoding="utf-8")
        for name in ("subprocess", "os.system", "importlib", "__import__", "socket", "urllib", "http.client",
                     "ctypes", "exec(", "eval(", "open(", "Popen"):
            self.assertNotIn(name, source)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        marker = Path(directory.name) / "ran"
        text = java("class Boot {", "    static {", f'        new java.io.File("{marker}").mkdir();', "    }",
                    "    public static void main(String[] args) throws Exception {",
                    f'        Runtime.getRuntime().exec("touch {marker}");', "    }", "}")
        index = api().build_declaration_index(snapshot({"app/Boot.java": text}))
        self.assertFalse(marker.exists())
        self.assertEqual(rows(index, "app/Boot.java", "qualname", "kind"),
                         [("Boot", "class"), ("Boot.<static-initializer>", "initializer"), ("Boot.main", "method")])

    def test_ids_are_deterministic_and_distinct_for_overloads_on_one_line(self):
        text = java("class O { void a() { } void a(int x) { } int a(String s) { return 1; } }")
        first = api().build_declaration_index(snapshot({"a/O.java": text}))
        second = api().build_declaration_index(snapshot({"a/O.java": text}))
        self.assertEqual(first, second)
        methods = [item for item in first["declarations"] if item["kind"] == "method"]
        self.assertEqual([(item["qualname"], item["start_line"], item["end_line"], item["parameter_count"])
                          for item in methods], [("O.a", 1, 1, 0), ("O.a", 1, 1, 1), ("O.a", 1, 1, 1)])
        self.assertEqual(len({item["id"] for item in methods}), 3)
        self.assertEqual(len({item["signature_sha256"] for item in methods}), 3)
        for item in first["declarations"]:
            self.assertRegex(item["id"], r"^decl-[0-9a-f]{24}$")
        built = snapshot({WIDGET_PATH: WIDGET})
        self.assertEqual(api().build_index(built)["index_sha256"], api().build_index(snapshot({WIDGET_PATH: WIDGET}))["index_sha256"])


class LanguageCompatibilityTests(unittest.TestCase):
    PYTHON = "class Basket:\n    def add(self):\n        helper()\n\n\ndef helper():\n    return 1\n"

    def test_other_languages_stay_explicitly_unsupported(self):
        files = {"app/Main.kt": "fun main() {}\n", "web/app.ts": "export function f() {}\n",
                 "svc/main.go": "package main\n\nfunc main() {}\n", "app/Program.cs": "class P { void M() {} }\n",
                 "app/Main.java": "class Main { void run() { } }\n", "tool.py": "def run():\n    return 1\n"}
        index = api().build_declaration_index(snapshot(files))
        for path in ("app/Main.kt", "web/app.ts", "svc/main.go", "app/Program.cs"):
            entry = index["files"][path]
            self.assertEqual(set(entry), LEGACY_FILE_KEYS)
            self.assertEqual((entry["status"], entry["parse"], entry["redacted_parse"], entry["declarations"]),
                             ("unsupported_language", "not_indexed", "not_indexed", []))
        self.assertEqual(index["files"]["app/Main.java"]["status"], "indexed")
        self.assertEqual(index["files"]["tool.py"]["status"], "indexed")

    def test_python_entries_ids_and_calls_are_unchanged_next_to_java(self):
        mixed = snapshot({"shop/basket.py": self.PYTHON, WIDGET_PATH: WIDGET})
        alone = snapshot({"shop/basket.py": self.PYTHON})
        indexes = [api().build_declaration_index(item) for item in (mixed, alone)]
        for built, index in zip((mixed, alone), indexes):
            self.assertEqual(set(index), INDEX_KEYS)
            self.assertEqual(index["schema"], "declaration-index/1")
            entry = index["files"]["shop/basket.py"]
            self.assertEqual(set(entry), LEGACY_FILE_KEYS)
            self.assertEqual((entry["status"], entry["parse"], entry["redacted_parse"], entry["error"]),
                             ("indexed", "ok", "ok", None))
            for item in entry["declarations"]:
                self.assertEqual(set(item), LEGACY_DECLARATION_KEYS)
                name = item["qualname"]
                self.assertEqual(item["id"], "decl-" + sha(f"{built.snapshot_id}:shop/basket.py:{item['start_line']}:"
                                                           f"{item['end_line']}:{name}")[:24])
            [call] = [call for call in index["calls"] if call["path"] == "shop/basket.py"]
            self.assertEqual((call["symbol"], call["status"]), ("helper", "resolved"))
        strip = lambda index: [{key: value for key, value in item.items() if key not in {"id", "parent_id"}}
                               for item in index["files"]["shop/basket.py"]["declarations"]]
        self.assertEqual(strip(indexes[0]), strip(indexes[1]))
        self.assertEqual([item["path"] for item in indexes[1]["declarations"]], ["shop/basket.py"] * 3)


class DeclarationCoverageTests(unittest.TestCase):
    def setUp(self):
        self.built = snapshot({BATCH_PATH: BATCH, "web/view.ts": "export function show() {}\n"})
        self.index = api().build_declaration_index(self.built)
        self.catalog = {"snapshot_id": self.built.snapshot_id, "catalog_sha256": "c" * 64, "candidates": [
            anchor(self.built, "c-submit", BATCH_PATH, 4, 7, "Batch.submit"),
            anchor(self.built, "c-cross", BATCH_PATH, 6, 10, "Batch.submit"),
            anchor(self.built, "c-view", "web/view.ts", 1, 1, "show")],
            "context_anchors": [anchor(self.built, "x-check", BATCH_PATH, 10, 12, "Batch.check", role="context")]}

    def test_units_bind_java_anchors_and_keep_every_candidate_id(self):
        record = api().build_command_units(self.catalog, self.index)
        units = {member: unit for unit in record["units"] for member in unit["member_ids"]}
        self.assertEqual((units["c-submit"]["status"], units["c-submit"]["declaration"]["qualname"],
                          units["c-submit"]["declaration"]["kind"]), ("verified", "Batch.submit", "method"))
        self.assertEqual((units["x-check"]["status"], units["x-check"]["role"], units["x-check"]["declaration"]["qualname"]),
                         ("verified", "context", "Batch.check"))
        self.assertEqual((units["c-cross"]["status"], units["c-cross"]["reason"]), ("tentative", "spans_declarations"))
        self.assertEqual((units["c-view"]["status"], units["c-view"]["reason"]), ("tentative", "not_indexed"))
        self.assertEqual(sorted(identifier for unit in record["units"] for identifier in unit["candidate_ids"]),
                         ["c-cross", "c-submit", "c-view"])
        self.assertEqual(len(record["units"]), 4)  # no unit is invented for an unanchored declaration
        declared = {unit["declaration"]["qualname"] for unit in record["units"] if unit["declaration"]}
        self.assertEqual(declared, {"Batch.submit", "Batch.check"})

    def test_declarations_without_anchors_are_reported_and_never_become_business_units(self):
        catalog, index = copy.deepcopy(self.catalog), copy.deepcopy(self.index)
        units_before = api().build_command_units(catalog, index)
        coverage = api().declaration_coverage(catalog, index)
        self.assertEqual((catalog, index), (self.catalog, self.index))           # inputs are not changed
        self.assertEqual(api().build_command_units(catalog, index), units_before)
        self.assertEqual(api().declaration_coverage(catalog, index), coverage)   # deterministic
        self.assertEqual((coverage["schema"], coverage["snapshot_id"], coverage["index_sha256"],
                          coverage["catalog_sha256"]),
                         ("declaration-coverage/1", self.built.snapshot_id, index["index_sha256"], "c" * 64))
        self.assertRegex(coverage["coverage_sha256"], r"^[0-9a-f]{64}$")
        unrepresented = [(row["qualname"], row["kind"], row["start_line"], row["end_line"], row["coverage"],
                          row["has_body"], row["callable"], row["visibility"]) for row in coverage["unrepresented"]]
        self.assertEqual(unrepresented, [
            ("Batch.store", "method", 15, 17, "none", True, True, "private"),
            ("Batch.audit", "method", 19, 20, "none", True, True, "package"),
            ("Batch.Hook", "interface", 22, 24, "none", True, False, "package"),
            ("Batch.Hook.fire", "method", 23, 23, "none", False, True, "public")])
        declarations = {item["qualname"]: item["id"] for item in index["declarations"]}
        for row in coverage["unrepresented"]:
            self.assertEqual(row["declaration_id"], declarations[row["qualname"]])
            self.assertEqual(row["path"], BATCH_PATH)
            for key in ("operation_id", "operation_ids", "candidate_id", "candidate_ids", "unit_id", "role",
                        "disposition"):
                self.assertNotIn(key, row)  # an inventory row is not a candidate, unit or operation
        summary = coverage["summary"]
        self.assertEqual((summary["declarations"], summary["callables"], summary["unrepresented"],
                          summary["unrepresented_callables_with_body"]), (7, 5, 4, 2))
        self.assertEqual(summary["by_coverage"], {"candidate": 1, "context": 1, "none": 4, "overlap_only": 1})
        self.assertEqual(coverage["unindexed_files"], [{"path": "web/view.ts", "status": "unsupported_language",
                                                        "parse": "not_indexed", "anchors": 1, "candidate_anchors": 1}])
        self.assertRegex(coverage["basis"], re.compile(r"not .*business", re.I))

    def test_the_review_catalog_path_binds_java_anchors_and_reports_the_unreviewed_helpers(self):
        from process_observability.action_catalog import build_catalog
        from process_observability.code_graph import build_graph
        from process_observability.log_discovery import discover_logging
        from process_observability.source_tools import snapshot_repository
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve() / "repo"
        (root / "sample/units").mkdir(parents=True)
        (root / BATCH_PATH).write_text(BATCH, encoding="utf-8")
        built = snapshot_repository(root, max_files=None, max_chars=None)
        inventory = discover_logging(built, max_sites=None, max_per_file=None, max_hints=None)
        review = {"findings": [{"id": "f-submit", "kind": "entry_point", "description": "Синтетическая команда.",
                                "task_id": "task-1", "task_kind": "base",
                                "refs": [{"path": BATCH_PATH, "start_line": 5, "end_line": 6,
                                          "symbol": "Batch.submit"}]}],
                  "unknowns": [], "complete": True, "snapshot_id": built.snapshot_id, "run_fingerprint": "rf-1"}
        catalog = build_catalog(review, built, graph=build_graph(built, inventory), inventory=inventory)
        index = api().build_index(built)
        [unit] = api().build_units(catalog, index, built)["units"]
        self.assertEqual((unit["status"], unit["declaration"]["qualname"], unit["declaration"]["start_line"],
                          unit["declaration"]["end_line"]), ("verified", "Batch.submit", 4, 7))
        self.assertEqual({member["symbol_check"] for member in unit["members"]}, {"consistent"})
        coverage = api().declaration_coverage(catalog, index)
        self.assertEqual([row["qualname"] for row in coverage["unrepresented"]],
                         ["Batch.check", "Batch.store", "Batch.audit", "Batch.Hook", "Batch.Hook.fire"])
        self.assertEqual(coverage["catalog_sha256"], catalog["catalog_sha256"])


if __name__ == "__main__":
    unittest.main()
