"""Protocol counterexamples independent of the real credit source; no model calls."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from process_observability.logging_review import _repair_details, _validate_reply
from process_observability.logging_contracts import LoggingReviewError
import test_map_first_context_accounting as accounting
import test_map_first_logging as basic
from process_observability.map_input import MapView
from process_observability.source_tools import snapshot_repository
from process_observability.operation_coverage import validate_saved_rows
from process_observability.logging_review import review_logging, SYSTEM


class PrimaryCoherenceControls(unittest.TestCase):
    def fixture(self, java=False):
        return accounting.ContextAccountingControls().fixture(java)

    def test_context_shape_errors_are_reported_despite_an_independent_binding_error(self):
        for java in (False, True):
            with self.subTest(java=java):
                packet, context, reply, _ = self.fixture(java)
                calls = packet['downstream_context']['delivered']
                reply['context_reviews'] = [
                    {'context_call_id': e['context_call_id'], 'classification': 'other_operation_log',
                     'reason': 'Другая операция'} for e in calls]
                reply['bindings'] = [{'site_id': packet['candidates'][0]['id'], 'establishes': []}]
                before = deepcopy(reply)
                details = _repair_details(reply, packet, context)
                issues = [i for i in details['issues'] if i['code'] == 'invalid_context_review']
                self.assertEqual(len(issues), len(calls))
                self.assertEqual(issues[0]['field'], 'context_reviews[0]')
                self.assertEqual(issues[0]['allowed_uses'], ['prior_fact_not_bound', 'not_relevant', 'unresolved'])
                self.assertIn('binding_establishes_invalid', {i['code'] for i in details['issues']})
                self.assertIn('context_not_assessed', {i['code'] for i in details['issues']})
                self.assertEqual(reply, before)
                with self.assertRaises(LoggingReviewError):
                    _validate_reply(reply, packet, context)

    def test_feedback_rejects_duplicate_unknown_and_fabricated_bound_context(self):
        packet, context, reply, _ = self.fixture()
        key = packet['downstream_context']['delivered'][0]['context_call_id']
        good = {'context_call_id': key, 'use': 'not_relevant', 'reason': 'Другой факт по коду'}
        for bad in ([good, good], [{**good, 'context_call_id': 'unknown'}], [{**good, 'use': 'bound'}],
                    [{'context_call_id': key, 'use': [], 'reason': 'invalid type'}]):
            with self.subTest(bad=bad):
                reply['context_reviews'] = bad
                issues = _repair_details(reply, packet, context)['issues']
                self.assertIn('invalid_context_review', {i['code'] for i in issues})
                with self.assertRaises(LoggingReviewError):
                    _validate_reply(reply, packet, context)

    def test_valid_context_assessment_is_not_asked_to_change_its_semantics(self):
        packet, context, reply, _ = self.fixture()
        reply['context_reviews'] = [
            {'context_call_id': e['context_call_id'], 'use': 'not_relevant', 'reason': 'Иной факт'}
            for e in packet['downstream_context']['delivered']]
        self.assertNotIn('invalid_context_review', {i['code'] for i in _repair_details(reply, packet, context)['issues']})
        self.assertEqual(_validate_reply(reply, packet, context)['state'], 'U')

    def run_intent(self, callback):
        code = 'import logging\nlogger = logging.getLogger(__name__)\ndef reserve(item):\n    logger.info("starting")\n    apply(item)\n'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'app.py').write_text(code)
            view = MapView.from_dict({'schema_version': '2.0', 'sources': [{'id': 'source-1'}], 'scenario': {},
                'nodes': [basic.operation(code, first=3, last=5)], 'edges': [], 'run': {'id': 'synthetic', 'status': 'completed'}})
            model = basic.Scripted(callback)
            return review_logging(view, snapshot_repository(root, max_files=None, max_chars=None), model), model

    def intent(self, packet):
        value = basic.model_reply(packet, key='starting', role='request', establishes=['occurrence'])
        value['bindings'][0].update(moment='before_action', covers_outcomes=[])
        value['missing'] = ['Результат выполнения не записан']
        return value

    def test_intent_cannot_claim_result_or_any_branch_outcome(self):
        for change in ({'covers_outcomes': ['success']}, {'establishes': ['result']},
                       {'covers_outcomes': ['no_decision']}, {'establishes': ['occurrence', 'result']}):
            with self.subTest(change=change):
                def callback(packet, number):
                    value = self.intent(packet); value['bindings'][0].update(change); return value
                result, model = self.run_intent(callback)
                self.assertEqual(result['operations'][0]['state'], 'X')
                self.assertIn('binding_moment_conflict', {i['code'] for i in model.calls[1]['repair_details']['issues']})

    def test_consistent_intent_remains_partial_and_saved_gate_is_profile_scoped(self):
        result, _ = self.run_intent(lambda p, n: self.intent(p))
        self.assertEqual(result['operations'][0]['state'], 'P')
        view = MapView.from_dict(result['map']); validate_saved_rows(view, result)
        edited = deepcopy(result); edited['operations'][0]['bindings'][0]['covers_outcomes'] = ['success']
        with self.assertRaises(LoggingReviewError): validate_saved_rows(view, edited)
        edited['review_profile'].pop('lifecycle')
        validate_saved_rows(view, edited)  # Historical reports keep historical acceptance.

    def test_source_navigation_shows_ownership_branch_and_context_without_binding(self):
        for java in (False, True):
            with self.subTest(java=java):
                packet, _, _, ids = self.fixture(java)
                direct = [s for s in packet['candidates'] if s.get('own_code')]
                self.assertTrue(direct)
                self.assertTrue(any(s['path_preview']['path_conditions'] for s in direct))
                for entry in packet['downstream_context']['delivered']:
                    self.assertTrue(entry['selection_preview'])
                    preview = entry['selection_preview'][0]
                    self.assertTrue(preview['navigation_only'])
                    self.assertFalse(preview['semantic_verified'])
                    self.assertTrue(preview['exact_single_hop'])
                    self.assertTrue(preview['record_at_entry'])
                self.assertFalse([s for s in packet['candidates'] if s['id'] in ids and s['own_code']])
                self.assertNotIn('bindings', packet)

    def test_primary_review_frame_has_all_lists_and_no_preselected_success(self):
        marker = '{"action":"review"'
        example, _ = json.JSONDecoder().raw_decode(SYSTEM[SYSTEM.index(marker):])
        self.assertEqual(set(example), {'action', 'operation_id', 'bindings', 'candidate_reviews', 'context_reviews',
                                       'missing', 'review_tasks', 'limitations', 'changes'})
        self.assertEqual(example['bindings'], [])
        self.assertEqual(example['changes'], [])
        result, _ = self.run_intent(lambda p, n: {**deepcopy(example), 'operation_id': p['operation']['id'],
            'bindings': self.intent(p)['bindings'], 'missing': ['Нет результата']})
        self.assertEqual(result['operations'][0]['state'], 'P')

    def test_intervening_assignment_does_not_get_a_positive_adjacent_preview(self):
        code = accounting.PYTHON.replace('journal', 'logger').replace(
            'return appointment_details(item.key)', 'item = reload(item)\n    return appointment_details(item.key)')
        packet, _, _, _ = accounting.ContextAccountingControls().fixture(code=code)
        entry = packet['downstream_context']['delivered'][0]
        self.assertEqual(entry['selection_preview'][0]['status_if_explicitly_selected'], 'not_established')
        self.assertIn('not_immediately_after_operation_anchor', entry['selection_preview'][0]['reasons'])

    def test_own_code_hint_is_reported_without_rewriting_a_route(self):
        for java in (False, True):
            with self.subTest(java=java):
                packet, context, reply, _ = self.fixture(java)
                site = next(s for s in packet['candidates'] if s['own_code'])
                ref = {k: site[k] for k in ('path', 'start_line', 'end_line')}
                reply['bindings'] = [{'site_id': site['id'], 'invocation_ref': ref, 'argument_evidence': [],
                                      'establishes': ['occurrence']}]
                before = deepcopy(reply)
                hints = [i for i in _repair_details(reply, packet, context)['issues'] if i['code'] == 'own_code_invocation']
                self.assertEqual(len(hints), 1)
                self.assertEqual(reply, before)

    def test_optional_preview_failure_does_not_prevent_actual_source_assessment(self):
        for function in ('contextual_basis', 'statement_witness'):
            for error in (AttributeError, IndexError, RecursionError):
                with self.subTest(function=function, error=error):
                    with patch('process_observability.logging_source_context.' + function,
                               side_effect=error('synthetic navigation failure')):
                        packet, context, reply, _ = self.fixture()
                    previews = packet['downstream_context']['delivered'][0]['selection_preview']
                    self.assertEqual(previews[0]['status_if_explicitly_selected'], 'not_established')
                    self.assertFalse(previews[0]['semantic_verified'])
                    reply['context_reviews'] = [{'context_call_id': e['context_call_id'], 'use': 'not_relevant',
                        'reason': 'Прочитанный вызов описывает иной факт'} for e in packet['downstream_context']['delivered']]
                    self.assertEqual(_validate_reply(reply, packet, context)['state'], 'U')


if __name__ == '__main__':
    unittest.main()
