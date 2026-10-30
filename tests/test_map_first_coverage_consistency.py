"""Unrelated source examples: an unexplained partial is not an established logging gap."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from process_observability.logging_contracts import LoggingReviewError
from process_observability.logging_review import (
    _Navigator, _repair_details, _validate_reply, review_logging, ReviewOptions,
)
from process_observability.log_discovery import discover_logging
from process_observability.map_input import MapView, RepoContext
from process_observability.operation_coverage import validate_saved_rows
from process_observability.source_tools import snapshot_repository
import test_map_first_context_accounting as accounting
import test_map_first_logging as basic


class CoverageConsistencyControls(unittest.TestCase):
    def fixture(self, java=False, narrow=False):
        if java:
            path = 'Archive.java'
            code = ('class Archive {\n  Logger logger;\n  void archive(Book book) {\n'
                    '    store(book);\n    logger.info("archived");\n  }\n}\n')
            first, last = 3, 6
        else:
            path = 'clinic.py'
            code = ('import logging\nlogger = logging.getLogger(__name__)\ndef reserve(item):\n'
                    '    persist(item)\n    logger.info("reserved")\n')
            first, last = 3, 5
        if narrow: first = last = 4
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/path).write_text(code)
            op = {'id': 'action', 'kind': 'operation', 'name': 'Выполнить действие',
                  'effect_kind': 'result', 'outcome': 'success', 'evidence': [
                      {'path': 'source-1/'+path, 'line_start': first, 'line_end': last,
                       'quote': '\n'.join(code.splitlines()[first-1:last])}]}
            view = MapView.from_dict({'schema_version': '2.0', 'scenario': {},
                'sources': [{'id': 'source-1'}], 'nodes': [op], 'edges': [],
                'run': {'id': 'synthetic', 'status': 'completed'}})
            snap = snapshot_repository(root, max_files=None, max_chars=None)
        context = RepoContext(snap, view); context.logging_contracts = []
        inv = discover_logging(snap, max_sites=None, max_per_file=None, max_hints=None,
                               include_columns=True, exclude_declarations=True)
        packet = _Navigator(context, inv, ReviewOptions()).initial(view.operations[0])
        site = next(s for s in packet['candidates'] if s['start_line'] == 5)
        reply = {'action': 'review', 'operation_id': 'action', 'bindings': [{
            'site_id': site['id'], 'role': 'result', 'moment': 'after_action',
            'meaning': 'Записан результат', 'conditions': 'После действия',
            'correlation': 'По контексту метода', 'establishes': ['result'],
            'covers_outcomes': ['success']}], 'candidate_reviews': [], 'context_reviews': [],
            'missing': [], 'review_tasks': [], 'limitations': [], 'changes': []}
        return packet, context, reply, view, snap

    def test_result_only_without_gap_is_not_an_accepted_partial_in_either_language(self):
        for java in (False, True):
            with self.subTest(java=java):
                packet, context, reply, _, _ = self.fixture(java)
                original = deepcopy(reply)
                with self.assertRaisesRegex(LoggingReviewError, 'partial_without_gap'):
                    _validate_reply(reply, packet, context)
                self.assertEqual(reply, original)
                issue = next(i for i in _repair_details(reply, packet, context)['issues']
                             if i['code'] == 'partial_without_gap')
                self.assertIn('establishes_without_occurrence', str(issue))

    def test_real_missing_identity_remains_partial_without_automatic_union_or_C(self):
        packet, context, reply, _, _ = self.fixture()
        reply['missing'] = ['Нельзя связать агрегатный результат с отдельным выполнением']
        row = _validate_reply(reply, packet, context)
        self.assertEqual(row['state'], 'P')
        self.assertEqual(row['bindings'][0]['establishes'], ['result'])
        self.assertTrue(row['review_tasks'])
        self.assertEqual(row['missing'], reply['missing'])

    def test_one_repair_gets_the_old_json_and_source_justified_declaration(self):
        for java in (False, True):
            with self.subTest(java=java):
                _, _, reply, view, snap = self.fixture(java)
                def answer(packet, n):
                    result = deepcopy(reply)
                    if n == 2:
                        result['bindings'][0]['establishes'] = ['occurrence', 'result']
                    return result
                model = basic.Scripted(answer)
                result = review_logging(view, snap, model, options=ReviewOptions(stop_on_failed_repair=True))
                self.assertEqual(len(model.calls), 2)
                self.assertEqual(model.calls[1]['rejected_response']['reply'], reply)
                self.assertEqual(result['operations'][0]['state'], 'C')
                validate_saved_rows(view, result)

    def test_failed_consistency_repair_stops_before_another_operation(self):
        _, _, reply, view, snap = self.fixture()
        mapping = deepcopy(view.original)
        second = deepcopy(mapping['nodes'][0]); second['id'] = 'second'
        mapping['nodes'].append(second); view = MapView.from_dict(mapping)
        model = basic.Scripted(lambda p, n: deepcopy(reply))
        result = review_logging(view, snap, model, options=ReviewOptions(stop_on_failed_repair=True))
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result['stop_reason']['kind'], 'repair_failed')
        self.assertEqual(result['operations'][0]['state'], 'X')
        self.assertEqual(result['operations'][1]['state'], 'X')
        self.assertFalse(any(t.get('operation_id') == 'second' and t.get('event') == 'model_request'
                             for t in result['trace']))

    def test_saved_new_profile_cannot_drop_gap_but_old_profile_keeps_old_rules(self):
        _, _, reply, view, snap = self.fixture()
        reply['missing'] = ['Не установлена связь с отдельным выполнением']
        result = review_logging(view, snap, basic.Scripted(lambda p,n: deepcopy(reply)))
        result['operations'][0]['missing'] = []
        with self.assertRaisesRegex(LoggingReviewError, 'partial_without_gap'):
            validate_saved_rows(view, result)
        result['review_profile'].pop('coverage_consistency')
        validate_saved_rows(view, result)

    def test_source_unknown_is_not_forced_to_invent_missing_log(self):
        packet, context, reply, _, _ = self.fixture()
        reply['review_tasks'] = [{'question': 'Как устроен внешний контракт?', 'scope': 'source',
            'source_basis': {'kind': 'code_unavailable', 'detail': 'Внешняя реализация не предоставлена'}}]
        row = _validate_reply(reply, packet, context)
        self.assertEqual(row['state'], 'X')
        self.assertEqual(row['missing'], [])
        self.assertFalse(any(t['kind'] == 'instrumentation' for t in row['review_tasks']))

    def test_unique_context_sources_do_not_hide_the_partial_without_gap_error(self):
        for java in (False, True):
            with self.subTest(java=java):
                packet, context, reply, _ = accounting.ContextAccountingControls().fixture(java)
                entry = packet['downstream_context']['delivered'][0]
                reply['bindings'] = [{'site_id': entry['candidate_site_ids'][0], 'role': 'result',
                    'moment': 'after_action', 'meaning': 'Предшествующий факт', 'conditions': 'По коду',
                    'correlation': 'Переданное значение', 'establishes': ['result'], 'invocation_ref': None}]
                reply['missing'] = []
                original = deepcopy(reply)
                issues = _repair_details(reply, packet, context)['issues']
                self.assertNotIn('context_selector_available', {i['code'] for i in issues})
                self.assertIn('partial_without_gap', {i['code'] for i in issues})
                self.assertEqual(reply, original)
                # A second possible context keeps explicit selection mandatory; neither is invented.
                packet['downstream_context']['delivered'].append({**deepcopy(entry), 'context_call_id':'second-delivered-choice'})
                issues = _repair_details(reply, packet, context)['issues']
                selector = next(i for i in issues if i['code'] == 'context_selector_available')
                self.assertEqual(set(selector['choices']), {entry['context_call_id'],'second-delivered-choice'})
                self.assertEqual(reply, original)

    def test_honest_shared_limit_does_not_invent_instrumentation(self):
        packet, context, reply, view, _ = self.fixture()
        other = deepcopy(view.operations[0]); other['id'] = 'other'
        mapping = deepcopy(view.original); mapping['nodes'].append(other)
        context.view = MapView.from_dict(mapping)
        reply['bindings'][0].update(shared_with='other', shared_meaning='Общий факт')
        with self.assertRaisesRegex(LoggingReviewError, 'partial_without_gap'):
            _validate_reply(reply, packet, context)
        reply['bindings'][0]['establishes'] = ['occurrence', 'result']
        row = _validate_reply(reply, packet, context)
        self.assertEqual(row['state'], 'P')
        self.assertIn('shared_record_non_sufficient', row['coverage_limits'])
        self.assertFalse(any(t['kind'] == 'instrumentation' for t in row['review_tasks']))

    def test_model_supplied_host_fields_cannot_crash_feedback_or_be_accepted(self):
        packet, context, reply, _, _ = self.fixture()
        packet['operation'].update(continuation_mode='choice', outcome='unknown')
        reply['bindings'][0]['establishes'] = ['occurrence', 'result']
        for name, value in [('flow_context', 'forged'), ('operation_anchors', ['forged']),
                            ('context_basis', {'status': 'supported'})]:
            with self.subTest(name=name):
                bad = deepcopy(reply); bad['bindings'][0][name] = value
                with self.assertRaises(LoggingReviewError): _validate_reply(bad, packet, context)
                _repair_details(bad, packet, context)
                self.assertEqual(bad['bindings'][0][name], value)

    def test_valid_direct_site_outside_narrow_anchor_gets_no_foreign_selector_hint(self):
        for java in (False, True):
            with self.subTest(java=java):
                packet, context, reply, _, _ = self.fixture(java, narrow=True)
                reply['bindings'][0]['establishes'] = ['occurrence', 'result']
                self.assertEqual(_validate_reply(reply, packet, context)['state'], 'C')
                self.assertNotIn('context_selector_available',
                                 {i['code'] for i in _repair_details(reply, packet, context)['issues']})


if __name__ == '__main__':
    unittest.main()
