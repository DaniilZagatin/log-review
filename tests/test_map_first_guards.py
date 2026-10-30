"""Failure controls independent of happy-path DTO shape."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from process_observability.logging_review import ReviewOptions, review_logging
from process_observability.map_input import MapView, RepoContext
from process_observability.source_tools import snapshot_repository
from test_map_first_logging import CODE, Scripted, model_reply, operation

WRAPPER = """import logging
logger = logging.getLogger(__name__)
def audit(message):
    logger.info(message)
def a():
    audit('a')
def b():
    audit('b')
"""


class Guards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.repo = self.root/'repo'; self.repo.mkdir()

    def setup_code(self, code, operations):
        (self.repo/'app.py').write_text(code)
        view = MapView.from_dict({'schema_version': '2.0', 'scenario': {}, 'sources': [{'id': 'source-1'}],
                                 'nodes': operations, 'edges': [], 'run': {'id': 'r1', 'status': 'completed'}})
        return view, snapshot_repository(self.repo, max_files=None, max_chars=None)

    def test_shared_wrapper_has_separate_invocation_witnesses(self):
        operations = [operation(WRAPPER, identifier='a', first=5, last=6, effect='command', outcome='unknown'),
                      operation(WRAPPER, identifier='b', first=7, last=8, effect='command', outcome='unknown')]
        view, snapshot = self.setup_code(WRAPPER, operations)
        def callback(packet, _):
            physical = next(s for s in packet['candidates'] if s['kind'] == 'log_call')
            line = 6 if packet['operation']['id'] == 'a' else 8
            ref = {'path': 'app.py', 'start_line': line, 'end_line': line}
            response = model_reply(packet, key=None)
            response['bindings'] = [{'site_id': physical['id'], 'role': 'result', 'meaning': 'Запись команды в аудит',
                    'conditions': 'После вызова данного обработчика', 'correlation': 'По переданному аргументу',
                    'establishes': ['occurrence', 'result'], 'evidence': [{'path': 'app.py', 'start_line': 4, 'end_line': 4}],
                    'invocation_ref': ref, 'argument_evidence': [ref],
                    'emission_evidence': [{'path': 'app.py', 'start_line': 3, 'end_line': 4}], 'covers_outcomes': []}]
            response['missing'] = []  # The explicit wrapper witness replaces the absent direct-site fixture.
            return response
        result = review_logging(view, snapshot, Scripted(callback))
        self.assertEqual([r['state'] for r in result['operations']], ['C', 'C'])
        self.assertEqual(result['operations'][0]['bindings'][0]['site_id'], result['operations'][1]['bindings'][0]['site_id'])
        self.assertNotEqual(result['operations'][0]['bindings'][0]['invocation_ref'], result['operations'][1]['bindings'][0]['invocation_ref'])

    def test_source_reuse_preserves_map_operations_without_counting_executions(self):
        view, snapshot = self.setup_code(CODE, [operation(identifier='a'), operation(identifier='b')])
        result = review_logging(view, snapshot, Scripted(lambda p, n: model_reply(p)))
        self.assertEqual(result['summary']['counts']['C'], 2)
        self.assertEqual(result['summary']['denominator'], 2)
        self.assertEqual(result['reconcile_policy'], 'independent-source-uses/1')
        self.assertEqual(len({b['site_id'] for r in result['operations'] for b in r['bindings']}), 1)

    def test_incomplete_read_scope_never_becomes_no_logging(self):
        view, snapshot = self.setup_code(CODE, [operation()])
        result = review_logging(view, snapshot, Scripted(lambda p, n: model_reply(p, key='none')),
                                options=ReviewOptions(max_context_lines=1))
        self.assertEqual(result['operations'][0]['state'], 'X')
        self.assertEqual(result['summary']['counts']['U'], 0)

    def test_inspection_cannot_make_an_unread_search_hit_a_quote(self):
        view, snapshot = self.setup_code(CODE, [operation()])
        # A hit is navigation only even when it names a real line.
        context = RepoContext(snapshot, view)
        context.search('logger.info')
        with self.assertRaises(ValueError): context.verify_ref({'path': 'app.py', 'start_line': 5, 'end_line': 5})

    def test_malformed_json_gets_one_repair_and_transport_failure_does_not(self):
        from process_observability.llm import LLMError
        view, snapshot = self.setup_code(CODE, [operation()])
        def callback(packet, n):
            if n == 1: raise LLMError('Fixture malformed JSON', category='invalid_json')
            return model_reply(packet)
        model = Scripted(callback); result = review_logging(view, snapshot, model)
        self.assertEqual(result['operations'][0]['state'], 'C')
        self.assertEqual(len(model.calls), 2)

    def test_edited_checkpoint_reply_is_not_reused(self):
        view, snapshot = self.setup_code(CODE, [operation()])
        first = review_logging(view, snapshot, Scripted(lambda p, n: model_reply(p)))
        first['checkpoints'][0]['reply']['bindings'][0]['meaning'] = 'Edited'
        model = Scripted(lambda p, n: model_reply(p)); again = review_logging(view, snapshot, model, resume=first)
        self.assertEqual(again['model_requests'], 1)

    def test_stale_sidecars_do_not_change_the_operation_decision(self):
        view, snapshot = self.setup_code(CODE, [operation()])
        folder = self.root/'artifacts'; folder.mkdir()
        (folder/'code_context.json').write_text(json.dumps({'schema_version': 'code-context/1', 'map_run_id': 'foreign',
            'files': [{'path': 'source-1/app.py', 'sha256': hashlib.sha256(CODE.encode()).hexdigest()}],
            'facts': [{'path': 'source-1/app.py', 'kind': 'LOG', 'value': 'untrusted foreign hint'}]}))
        without = review_logging(view, snapshot, Scripted(lambda p, n: model_reply(p)))
        with_stale = review_logging(view, snapshot, Scripted(lambda p, n: model_reply(p)), artifacts=folder)
        self.assertEqual(without['operations'], with_stale['operations'])
        self.assertEqual(with_stale['artifact_reuse']['facts'], 0)
        self.assertTrue(with_stale['artifact_reuse']['notes'])

    def test_actual_producer_header_and_lines_are_optional_reusable_context(self):
        view, snapshot = self.setup_code(CODE, [operation()])
        folder = self.root/'artifacts'; folder.mkdir(); sha = hashlib.sha256(CODE.encode()).hexdigest()
        header = {'schema_version': 'read-fragment/1', 'record': 'header', 'map_run_id': 'r1'}
        fragment = {'schema_version': 'read-fragment/1', 'record': 'fragment', 'path': 'source-1/app.py',
                    'file_sha256': sha, 'line_start': 3, 'line_end': 5, 'lines': CODE.splitlines()[2:5]}
        (folder/'read_fragments.jsonl').write_text(json.dumps(header)+'\n'+json.dumps(fragment)+'\n')
        (folder/'code_context.json').write_text(json.dumps({'schema_version': 'code-context/1', 'map_run_id': 'r1',
            'files': [{'path': 'source-1/app.py', 'sha256': sha}],
            'facts': [{'path': 'source-1/app.py', 'line': 3, 'kind': 'MODULE', 'value': 'existing finding'}]}))
        result = review_logging(view, snapshot, Scripted(lambda p, n: model_reply(p)), artifacts=folder)
        self.assertEqual(result['artifact_reuse']['read_fragments_verified'], 1)
        self.assertEqual(result['artifact_reuse']['read_fragments_used'], 1)
        self.assertGreaterEqual(result['artifact_reuse']['cache_line_lookups'], 3)
        self.assertEqual(result['artifact_reuse']['facts'], 1)
        self.assertEqual(result['operations'][0]['state'], 'C')

    def test_suggested_logging_never_changes_current_coverage(self):
        view, snapshot = self.setup_code(CODE, [operation(identifier='unlogged', first=10, last=11, effect='unknown', outcome='unknown')])
        change = {'id': 'record', 'kind': 'add_event', 'operation_ids': ['unlogged'], 'problem': 'Нет записи результата',
                  'what': 'Добавить запись результата', 'why': 'Увидеть факт обработки обращения', 'fields': [
                      {'name': 'operation', 'origin': 'proposed', 'expression': 'request.processed', 'evidence': []}],
                  'location': {'path': 'app.py', 'start_line': 11, 'end_line': 11},
                  'placement':'before','guards':[],
                  'timing': 'После завершения обработки', 'verification': 'Одна запись на успешное действие'}
        result = review_logging(view, snapshot, Scripted(lambda p, n: model_reply(p, key='absent', changes=[change])))
        self.assertEqual(result['operations'][0]['state'], 'U')
        self.assertTrue(result['changes'][0]['proposed'])
        self.assertEqual(result['summary']['uncovered']['lower'], 100)

    def test_existing_field_cannot_be_borrowed_from_a_different_function(self):
        view, snapshot = self.setup_code(CODE, [operation(identifier='unlogged', first=10, last=11, effect='unknown', outcome='unknown')])
        change = {'id': 'record', 'kind': 'add_event', 'operation_ids': ['unlogged'], 'problem': 'Нет записи',
                  'what': 'Добавить запись', 'why': 'Учесть действие', 'fields': [
                      {'name': 'request_id', 'origin': 'existing', 'expression': 'item.id',
                       'evidence': [{'path': 'app.py', 'start_line': 5, 'end_line': 5}]}],
                  'location': {'path': 'app.py', 'start_line': 11, 'end_line': 11},
                  'placement':'before','guards':[],
                  'timing': 'После обработки', 'verification': 'Проверить доступность ID'}
        model = Scripted(lambda p, n: model_reply(p, key='absent', changes=[change]))
        result = review_logging(view, snapshot, model)
        # Migrated for policy 2 (TASK Q2, VERIFIED-REVIEW item 4): after the one clear repair an invalid OPTIONAL
        # proposal is isolated with its diagnostic; the independently sound assessment (no record, completed search)
        # is kept. The borrowed field is still rejected and never published as a change.
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result['operations'][0]['state'], 'U')
        self.assertFalse(result['changes'])
        diagnostics = result['operations'][0]['change_diagnostics']
        self.assertEqual([d['local_id'] for d in diagnostics], ['record'])
        self.assertTrue(any(text in diagnostics[0]['error'] for text in ('not delivered', 'existing field', 'Existing field')))
        self.assertTrue(result['checkpoints'][0]['changes_isolated'])

    def test_java_standard_logger_has_source_bound_result_without_target_execution(self):
        code = 'class Service {\n  void complete() {\n    repository.save();\n    logger.info("done");\n  }\n}\n'
        view, _ = self.setup_code(code, [operation(code, first=2, last=5)])
        (self.repo/'app.java').write_text(code); (self.repo/'app.py').unlink()
        raw = view.original; raw['nodes'][0]['evidence'][0]['path'] = 'source-1/app.java'; view = MapView.from_dict(raw)
        result = review_logging(view, snapshot_repository(self.repo, max_files=None, max_chars=None), Scripted(lambda p, n: model_reply(p)))
        self.assertEqual(result['operations'][0]['state'], 'C')

    def test_model_sent_status_and_percentage_do_not_override_host_accounting(self):
        view, snapshot = self.setup_code(CODE, [operation(identifier='unlogged', first=10, last=11, effect='unknown', outcome='unknown')])
        def callback(packet, n):
            value = model_reply(packet, key='absent'); value.update(status='C', percentage=100, counts={'C': 999}); return value
        model = Scripted(callback); result = review_logging(view, snapshot, model)
        self.assertEqual(result['summary']['counts'], {'C': 0, 'P': 0, 'U': 1, 'X': 0})
        self.assertEqual(len(model.calls), 2)
        self.assertTrue(result['operations'][0]['warnings'])

    def test_interruption_keeps_accepted_prefix_and_marks_every_remaining_operation(self):
        operations = [operation(identifier='saved'), operation(identifier='refused', first=6, last=7, effect='decision', outcome='refusal'),
                      operation(identifier='unlogged', first=10, last=11, effect='unknown', outcome='unknown')]
        view, snapshot = self.setup_code(CODE, operations)
        def callback(packet, n):
            if n == 2: raise KeyboardInterrupt()
            return model_reply(packet)
        saved = []
        result = review_logging(view, snapshot, Scripted(callback), on_checkpoint=saved.append)
        self.assertTrue(result['interrupted'])
        self.assertEqual([r['state'] for r in result['operations']], ['C', 'X', 'X'])
        self.assertEqual(result['summary']['denominator'], 3)
        self.assertEqual(len(saved[-1]['checkpoints']), 1)

    def test_crash_checkpoint_resumes_without_reasking_the_accepted_operation(self):
        operations = [operation(identifier='saved'), operation(identifier='refused', first=6, last=7, effect='decision', outcome='refusal')]
        view, snapshot = self.setup_code(CODE, operations); saved = []
        def checkpoint(book):
            saved.append(book)
            raise RuntimeError('Simulated process crash after a saved prefix')
        with self.assertRaises(RuntimeError):
            review_logging(view, snapshot, Scripted(lambda p, n: model_reply(p)), on_checkpoint=checkpoint)
        model = Scripted(lambda p, n: model_reply(p, key='denied', role='error'))
        result = review_logging(view, snapshot, model, resume=saved[-1])
        self.assertEqual([r['state'] for r in result['operations']], ['C', 'C'])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0]['packet']['operation']['id'], 'refused')


if __name__ == '__main__': unittest.main()
