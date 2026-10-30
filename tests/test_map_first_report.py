"""CLI, independent export checks and source-text HTML safety (all offline)."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from process_observability.cli import main
from process_observability.logging_report import build_html, export_logging, validate_report, verify_export, write_report
from test_map_first_logging import CODE, Scripted, model_reply, operation
from process_observability.logging_review import review_logging
from process_observability.map_input import MapView, load_hierarchy
from process_observability.source_tools import snapshot_repository


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.repo = self.root/'repo'; self.repo.mkdir()
        (self.repo/'app.py').write_text(CODE)
        self.map = {'schema_version': '2.0', 'scenario': {'title': 'Обращения'}, 'sources': [{'id': 'source-1'}],
                    'nodes': [operation()], 'edges': [], 'run': {'status': 'completed', 'id': 'fixture'}}
        self.mapfile = self.root/'scenario_map.json'; self.mapfile.write_text(json.dumps(self.map, ensure_ascii=False, indent=2))

    def bundle(self):
        return review_logging(MapView.from_dict(self.map), snapshot_repository(self.repo, max_files=None, max_chars=None),
                              Scripted(lambda p, n: model_reply(p)))

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err): result = main(args)
        return result, out.getvalue(), err.getvalue()

    def test_normal_cli_uses_only_logging_model_and_preserves_map_bytes(self):
        before = self.mapfile.read_bytes()
        config = self.root/'profile.json'
        config.write_text(json.dumps({'business': {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture'},
                                      'code': {'unused': 'The code role must not be constructed or read for TLS'}}))
        fake = Scripted(lambda p, n: model_reply(p))
        output = self.root/'review'
        with patch('process_observability.llm.OpenAICompatibleModel', return_value=fake), \
             patch('process_observability.workflow.run_analysis', side_effect=AssertionError('No legacy analysis allowed')):
            code, out, err = self.invoke(['review-logging', '--map', str(self.mapfile), '--repo', str(self.repo),
                                          '--config', str(config), '--out', str(output)])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.mapfile.read_bytes(), before)
        data = json.loads((output/'logging_review.json').read_text())
        self.assertEqual(data['map_sha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual(data['summary']['counts']['C'], 1)
        self.assertTrue(verify_export(output))
        self.assertEqual({p.name for p in output.iterdir()}, {'logging_review.json', 'report.html', 'operations.csv',
                         'logging_changes.csv', 'SHA256SUMS', 'SOURCE-ARTIFACTS.json'})

    def test_inventory_only_never_constructs_a_model(self):
        with patch('process_observability.llm.OpenAICompatibleModel', side_effect=AssertionError('No model')):
            code, out, err = self.invoke(['review-logging', '--map', str(self.mapfile), '--repo', str(self.repo),
                                          '--inventory-only', '--out', str(self.root/'inventory')])
        self.assertEqual(code, 1, err)
        self.assertEqual(json.loads(out)['summary']['counts']['X'], 1)

    def test_new_report_has_only_three_sections_and_source_is_inert(self):
        data = self.bundle()
        data['operations'][0]['bindings'][0]['meaning'] = '</script><img src=x onerror=alert(1)>'
        output = build_html(data)
        # Migrated (Q3 renderer reuse): the page now runs the first agent's renderer and the report shell as two
        # executable scripts and carries the map/overlay as JSON data blocks, so the former single-script count no
        # longer describes the injection boundary. Every script element must still close exactly once, the hostile
        # text stays escaped data, and only the two executable scripts exist.
        self.assertEqual(output.count('<script>'), 2)
        self.assertEqual(output.count('</script>'), output.count('<script'))
        self.assertNotIn('<img src=x', output)
        self.assertIn('&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;', output)
        for name in ('data-view="map"', 'data-view="logging"', 'data-view="improvements"'): self.assertIn(name, output)
        self.assertNotIn('data-view="metrics"', output)
        self.assertIn("script-src &#x27;sha256-", output)
        self.assertIn('Все операции', output)
        self.assertNotIn('applicability', output)

    def test_export_checksums_and_source_hashes_and_no_second_model(self):
        first, second = self.root/'first', self.root/'second'
        write_report(self.bundle(), first)
        original = (first/'logging_review.json').read_bytes()
        with patch('process_observability.llm.OpenAICompatibleModel', side_effect=AssertionError('No model')):
            export_logging(first, second)
        self.assertEqual(original, (first/'logging_review.json').read_bytes())
        self.assertTrue(verify_export(second))
        self.assertEqual(json.loads((second/'SOURCE-ARTIFACTS.json').read_text())['logging_review.json'], hashlib.sha256(original).hexdigest())
        (second/'operations.csv').write_text('modified')
        with self.assertRaises(ValueError): verify_export(second)

    def test_saved_model_percent_is_not_an_authoritative_export(self):
        data = self.bundle(); data['summary']['uncovered']['lower'] = 73
        with self.assertRaises(ValueError): validate_report(data)

    def test_output_is_never_overwritten_or_written_inside_the_target(self):
        before = self.mapfile.read_bytes()
        for output in (self.repo/'out', self.root):
            code, _, _ = self.invoke(['review-logging', '--map', str(self.mapfile), '--repo', str(self.repo),
                                     '--inventory-only', '--out', str(output)])
            self.assertEqual(code, 2)
        self.assertEqual(before, self.mapfile.read_bytes())

    def test_actual_membership_hierarchy_is_used_and_wrong_revision_is_disabled(self):
        view = MapView.from_dict(self.map)
        hierarchy = {'hierarchy_version': '1', 'map_revision': view.revision,
                     'stages': [{'id': 'stage-1', 'name': 'Обращения', 'collapsible': True}],
                     'membership': {'save': 'stage-1'}}
        p = self.root/'map_hierarchy.json'; p.write_text(json.dumps(hierarchy))
        loaded, warnings = load_hierarchy(p, view)
        self.assertEqual(loaded['stages'][0]['operation_ids'], ['save'])
        self.assertFalse(warnings)
        hierarchy['map_revision'] = 'foreign'; p.write_text(json.dumps(hierarchy))
        self.assertIsNone(load_hierarchy(p, view)[0])

    def test_http429_retries_cannot_escape_the_physical_request_cap(self):
        from process_observability.llm import TransportResponse
        config = self.root/'profile.json'
        config.write_text(json.dumps({'base_url': 'http://fixture.invalid/v1', 'model': 'fixture', 'rate_limit_retries': 2}))
        response = TransportResponse(429, b'{"error":"synthetic rate limit"}')
        with patch('process_observability.llm.urllib_transport', return_value=response) as transport, \
             patch('process_observability.llm.time.sleep'):
            code, out, err = self.invoke(['review-logging', '--map', str(self.mapfile), '--repo', str(self.repo),
                                         '--config', str(config), '--max-requests', '1', '--out', str(self.root/'rate-limited')])
        self.assertEqual(code, 1, err)
        self.assertEqual(transport.call_count, 1)
        saved = json.loads((self.root/'rate-limited/logging_review.json').read_text())
        self.assertEqual(len(saved['transport_attempts']), 1)
        self.assertEqual(saved['operations'][0]['state'], 'X')


if __name__ == '__main__': unittest.main()
