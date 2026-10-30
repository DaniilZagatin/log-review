"""Local repair budgets preserve useful work without multiplying diagnostics."""
from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pko.scenario_map.agent import AddressedRejections, MAX_ADDRESSED_REPAIR_ATTEMPTS, ResearchTools, run_agent
from pko.scenario_map.checkpoint_state import export_state, restore_state
from pko.scenario_map.sources import build_workspace
from scenario_script import SCENARIO, SPEC, research_calls, sources
from test_loop_guard_regressions import GeneratedBatchClient
import test_scenario_schema as schema_fixtures


def function(tool_name, **arguments):
    return {'name': tool_name, 'arguments': json.dumps(arguments, ensure_ascii=False)}


class AddressedRetryTests(unittest.TestCase):
    def setUp(self):
        helper = schema_fixtures.ScenarioMapTests()
        helper.setUp()
        helper.complete_small_map()
        self.state = helper.state
        self.tracker = AddressedRejections()
        self.bad = function('upsert_step', id='step-1', evidence=[
            {'path': 'source-1/channel.py', 'line_start': 100, 'line_end': 100}])
        self.failure = {'ok': False, 'code': 'invalid_evidence', 'error': 'Unread source range'}

    def exhaust(self):
        for _ in range(MAX_ADDRESSED_REPAIR_ATTEMPTS + 1):
            self.assertIsNone(self.tracker.blocked(self.bad, self.state))
            self.tracker.begin_step()
            self.assertTrue(self.tracker.observe(self.bad, self.failure, self.state))

    def test_duplicate_case_diagnostics_count_cases_once_not_shared_path_nodes(self):
        cases = [{'name': str(index), 'input': 'bad', 'path': ['step-1', 'step-2'],
                  'evidence': []} for index in range(2)]
        diagnostics = [{'case_index': index, 'field': 'input', 'problem': 'Use business input'}
                       for index in (1, 2)]
        payload = {'ok': False, 'code': 'quality_check', 'error': 'Case inputs', 'details': {
            'cases': deepcopy(diagnostics),
            'issues': [{'field': 'route_cases', 'problem': 'Case inputs', 'cases': deepcopy(diagnostics)}]}}
        self.assertTrue(self.tracker.observe(function('review_semantics', route_cases=cases), payload, self.state))
        self.assertEqual(len(self.tracker.problems), 2)
        self.assertEqual([row['count'] for row in self.tracker.problems.values()], [1, 1])
        self.assertEqual({identifier for row in self.tracker.problems.values() for identifier in row['identifiers']},
                         {'case:1', 'case:2'})
        self.assertFalse(self.tracker.stalled())

    def test_four_repairs_are_allowed_after_initial_rejection_then_blocked(self):
        self.assertEqual(MAX_ADDRESSED_REPAIR_ATTEMPTS, 4)
        self.exhaust()
        blocked = self.tracker.blocked(self.bad, self.state)
        self.assertEqual(blocked['code'], 'retry_budget_exhausted')
        pending = self.state.model['run']['unresolved_rejections']
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['repair_attempts'], 4)
        self.assertEqual(self.tracker.problems[self.tracker.current]['count'], 5)
        self.assertIsNone(self.tracker.blocked(function('upsert_step', id='step-2', output='Other work'), self.state))
        self.assertIsNone(self.tracker.blocked(function('upsert_step', id='step-1', output='Independent output'), self.state))
        self.assertIsNone(self.tracker.blocked(function('finish', summary='Partial'), self.state))

    def test_success_on_fourth_repair_is_not_blocked(self):
        for _ in range(4):  # Initial rejection plus three rejected repairs.
            self.tracker.observe(self.bad, self.failure, self.state)
        fourth_repair = function('upsert_step', id='step-1', evidence=self.state.model['nodes'][0]['evidence'])
        self.assertIsNone(self.tracker.blocked(fourth_repair, self.state))
        self.tracker.observe(fourth_repair, {'ok': True}, self.state)
        self.assertFalse(self.tracker.problems)
        self.assertFalse(self.state.model['run'].get('unresolved_rejections'))

    def test_duplicate_patch_issue_is_one_attempt_and_other_target_is_not_blocked(self):
        bad = AddressedRejections._arguments(self.bad)
        call = function('patch_fragment', steps=[bad, {'id': 'step-2', 'output': 'Other work'}])
        issue = {'field': 'steps[0]', 'code': 'invalid_evidence', 'problem': 'Unread source range'}
        rejection = {'ok': False, 'code': 'invalid_evidence', 'details': {'issues': [issue, deepcopy(issue)]}}
        for _ in range(5):
            self.tracker.observe(call, rejection, self.state)
        self.assertEqual(len(self.tracker.problems), 1)
        self.assertEqual(next(iter(self.tracker.problems.values()))['count'], 5)
        reversed_call = function('patch_fragment', steps=[{'id': 'step-2'}, bad])
        self.assertIsNotNone(self.tracker.blocked(reversed_call, self.state))
        self.assertIsNone(self.tracker.blocked(function('patch_fragment', steps=[{'id': 'step-2'}]), self.state))

    def test_new_read_reopens_budget_without_claiming_resolution(self):
        # Add an existing source line not yet delivered by read_file.
        self.state._reads['source-1/channel.py'].pop(2, None)
        self.bad = function('upsert_step', id='step-1', evidence=[
            {'path': 'source-1/channel.py', 'line_start': 2, 'line_end': 2}])
        self.exhaust()
        original = deepcopy(self.state.model['run']['unresolved_rejections'])
        self.state.observe_read('source-1/channel.py', 2, 1)
        self.assertIsNone(self.tracker.blocked(self.bad, self.state))
        self.assertEqual(self.state.model['run']['unresolved_rejections'], original)
        self.tracker.observe(self.bad, {'ok': True}, self.state)
        self.assertEqual(self.state.model['run']['unresolved_rejections'], [])

    def test_inventory_and_scope_diagnostics_have_local_budgets(self):
        for name, arguments, details, expected in (
            ('review_execution', {'id': 'review-1', 'operations': [{'id': 'op-1'}]},
             {'review_id': 'review-1', 'issues': [{'operation_id': 'op-1', 'field': 'operations', 'problem': 'Amend fact'}]},
             'op-1'),
            ('patch_fragment', {'execution_bindings': [{'review_id': 'review-1', 'operation_id': 'op-1'}]},
             {'issues': [{'operation_id': 'op-1', 'field': 'execution_bindings', 'problem': 'Preserve order'}]}, 'op-1'),
            ('set_scope', {'instance_unit': 'Another unit'}, {'field': 'instance_unit'}, 'scenario_scope'),
        ):
            with self.subTest(tool=name):
                tracker = AddressedRejections()
                call = function(name, **arguments)
                rejection = {'ok': False, 'code': 'fact_amendment_required', 'error': 'Explicit amendment required', 'details': details}
                for _ in range(5):
                    tracker.observe(call, rejection, self.state)
                self.assertEqual(len(tracker.problems), 1)
                self.assertEqual(next(iter(tracker.problems.values()))['identifiers'], {expected})
                self.assertEqual(tracker.blocked(call, self.state)['code'], 'retry_budget_exhausted')

    def test_unrelated_source_read_does_not_reset_an_addressed_failure(self):
        call = function('upsert_step', id='step-1', continuation_mode='choice')
        rejection = {'ok': False, 'code': 'quality_check', 'error': 'Missing continuation'}
        for _ in range(5):
            self.tracker.observe(call, rejection, self.state)
        self.state.observe_read('source-2/cards.py', 1, 3)
        self.assertEqual(self.tracker.blocked(call, self.state)['code'], 'retry_budget_exhausted')

    def test_malformed_patch_still_reaches_normal_argument_validation(self):
        self.exhaust()
        for value in (None, 4, 'not a list', {'id': 'step-1'}):
            with self.subTest(value=value):
                self.assertIsNone(self.tracker.blocked(function('patch_fragment', steps=value), self.state))

    def test_cosmetic_success_does_not_resolve_a_rejected_outcome(self):
        bad = function('upsert_step', id='step-1', name='Rename', outcome='bad')
        rejected = self.state.dispatch('upsert_step', AddressedRejections._arguments(bad))
        self.assertFalse(rejected['ok'])
        for _ in range(5):
            self.tracker.observe(bad, rejected, self.state)
        rename = function('upsert_step', id='step-1', name='Renamed')
        self.assertIsNone(self.tracker.blocked(rename, self.state))
        self.tracker.observe(rename, self.state.dispatch('upsert_step', AddressedRejections._arguments(rename)), self.state)
        self.assertEqual(self.tracker.blocked(bad, self.state)['code'], 'retry_budget_exhausted')
        self.assertEqual(len(self.state.model['run']['unresolved_rejections']), 1)

    def test_switching_already_read_wrong_quotes_does_not_reset_budget(self):
        self.state.observe_read('source-1/channel.py', 1, 1)
        self.state.observe_read('source-2/cards.py', 1, 1)
        for index in range(5):
            path = 'source-1/channel.py' if index % 2 else 'source-2/cards.py'
            call = function('upsert_step', id='step-1', evidence=[
                {'path': path, 'line_start': 1, 'line_end': 1, 'quote': 'wrong quote'}])
            rejection = self.state.dispatch('upsert_step', AddressedRejections._arguments(call))
            self.assertEqual(rejection['code'], 'invalid_evidence')
            self.tracker.observe(call, rejection, self.state)
        self.assertEqual(next(iter(self.tracker.problems.values()))['count'], 5)
        self.assertIsNotNone(self.tracker.blocked(call, self.state))

    def test_scope_title_does_not_resolve_boundary_amendment(self):
        call = function('set_scope', title='Changed title', instance_unit='Another case')
        rejection = {'ok': False, 'code': 'scope_amendment_required', 'error': 'Explicit change required',
                     'details': {'fields': ['instance_unit']}}
        for _ in range(5):
            self.tracker.observe(call, rejection, self.state)
        rename = function('set_scope', title='Only a public title')
        self.assertIsNone(self.tracker.blocked(rename, self.state))
        self.state.model['scenario']['title'] = 'Only a public title'
        self.tracker.observe(rename, {'ok': True}, self.state)
        self.assertIsNotNone(self.tracker.blocked(call, self.state))
        self.assertTrue(self.state.model['run']['unresolved_rejections'])

    def test_route_only_projection_diagnostic_blocks_same_edge_patch(self):
        call = function('patch_fragment', transitions=[{'id': 'edge-1', 'source': 'step-2', 'target': 'step-1'}])
        rejection = {'ok': False, 'code': 'execution_projection', 'error': 'Wrong order', 'details': {
            'issues': [{'route_id': 'route-1', 'field': 'route_expectations', 'problem': 'Wrong order'}]}}
        for _ in range(5):
            self.tracker.observe(call, rejection, self.state)
        self.assertIsNotNone(self.tracker.blocked(call, self.state))
        independent = function('patch_fragment', transitions=[{'id': 'edge-9', 'source': 'step-8', 'target': 'step-9'}])
        self.assertIsNone(self.tracker.blocked(independent, self.state))

    def test_accepted_withdrawal_resolves_a_superseded_inventory_problem(self):
        call = function('review_execution', id='review-1', operations=[{'id': 'op-1', 'action': 'Changed'}])
        rejection = {'ok': False, 'code': 'fact_amendment_required', 'error': 'Explicit amendment required',
                     'details': {'operation_id': 'op-1', 'field': 'operations'}}
        for _ in range(5):
            self.tracker.observe(call, rejection, self.state)
        discard = function('review_execution', id='review-1', discard_operations=[{'id': 'op-1'}])
        self.assertIsNone(self.tracker.blocked(discard, self.state))
        self.tracker.observe(discard, {'ok': True}, self.state)
        self.assertFalse(self.state.model['run']['unresolved_rejections'])
        self.assertFalse(self.tracker.problems)

    def test_resume_keeps_diagnostic_but_starts_fresh_runtime_budget(self):
        self.exhaust()
        restored = restore_state(self.state.workspace, export_state(self.state))
        fresh = AddressedRejections()
        self.assertIsNone(fresh.blocked(self.bad, restored))
        self.assertTrue(restored.model['run']['unresolved_rejections'])
        fresh.observe(self.bad, {'ok': True}, restored)
        self.assertEqual(restored.model['run']['unresolved_rejections'], [])


class AddressedRetryLoopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = build_workspace(sources(), Path(self.temporary.name) / 'sources')

    def test_exhausted_target_does_not_block_independent_work_or_finish(self):
        full = [call for call in research_calls() if call[0] != 'finish']
        bad = ('upsert_step', {'id': 'step-1', 'evidence': [
            {'path': 'source-1/app.py', 'line_start': 100, 'line_end': 100}]})
        responses = []
        dispatched_bad = []
        original = ResearchTools.answer
        def answer(owner, call):
            if (call.get('function') or {}).get('name') == 'upsert_step' and '100' in call['function']['arguments']:
                dispatched_bad.append(call)
            response = original(owner, call)
            responses.append(json.loads(response['content']))
            return response
        def batch(number):
            if number == 1:
                return full
            if number <= 6:
                return [bad]
            if number == 7:
                return [bad, ('upsert_step', {'id': 'step-18', 'name': 'Подтверждение выпуска карты'})]
            return [('finish', {'summary': 'Сохранены результаты с открытым вопросом'})]
        client = GeneratedBatchClient(batch)
        with patch.object(ResearchTools, 'answer', answer):
            model = run_agent(self.workspace, SCENARIO, SPEC, client=client, max_steps=20)
        self.assertEqual(client.requests, 8)
        self.assertEqual(len(dispatched_bad), 5, 'Initial failure plus exactly four repairs are dispatched')
        self.assertEqual(model['run']['stop_kind'], 'finish', model['run'])
        self.assertEqual(model['run']['status'], 'partial', model['run'])
        self.assertEqual(next(n for n in model['nodes'] if n['id'] == 'step-18')['name'], 'Подтверждение выпуска карты')
        self.assertEqual(len(model['run']['unresolved_rejections']), 1)
        self.assertFalse(model['run'].get('loop_recovery_attempted'))

    def test_inspection_repetitions_do_not_cut_short_fourth_repair(self):
        full = [call for call in research_calls() if call[0] != 'finish']
        bad = ('upsert_step', {'id': 'step-1', 'outcome': 'invalid'})
        def batch(number):
            if number == 1:
                return full
            if number <= 5:
                return [bad] + [('inspect_map', {'section': 'summary'})] * 3
            if number == 6:
                return [('upsert_step', {'id': 'step-1', 'outcome': 'unknown'})]
            return [('finish', {'summary': 'Четвёртое исправление принято'})]
        client = GeneratedBatchClient(batch)
        model = run_agent(self.workspace, SCENARIO, SPEC, client=client, max_steps=20)
        self.assertEqual(client.requests, 7)
        self.assertEqual(model['run']['stop_kind'], 'finish', model['run'])
        self.assertFalse(model['run'].get('unresolved_rejections'))
        self.assertFalse(model['run'].get('loop_recovery_attempted'))


if __name__ == '__main__':
    unittest.main()
