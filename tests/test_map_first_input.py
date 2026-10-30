"""Behavioral controls for a map-owned denominator and exact source navigation."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from process_observability.source_tools import snapshot_repository


def example_map():
    return {"schema_version": "2.0", "scenario": {"title": "Обработка обращения"},
            "sources": [{"id": "source-1", "name": "repo", "kind": "directory"}],
            "nodes": [{"id": "submit", "kind": "operation", "name": "Сохранить обращение",
                       "description": "Сохраняет новое обращение", "output": "Обращение сохранено",
                       "effect_kind": "result", "outcome": "success",
                       "evidence": [{"path": "source-1/app.py", "line_start": 3, "line_end": 5,
                                     "quote": "def submit(item):\n    save(item)\n    logger.info('saved', extra={'id': item.id})"}]}],
            "edges": [], "gaps": [], "run": {"status": "completed"}}


class MapInputTests(unittest.TestCase):
    def test_import_preserves_original_and_excludes_display_groups_from_denominator(self):
        from process_observability.map_input import MapView
        source = example_map()
        source['nodes'].append({'id': 'display', 'kind': 'group', 'name': 'Обращения'})
        before = copy.deepcopy(source)
        view = MapView.from_dict(source)
        self.assertEqual(view.operation_ids, ('submit',))
        self.assertEqual(view.original, before)
        source['nodes'][0]['name'] = 'Mutated externally'
        self.assertEqual(view.operations[0]['name'], before['nodes'][0]['name'])

    def test_duplicate_ids_and_dangling_transitions_are_rejected(self):
        from process_observability.map_input import MapView, MapInputError
        for change in ('duplicate', 'edge'):
            data = example_map()
            if change == 'duplicate': data['nodes'].append(copy.deepcopy(data['nodes'][0]))
            else: data['edges'].append({'id': 'e', 'source': 'submit', 'target': 'foreign'})
            with self.subTest(change=change), self.assertRaises(MapInputError):
                MapView.from_dict(data)

    def test_checkpoint_is_not_a_published_map(self):
        from process_observability.map_input import load_map, MapInputError
        with tempfile.TemporaryDirectory() as raw:
            p = Path(raw) / 'scenario_map.checkpoint.json'
            p.write_text(json.dumps(example_map()))
            with self.assertRaises(MapInputError): load_map(p)

    def test_source_prefix_resolves_without_rewriting_map_and_only_seen_text_is_citable(self):
        from process_observability.map_input import MapView, RepoContext, MapInputError
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, 'app.py').write_text("import logging\nlogger = logging.getLogger(__name__)\ndef submit(item):\n    save(item)\n    logger.info('saved', extra={'id': item.id})\n")
            snapshot = snapshot_repository(raw, max_files=None, max_chars=None)
            view = MapView.from_dict(example_map())
            context = RepoContext(snapshot, view)
            ref = view.operations[0]['evidence'][0]
            self.assertEqual(context.verify_map_ref(ref)['path'], 'app.py')
            with self.assertRaises(MapInputError):
                context.verify_ref({'path': 'app.py', 'start_line': 3, 'end_line': 5})
            context.read('app.py', 3, 5)
            self.assertEqual(context.verify_ref({'path': 'app.py', 'start_line': 3, 'end_line': 5})['quote'], ref['quote'])
            with self.assertRaises(MapInputError): context.read('../outside.py', 1, 2)

    def test_quote_mismatch_is_an_operation_issue_not_a_silent_repair(self):
        from process_observability.map_input import MapView, RepoContext, MapInputError
        with tempfile.TemporaryDirectory() as raw:
            Path(raw, 'app.py').write_text('one\ntwo\nchanged\nfour\nfive\n')
            context = RepoContext(snapshot_repository(raw, max_files=None, max_chars=None), MapView.from_dict(example_map()))
            with self.assertRaises(MapInputError): context.verify_map_ref(example_map()['nodes'][0]['evidence'][0])


class DeterministicCoverageTests(unittest.TestCase):
    def test_fixed_denominator_with_unknown_range(self):
        from process_observability.operation_coverage import summarize
        ids = tuple(str(i) for i in range(10))
        states = ['C'] * 6 + ['P', 'P', 'U', 'X']
        summary = summarize(ids, [{'operation_id': k, 'state': s} for k, s in zip(ids, states)])
        self.assertEqual(summary['counts'], {'C': 6, 'P': 2, 'U': 1, 'X': 1})
        self.assertEqual(summary['uncovered']['lower'], 30)
        self.assertEqual(summary['uncovered']['upper'], 40)
        self.assertFalse(summary['uncovered']['exact'])

    def test_missing_operation_remains_unknown_and_zero_has_no_percentage(self):
        from process_observability.operation_coverage import summarize
        self.assertEqual(summarize(('a', 'b'), [{'operation_id': 'a', 'state': 'C'}])['counts']['X'], 1)
        self.assertIsNone(summarize((), [])['uncovered']['lower'])

    def test_foreign_or_duplicate_accounting_cannot_change_denominator(self):
        from process_observability.operation_coverage import summarize
        for rows in ([{'operation_id': 'foreign', 'state': 'C'}],
                     [{'operation_id': 'a', 'state': 'C'}, {'operation_id': 'a', 'state': 'U'}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError): summarize(('a',), rows)


if __name__ == '__main__': unittest.main()
