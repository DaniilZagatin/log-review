"""Source tools for one atomic loop. OWN strings only, never application execution."""
import hashlib
import unittest

from process_observability.atomic_sources import AtomicSources, SourceRequestError
from process_observability.source_tools import RepoSnapshot, SourceFile


def source(path, text, raw=None):
    return SourceFile(path, hashlib.sha256((raw or text).encode()).hexdigest(), text, raw or text)


class AtomicSourceTests(unittest.TestCase):
    def setUp(self):
        self.text = ('raise RuntimeError("fixture: do not execute")\n'
                     'def receive(item):\n'
                     '    result = inspect(item)\n'
                     '    return result\n'
                     '\n'
                     'def inspect(item):\n'
                     '    return item.valid\n')
        self.snapshot = RepoSnapshot({'pkg/flow.py': source('pkg/flow.py', self.text),
                                     'pkg/view.html': source('pkg/view.html', '<script>never_execute()</script>\n'),
                                     'pkg/data.py': source('pkg/data.py', 'password = "[REDACTED]"\n',
                                                           'password = "synthetic-secret-only"\n')}, {})
        self.units = [{'id': 'u-receive', 'path': 'pkg/flow.py', 'symbol': 'receive',
                       'start_line': 2, 'end_line': 4},
                      {'id': 'u-inspect', 'path': 'pkg/flow.py', 'symbol': 'inspect',
                       'start_line': 6, 'end_line': 7}]
        self.tools = AtomicSources(self.snapshot, self.units)

    def test_index_is_complete_paged_navigation_and_literal_query_is_not_resolution(self):
        first = self.tools.index(limit=1)
        second = self.tools.index(offset=1, limit=1)
        self.assertEqual((first['total'], first['omitted'], first['next_offset']), (2, 1, 1))
        self.assertEqual({x['id'] for x in first['units'] + second['units']}, {'u-receive', 'u-inspect'})
        match = self.tools.index(query='inspect')
        self.assertEqual([x['id'] for x in match['units']], ['u-inspect'])
        self.assertEqual(self.tools.index(query='inspe.*')['units'], [])
        self.assertFalse(match['control_flow_proven'])

    def test_read_delivers_full_requested_redacted_lines_with_hash_and_exact_address(self):
        read = self.tools.read('pkg/flow.py', 2, 4)
        self.assertEqual(read['text'], '2: def receive(item):\n3:     result = inspect(item)\n4:     return result')
        self.assertEqual(read['source_sha256'], self.snapshot.files['pkg/flow.py'].sha256)
        self.assertEqual(read['snapshot_id'], self.snapshot.snapshot_id)
        self.assertEqual(read['new_lines'], 3)
        self.assertTrue(self.tools.can_cite('pkg/flow.py', 2, 4))
        self.assertFalse(self.tools.can_cite('pkg/flow.py', 1, 4))
        safe = self.tools.read('pkg/data.py', 1, 1)
        self.assertNotIn('synthetic-secret-only', str(safe))
        self.assertIn('[REDACTED]', safe['text'])

    def test_repeat_is_not_new_source_progress_and_adjacent_ranges_merge_without_filling_gaps(self):
        self.tools.read('pkg/flow.py', 2, 3)
        repeated = self.tools.read('pkg/flow.py', 2, 3)
        self.assertEqual((repeated['seen_before'], repeated['new_lines']), (True, 0))
        self.tools.read('pkg/flow.py', 6, 7)
        self.assertFalse(self.tools.can_cite('pkg/flow.py', 2, 7))
        self.tools.read('pkg/flow.py', 4, 5)
        self.assertTrue(self.tools.can_cite('pkg/flow.py', 2, 7))
        self.assertEqual(self.tools.supplied_ranges(), [{'path': 'pkg/flow.py', 'start_line': 2, 'end_line': 7}])

    def test_search_is_literal_paged_and_does_not_authorize_citing_found_lines(self):
        result = self.tools.search('inspect', limit=1)
        self.assertEqual((result['total'], result['omitted']), (2, 1))
        self.assertEqual(result['matches'][0]['line'], 3)
        self.assertEqual(result['matches'][0]['unit_ids'], ['u-receive'])
        self.assertFalse(self.tools.can_cite('pkg/flow.py', 3, 3))
        rest = self.tools.search('inspect', offset=1, limit=1)
        self.assertEqual(rest['matches'][0]['line'], 6)
        self.assertEqual(self.tools.search('inspe.*')['matches'], [])

    def test_unit_read_is_exact_identity_and_does_not_guess_another_symbol(self):
        result = self.tools.read_unit('u-inspect')
        self.assertEqual((result['path'], result['start_line'], result['end_line']), ('pkg/flow.py', 6, 7))
        with self.assertRaises(SourceRequestError):
            self.tools.read_unit('inspect')

    def test_foreign_unsafe_and_noninteger_ranges_are_refused(self):
        for path in ('/tmp/file.py', '../pkg/flow.py', 'not-selected.py', '.env', 'pkg/../pkg/flow.py'):
            with self.subTest(path=path), self.assertRaises(SourceRequestError):
                self.tools.read(path, 1, 1)
        for first, last in ((0, 1), (4, 3), (True, 3), (2, 999), (2, 2.0)):
            with self.subTest(first=first, last=last), self.assertRaises(SourceRequestError):
                self.tools.read('pkg/flow.py', first, last)

    def test_oversized_read_or_search_fails_without_partial_delivery_or_silent_truncation(self):
        tiny = AtomicSources(self.snapshot, self.units, max_lines=2, max_chars=35)
        with self.assertRaises(SourceRequestError):
            tiny.read('pkg/flow.py', 2, 4)
        with self.assertRaises(SourceRequestError):
            tiny.read('pkg/flow.py', 1, 1)
        with self.assertRaises(SourceRequestError):
            tiny.search('fixture')
        self.assertEqual(tiny.supplied_ranges(), [])

    def test_bad_unit_bindings_and_limits_are_refused_before_any_read(self):
        for units in ([self.units[0], self.units[0]], [{**self.units[0], 'path': '../flow.py'}],
                      [{**self.units[0], 'end_line': 200}], [{**self.units[0], 'id': ''}]):
            with self.assertRaises(SourceRequestError):
                AtomicSources(self.snapshot, units)
        for options in ({'max_lines': 0}, {'max_chars': True}, {'max_lines': 1001}):
            with self.assertRaises(SourceRequestError):
                AtomicSources(self.snapshot, self.units, **options)

    def test_markup_is_delivered_as_inert_text_and_no_application_is_imported(self):
        result = self.tools.read('pkg/view.html', 1, 1)
        self.assertEqual(result['text'], '1: <script>never_execute()</script>')
        self.assertEqual(result['use'], 'source_data_only')

    def test_changing_selected_snapshot_refuses_prior_citation_and_further_observations(self):
        self.tools.read('pkg/flow.py', 2, 4)
        self.snapshot.files['pkg/flow.py'] = source('pkg/flow.py', self.text.replace('result', 'changed'))
        for operation in (lambda: self.tools.read('pkg/flow.py', 2, 4),
                          lambda: self.tools.index(), lambda: self.tools.search('inspect'),
                          lambda: self.tools.can_cite('pkg/flow.py', 2, 4), self.tools.supplied_ranges):
            with self.assertRaises(SourceRequestError) as raised:
                operation()
            self.assertEqual(raised.exception.code, 'snapshot_changed')

    def test_paging_and_literal_queries_have_strict_bounds(self):
        for kwargs in ({'offset': True}, {'limit': 0}, {'limit': 65}, {'query': ''}, {'query': 'x' * 257}):
            with self.assertRaises(SourceRequestError):
                self.tools.index(**kwargs)
        after = self.tools.search('inspect', offset=200)
        self.assertEqual((after['total'], after['matches'], after['next_offset']), (2, [], None))

    def test_preview_read_does_not_publish_ranges_until_host_accepts_it(self):
        self.tools.read('pkg/flow.py', 2, 3)
        preview = self.tools.fork()
        self.assertTrue(preview.can_cite('pkg/flow.py', 2, 3))
        preview.read('pkg/flow.py', 6, 7)
        self.assertTrue(preview.can_cite('pkg/flow.py', 6, 7))
        self.assertFalse(self.tools.can_cite('pkg/flow.py', 6, 7))
        self.tools.adopt(preview)  # the sequential host adopts the accepted observation
        self.assertTrue(self.tools.read('pkg/flow.py', 6, 7)['seen_before'])

    def test_stale_or_unrelated_preview_cannot_erase_newer_source_observations(self):
        preview = self.tools.fork()
        self.tools.read('pkg/flow.py', 2, 4)
        with self.assertRaises(SourceRequestError) as raised:
            self.tools.adopt(preview)
        self.assertEqual(raised.exception.code, 'stale_preview')
        self.assertTrue(self.tools.can_cite('pkg/flow.py', 2, 4))
        with self.assertRaises(SourceRequestError):
            self.tools.adopt(AtomicSources(self.snapshot, self.units).fork())

    def test_one_unsupported_snapshot_path_does_not_hide_safe_search_results(self):
        odd = 'pkg/odd\\name.py'
        self.snapshot.files[odd] = source(odd, 'inspect\n')
        tools = AtomicSources(self.snapshot, self.units)
        result = tools.search('inspect')
        self.assertEqual((result['total'], result['unsupported_paths']), (2, 1))
        with self.assertRaises(SourceRequestError):
            tools.read(odd, 1, 1)


if __name__ == '__main__':
    unittest.main()
