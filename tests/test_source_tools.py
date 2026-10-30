import json
from pathlib import Path
import tempfile
import unittest

from process_observability.contracts import ValidationState
from process_observability.redaction import redact_text
from process_observability.source_tools import SourceError, covering_ranges, snapshot_repository

XML_CANARY = "SYNTHETIC-XML-SECRET-71f3"
# Ordinary logging and configuration XML: none of it is a credential, so it must stay readable.
ORDINARY_XML = (
    '<configuration scan="true">\n'
    '  <property name="LOG_PATTERN" value="%d{ISO8601} [%X{traceId}] %-5level %logger - %msg%n"/>\n'
    '  <appender name="JSON" class="ch.qos.logback.core.ConsoleAppender">\n'
    '    <encoder class="net.logstash.logback.encoder.LogstashEncoder"><includeMdcKeyName>traceId</includeMdcKeyName></encoder>\n'
    '  </appender>\n'
    '  <logger name="com.example.auth.token" level="DEBUG"/>\n'
    '  <logger name="com.example.orders" level="INFO" additivity="false"><appender-ref ref="JSON"/></logger>\n'
    '  <root level="INFO"><appender-ref ref="JSON"/></root>\n'
    '</configuration>\n'
    '<beans>\n'
    '  <property name="tokenValiditySeconds" value="3600"/>\n'
    '  <property name="passwordEncoder" ref="encoder"/>\n'
    '  <property name="username" value="orders_app"/>\n'
    '  <property name="url" value="jdbc:postgresql://db:5432/orders"/>\n'
    '  <property><name>fs.defaultFS</name><value>hdfs://namenode:8020</value></property>\n'
    '  <authorization><allow users="*"/></authorization>\n'
    '  <input type="password" name="password" value=""/>\n'
    '  <password/>\n'
    '</beans>\n')


