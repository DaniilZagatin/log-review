"""UI template text is source evidence; the ingestion never renders or runs it."""
from pathlib import Path
import re
import tempfile
import unittest

from process_observability.source_tools import SourceError, snapshot_repository
from process_observability.report import build_html


class TemplateSourceInputs(unittest.TestCase):
    def test_template_script_is_escaped_in_the_generated_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            text = '</script><img src=x onerror=alert(1)>\n'
            (root / 'action.html').write_text(text, encoding='utf-8')
            snapshot = snapshot_repository(root, max_files=None, max_chars=None)
            evidence = snapshot.evidence({'path': 'action.html', 'start_line': 1,
                                          'end_line': 1, 'symbol': '<template>'})
            page = build_html({'report_sources': {'excerpts': [evidence.to_dict()], 'unavailable': []}})
            self.assertIn('&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;', page)
            self.assertNotIn(text.strip(), page)
            plain = build_html({'report_sources': {'excerpts': [], 'unavailable': []}})
            self.assertEqual(len(re.findall(r'</script\b', page, re.I)),
                             len(re.findall(r'</script\b', plain, re.I)))

    def test_unreadable_or_oversized_templates_abort_instead_of_disappearing(self):
        for content, limit in ((b'\xff\xfe', 512000), (b'<p>too long</p>', 3)):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                (root / 'action.html').write_bytes(content)
                with self.assertRaises(SourceError):
                    snapshot_repository(root, max_files=None, max_chars=None, max_file_bytes=limit)

    def test_html_and_htm_are_read_as_literal_source_with_exact_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            template = ('<form method="post" action="/cases/42/close/">\n'
                        '<button type="submit">Close case</button>\n'
                        '<script>throw new Error("TARGET MUST NEVER RUN");</script>\n'
                        '</form>\n')
            for name in ('action.html', 'action.HTM'):
                (root / name).write_text(template, encoding='utf-8')
            snapshot = snapshot_repository(root, max_files=None, max_chars=None)
            self.assertEqual(set(snapshot.files), {'action.html', 'action.HTM'})
            for name in snapshot.files:
                self.assertEqual(snapshot.files[name].text, template)
                evidence = snapshot.evidence({'path': name, 'start_line': 1, 'end_line': 3, 'symbol': '<template>'})
                self.assertIn('TARGET MUST NEVER RUN', evidence.safe_quote)
                self.assertIn('Close case', evidence.safe_quote)
            self.assertFalse(snapshot.scope['partial_input'])

    def test_secret_and_symlink_policy_still_applies_to_templates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / 'safe.html').write_text('<button>Submit</button>\n', encoding='utf-8')
            (root / '.env.html').write_text('secret placeholder\n', encoding='utf-8')
            (root / 'alias.html').symlink_to(root / 'safe.html')
            snapshot = snapshot_repository(root, max_files=None, max_chars=None)
            self.assertEqual(set(snapshot.files), {'safe.html'})
            self.assertTrue(any(x['path'] == '.env.html' and x['reason'] == 'secret'
                                for x in snapshot.scope['exclusions']))
            self.assertTrue(any(x['path'] == 'alias.html' and x['reason'] == 'symlink'
                                for x in snapshot.scope['exclusions']))


if __name__ == '__main__':
    unittest.main()