class SourceToolsTests(unittest.TestCase):
    def test_redaction_handles_quoted_spaces_escapes_headers_urls_and_pem(self):
        samples = ['{"api_key": "CANARY secret with spaces"}', "password='CANARY secret'",
                   'Authorization: Bearer CANARY_SECRET', '{"Authorization": "Bearer CANARY_SECRET"}',
                   'url="https://user:CANARY_SECRET@host/a?token=CANARY_QUERY"',
                   'secret = """CANARY one\nCANARY two"""',
                   'x="-----BEGIN PRIVATE KEY-----\nCANARY_KEY\n-----END PRIVATE KEY-----"',
                   '{"token": "CANARY \\"more text"}']
        for raw in samples:
            with self.subTest(raw_type=raw.split('=')[0]):
                safe = redact_text(raw)
                self.assertNotIn("CANARY", safe)
                self.assertEqual(safe.count("\n"), raw.count("\n"))
        self.assertEqual(redact_text('logger.info("order.completed")'), 'logger.info("order.completed")')

    def test_xml_credential_elements_and_named_properties_are_masked(self):
        samples = {
            "named property": f'<property name="password" value="{XML_CANARY}"/>',
            "credential element": f"<password>{XML_CANARY}</password>",
            "value before name, single quotes": f"<property value='{XML_CANARY}' name='db.password' />",
            "namespaced, mixed case": f'<beans:Property Name="Spring.Datasource.Password" Value="{XML_CANARY}"/>',
            "namespaced element with attribute": f'<db:Password encrypted="false">{XML_CANARY}</db:Password>',
            "CDATA and comment": f"<clientSecret><!-- rotated --><![CDATA[{XML_CANARY}]]></clientSecret>",
            "multiline attribute": f'<add key="ApiKey"\n     value="{XML_CANARY}\n{XML_CANARY}"/>',
            "multiline element": f"<password>\n  {XML_CANARY}\n  {XML_CANARY}\n</password>",
            "properties entry": f'<entry key="jdbc.password">{XML_CANARY}</entry>',
            "nested value": f'<property name="password">\n  <value>{XML_CANARY}</value>\n</property>',
            "props": f'<props><prop key="access_token">{XML_CANARY}</prop></props>',
            "name/value elements": f"<property>\n  <name>fs.s3a.secret.key</name>\n  <value>{XML_CANARY}</value>\n</property>",
            "param elements": f"<context-param><param-name>dbPassword</param-name>"
                              f"<param-value>{XML_CANARY}</param-value></context-param>",
        }
        for label, raw in samples.items():
            with self.subTest(label):
                safe = redact_text(raw)
                self.assertNotIn(XML_CANARY, safe)
                self.assertIn("[REDACTED]", safe)
                self.assertEqual(safe.count("\n"), raw.count("\n"))
        self.assertEqual(redact_text(samples["named property"]), '<property name="password" value="[REDACTED]"/>')
        self.assertEqual(redact_text(ORDINARY_XML), ORDINARY_XML)

    def test_xml_credentials_stay_out_of_context_and_evidence_slices(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "applicationContext.xml").write_text(
                '<beans>\n'
                '  <bean id="dataSource" class="org.example.DataSource">\n'
                f'    <property name="password" value="{XML_CANARY}"/>\n'
                '    <property name="url" value="jdbc:postgresql://db/orders"/>\n'
                '  </bean>\n'
                '  <password>\n'
                f'    {XML_CANARY}-line-one\n'
                f'    {XML_CANARY}-line-two\n'
                '  </password>\n'
                '</beans>\n', encoding="utf-8")
            (root / "logback.xml").write_text(ORDINARY_XML, encoding="utf-8", newline="\n")
            snapshot = snapshot_repository(root)
            self.assertNotIn(XML_CANARY, json.dumps(snapshot.to_context()))
            self.assertEqual(snapshot.files["logback.xml"].text, ORDINARY_XML)
            self.assertEqual(len(snapshot.files["applicationContext.xml"].text.splitlines()), 10)
            # A range inside the multiline credential shows only the masked lines.
            inside = snapshot.evidence(dict(path="applicationContext.xml", start_line=7, end_line=8))
            self.assertNotIn(XML_CANARY, inside.safe_quote or "")
            self.assertTrue(inside.raw_span_hash)
            whole = snapshot.evidence(dict(path="applicationContext.xml", start_line=1, end_line=10))
            self.assertNotIn(XML_CANARY, whole.safe_quote)
            self.assertIn('<property name="password" value="[REDACTED]"/>', whole.safe_quote)
            self.assertIn('<property name="url" value="jdbc:postgresql://db/orders"/>', whole.safe_quote)
            self.assertEqual(whole.safe_quote.splitlines()[9], "</beans>")

    def test_snapshot_is_frozen_and_source_application_is_never_executed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "app.py"
            source.write_text('raise RuntimeError("MUST_NOT_RUN")\nlogger.info("order.completed")\npassword="CANARY"\n')
            (root / ".env.json").write_text('{"api_key":"CANARY"}')
            (root / "secret.pem").write_text("CANARY")
            snapshot = snapshot_repository(root)
            self.assertEqual(list(snapshot.files), ["app.py"])
            self.assertNotIn("CANARY", json.dumps(snapshot.to_context()))
            self.assertEqual(snapshot.to_context()["candidates"][0]["event_key"], "order.completed")
            before = snapshot.fingerprints.copy()
            source.write_text("changed")
            evidence = snapshot.evidence(dict(path="app.py", start_line=2, end_line=3, symbol="<module>"))
            self.assertEqual(snapshot.fingerprints, before)
            self.assertEqual(evidence.text_validation, ValidationState.VERIFIED)
            self.assertEqual(evidence.semantic_validation, ValidationState.UNVERIFIED)
            self.assertIn("order.completed", evidence.safe_quote)
            self.assertNotIn("CANARY", evidence.safe_quote)
            for ref in [dict(path="../outside", start_line=1, end_line=1), dict(path="app.py", start_line=False, end_line=1),
                        dict(path="app.py", start_line=1, end_line=999)]:
                with self.assertRaises(SourceError):
                    snapshot.evidence(ref)

    def test_partial_requires_explicit_option(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.py").write_text("first=1\n", encoding="utf-8")
            (root / "b.py").write_text("second=2\n", encoding="utf-8")
            with self.assertRaises(SourceError):
                snapshot_repository(root, max_files=1)
            snapshot = snapshot_repository(root, max_files=1, allow_partial=True)
            self.assertTrue(snapshot.scope["partial_input"])
            self.assertEqual(snapshot.scope["eligible_files"], 2)

    def test_snapshot_does_not_read_symlinks(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as outside:
            root = Path(temp)
            (root / "a.py").write_text("first=1\n", encoding="utf-8")
            secret = Path(outside) / "external.py"
            secret.write_text('password="CANARY"', encoding="utf-8")
            try:
                (root / "link.py").symlink_to(secret)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows account lacks symbolic-link creation privilege")
                raise
            snapshot = snapshot_repository(root)
            self.assertNotIn("link.py", snapshot.files)
            self.assertEqual(snapshot.scope["eligible_files"], 1)

    def test_quote_inside_private_key_block_is_redacted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);(root / "app.py").write_text('key="-----BEGIN PRIVATE KEY-----\nCANARY_BODY\n-----END PRIVATE KEY-----"\n')
            evidence = snapshot_repository(root).evidence(dict(path="app.py", start_line=2, end_line=2))
            self.assertNotIn("CANARY", evidence.safe_quote or "")

    def test_range_cover_joins_touching_or_overlapping_ranges_of_one_path_and_never_a_missing_line(self):
        def span(path, first, last, name):
            return {"path": path, "start_line": first, "end_line": last, "id": name}

        def names(ranges, path, first, last):
            found = covering_ranges(ranges, path, first, last)
            return None if found is None else [item["id"] for item in found]

        touching = [span("app.py", 1, 2, "a"), span("app.py", 3, 3, "b")]
        self.assertEqual(names(touching, "app.py", 1, 3), ["a", "b"])
        self.assertEqual(names([span("app.py", 1, 2, "a"), span("app.py", 2, 3, "b")], "app.py", 1, 3), ["a", "b"])
        # Input order is kept; ranges of other paths or without a cited line are not contributors.
        mixed = [span("app.py", 3, 5, "b"), span("other.py", 1, 9, "x"), span("app.py", 1, 2, "a"),
                 span("app.py", 9, 9, "far")]
        self.assertEqual(names(mixed, "app.py", 2, 4), ["b", "a"])
        self.assertEqual(names([span("app.py", 1, 10, "a")], "app.py", 4, 6), ["a"])
        rejected = {
            "missing line": ([span("app.py", 1, 1, "a"), span("app.py", 3, 3, "b")], "app.py", 1, 3),
            "missing tail": (touching, "app.py", 1, 4),
            "missing head": (touching, "app.py", 0, 2),
            "other path": (touching, "other.py", 1, 3),
            "other path fills the gap": ([span("app.py", 1, 1, "a"), span("other.py", 2, 2, "x"),
                                          span("app.py", 3, 3, "b")], "app.py", 1, 3),
            "reversed range": (touching, "app.py", 3, 2),
            "boolean line": (touching, "app.py", True, 2),
            "nothing supplied": ([], "app.py", 1, 1),
        }
        for label, (ranges, path, first, last) in rejected.items():
            with self.subTest(label):
                self.assertIsNone(covering_ranges(ranges, path, first, last))

    def test_covered_range_still_meets_the_snapshot_line_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);(root / "app.py").write_text("".join(f"x_{n} = {n}\n" for n in range(1, 101)))
            snapshot = snapshot_repository(root)
            ranges = [{"path": "app.py", "start_line": 1, "end_line": 50}, {"path": "app.py", "start_line": 51, "end_line": 100}]
            self.assertIsNotNone(covering_ranges(ranges, "app.py", 1, 81))
            # Coverage never widens the frozen-text contract: more than 80 lines is still refused.
            with self.assertRaises(SourceError):
                snapshot.evidence({"path": "app.py", "start_line": 1, "end_line": 81})
            self.assertEqual(snapshot.evidence({"path": "app.py", "start_line": 40, "end_line": 60}).locator, "app.py:40-60")

    def test_trusted_citation_budget_accepts_full_long_text_with_original_hash_and_keeps_bounds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            content = "".join(f"value_{i} = {i}\n" for i in range(1200))
            (root / "app.py").write_text(content)
            snapshot = snapshot_repository(root)
            ref = {"path": "app.py", "start_line": 1, "end_line": 630}
            with self.assertRaises(SourceError):
                snapshot.evidence(ref)  # legacy callers retain their 80-line default
            evidence = snapshot.evidence(ref, max_lines=1000)
            self.assertEqual(evidence.safe_quote, "\n".join(content.splitlines()[:630]))
            self.assertEqual(evidence.source_anchor.end_line, 630)
            for bad in (0, True, 1001):
                with self.subTest(limit=bad), self.assertRaises(SourceError):
                    snapshot.evidence(ref, max_lines=bad)
            with self.assertRaises(SourceError):
                snapshot.evidence({**ref, "end_line": 1001}, max_lines=1000)


if __name__ == "__main__":
    unittest.main()
