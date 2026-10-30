"""Regression: repairs stay bounded and compaction cannot erase fine-grained work."""
from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from pko.llm.client import ChatResult
from pko.scenario_map.agent import AddressedRejections, _brief, run_agent
from pko.scenario_map.checkpoint_state import export_state, restore_state
from pko.scenario_map.compaction import compact_research_context, summary_request, validate_summary
from pko.scenario_map.context import _manifest, _size
from pko.scenario_map.sources import build_workspace
from scenario_script import SCENARIO, SPEC, scripted_summary, sources
import test_scenario_schema as schema_fixtures
import test_loop_guard_regressions as loop_fixtures


class StabilityMemoryTests(unittest.TestCase):
    def state(self):
        helper = schema_fixtures.ScenarioMapTests()
        helper.setUp()
        helper.complete_small_map()
        helper.review_current_operations()
        state = helper.state
        review = state.model['execution_reviews'][0]
        evidence = deepcopy(review['evidence'])
        review['route_expectations'] = [
            {'id': review['id'] + '/route-standard', 'condition': 'Платёж стандартный, договор не найден',
             'expected_result': 'Отказ с уточнением реквизитов', 'evidence': evidence,
             'edge_ids': [], 'completion_node_id': '', 'mapping_status': 'unmapped'},
            {'id': review['id'] + '/route-other', 'condition': 'Платёж нестандартный, договор не найден',
             'expected_result': 'Передача на ручную обработку', 'evidence': evidence,
             'edge_ids': [], 'completion_node_id': '', 'mapping_status': 'unmapped'},
        ]
        review['operations'][0].update(node_id='', mapping_status='unmapped')
        return state

    def messages(self):
        call = {'id': 'read-1', 'type': 'function',
                'function': {'name': 'read_file', 'arguments': '{}'}}
        return [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'scenario'},
                {'role': 'assistant', 'content': '', 'tool_calls': [call]},
                {'role': 'tool', 'tool_call_id': 'read-1', 'content': 'source data ' * 8000}]

    def test_oversized_lists_are_normalized_after_one_call_without_erasing_work(self):
        state = self.state()
        original = export_state(state)
        value = json.loads(scripted_summary().text)
        value['completed_work'] = ['Прочитан фрагмент ' + str(i) for i in range(20)]
        callback = Mock(return_value=ChatResult(text=json.dumps(value), usage={}))
        current = compact_research_context(self.messages(), state, 24000,
                                           summarize=callback, brief=_brief)
        callback.assert_called_once()
        self.assertLess(_size(current), _size(self.messages()))
        self.assertEqual(len(state.model['context_summary']['summary']['completed_work']), 12)
        self.assertEqual(state.model['context_summary']['normalization']['list_dropped_items'],
                         {'completed_work': 8})
        for key in ('nodes', 'edges', 'scenario', 'execution_reviews', 'research_tasks'):
            self.assertEqual(state.model[key], original['model'][key], key)
        restored = restore_state(state.workspace, export_state(state))
        self.assertEqual(restored.model['execution_reviews'], state.model['execution_reviews'])
        self.assertEqual(restored.model['context_summary'], state.model['context_summary'])
        self.assertEqual(restored._reads, state._reads)

    def test_fallback_keeps_both_unmapped_alternatives_and_action(self):
        state = self.state()
        original = deepcopy(state.model['execution_reviews'])
        callback = Mock(side_effect=RuntimeError('offline'))
        current = compact_research_context(self.messages(), state, 24000,
                                           summarize=callback, brief=_brief)
        callback.assert_called_once()
        self.assertEqual(state.model['execution_reviews'], original)
        self.assertEqual(state.model['run']['context_summary_fallbacks'], 1)
        self.assertIn('unmapped', json.dumps(current))
        restored = restore_state(state.workspace, export_state(state))
        self.assertEqual(restored.model['execution_reviews'], original)
        self.assertEqual(len(restored.snapshot()['detail_review']['unmapped_action_ids']), 1)

    def test_recovery_and_summary_input_show_pending_route_conditions(self):
        state = self.state()
        request = summary_request(self.messages(), state, 24000)
        current = json.loads(request[1]['content'])['current_state']
        self.assertEqual(current['unmapped_route_count'], 2)
        self.assertEqual(len(current['unmapped_routes']), 2)
        manifest = _manifest(state.snapshot())
        self.assertEqual(manifest['unmapped_route_count'], 2)
        self.assertEqual(manifest['unmapped_action_count'], 1)
        self.assertEqual(_manifest(state.snapshot(), 0)['unmapped_route_count'], 2)
        self.assertNotEqual(manifest['unmapped_routes'][0]['condition'], manifest['unmapped_routes'][1]['condition'])

    def test_bad_tail_is_not_laundered_by_summary_truncation(self):
        value = json.loads(scripted_summary().text)
        value['completed_work'] = ['valid'] * 12 + [{'unexpected': 'object'}]
        with self.assertRaises(ValueError):
            validate_summary(json.dumps(value), self.state())

    def test_resume_rejects_dangling_route_binding(self):
        state = self.state()
        payload = export_state(state)
        route = payload['model']['execution_reviews'][0]['route_expectations'][0]
        route.update(edge_ids=['edge-999'], mapping_status='mapped')
        with self.assertRaisesRegex(ValueError, 'неизвестный переход'):
            restore_state(state.workspace, payload)


class AddressedLoopTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = build_workspace(sources(), Path(self.temporary.name) / 'sources')

    def test_unrelated_semantic_edit_cannot_reset_same_rejected_object(self):
        prefix = loop_fixtures.graph_prefix()
        def batch(number):
            if number == 1:
                return prefix
            return [('upsert_step', {'id': 'step-1', 'evidence': [
                        {'path': 'source-1/app.py', 'line_start': 100, 'line_end': 100}]}),
                    ('upsert_step', {'id': 'step-18', 'output': 'Сохранён результат выпуска карты ' + str(number)}),
                    ('read_file', {'path': 'source-1/missing-' + str(number) + '.py'})]
        client = loop_fixtures.GeneratedBatchClient(batch)
        model = run_agent(self.workspace, SCENARIO, SPEC, client=client, max_steps=30)
        self.assertEqual(model['run']['stop_kind'], 'step_limit', model['run'])
        self.assertEqual(client.requests, model['run']['max_steps'])
        self.assertFalse(model['run'].get('loop_recovery_attempted'))
        self.assertTrue(model['run']['unresolved_rejections'])
        self.assertTrue(all(row['repair_attempts'] == 4 for row in model['run']['unresolved_rejections']))
        self.assertEqual(model['run']['unresolved_rejections'][0]['object_ids'], ['step-1'])
        self.assertEqual(len(model['nodes']), 18)

    def test_same_patch_item_stays_addressed_when_its_index_changes(self):
        prefix = loop_fixtures.graph_prefix()
        def batch(number):
            if number == 1:
                return prefix
            bad = {'id': 'step-1', 'evidence': [{'path': 'source-1/app.py', 'line_start': 100 + number, 'line_end': 100 + number}]}
            good = {'id': 'step-18', 'output': 'Результат выпуска сохранён ' + str(number)}
            steps = [bad, good] if number % 2 else [good, bad]
            return [('patch_fragment', {'steps': steps}),
                    ('upsert_step', good)]
        client = loop_fixtures.GeneratedBatchClient(batch)
        model = run_agent(self.workspace, SCENARIO, SPEC, client=client, max_steps=15)
        self.assertEqual(model['run']['stop_kind'], 'step_limit', model['run'])
        self.assertEqual(client.requests, model['run']['max_steps'])
        self.assertFalse(model['run'].get('loop_recovery_attempted'))
        self.assertTrue(model['run']['unresolved_rejections'])
        self.assertTrue(all(row['repair_attempts'] == 4 for row in model['run']['unresolved_rejections']))
        self.assertEqual(len(model['nodes']), 18)

    def test_missing_review_coverage_survives_unrelated_edits(self):
        from quality_helpers import review_payload
        saved = []
        prefix = loop_fixtures.graph_prefix()
        run_agent(self.workspace, SCENARIO, SPEC,
                  client=loop_fixtures.GeneratedBatchClient(lambda _: prefix),
                  max_steps=1, checkpoint=saved.append)
        state = restore_state(self.workspace, saved[-1]['_resume']['state'])
        args = review_payload(state)
        args['checks']['atomicity']['node_ids'] = [n['id'] for n in state.model['nodes'] if n['id'] != 'step-1']
        def batch(number):
            return [('review_semantics', args),
                    ('upsert_step', {'id': 'step-18', 'output': 'Результат выпуска сохранён ' + str(number)})]
        client = loop_fixtures.GeneratedBatchClient(batch)
        model = run_agent(self.workspace, SCENARIO, SPEC, client=client, max_steps=15,
                          resume_checkpoint=saved[-1])
        self.assertEqual(model['run']['stop_kind'], 'step_limit', model['run'])
        self.assertEqual(client.requests, model['run']['max_steps'])
        self.assertFalse(model['run'].get('loop_recovery_attempted'))
        self.assertTrue(model['run']['unresolved_rejections'])
        self.assertTrue(all(row['repair_attempts'] == 4 for row in model['run']['unresolved_rejections']))

    def test_output_edits_cannot_repair_an_unread_citation(self):
        prefix = loop_fixtures.graph_prefix()
        def batch(number):
            if number == 1:
                return prefix
            return [('upsert_step', {'id': 'step-1', 'evidence': [
                        {'path': 'source-1/app.py', 'line_start': 100, 'line_end': 100}]}),
                    ('upsert_step', {'id': 'step-1', 'output': 'Параметры обращения приняты с номером ' + str(number)})]
        client = loop_fixtures.GeneratedBatchClient(batch)
        model = run_agent(self.workspace, SCENARIO, SPEC, client=client, max_steps=10)
        self.assertEqual(model['run']['stop_kind'], 'step_limit', model['run'])
        self.assertEqual(client.requests, 10)
        self.assertFalse(model['run'].get('loop_recovery_attempted'))
        self.assertTrue(model['run']['unresolved_rejections'])
        self.assertEqual(model['run']['unresolved_rejections'][0]['repair_attempts'], 4)

    def test_meaningful_target_change_releases_non_evidence_problem(self):
        state = StabilityMemoryTests().state()
        tracker = AddressedRejections()
        function = {'name': 'upsert_step', 'arguments': json.dumps({'id': 'step-1'})}
        rejection = {'ok': False, 'code': 'quality_error', 'error': 'Missing continuation'}
        for _ in range(5):
            tracker.observe(function, rejection, state)
        self.assertTrue(tracker.stalled())
        state.model['nodes'][0]['continuation_mode'] = 'choice'
        tracker.refresh(state)
        self.assertFalse(tracker.stalled())

    def test_reordering_or_duplicating_evidence_does_not_release_rejection(self):
        state = StabilityMemoryTests().state()
        tracker = AddressedRejections()
        first = {'path': 'source-1/channel.py', 'line_start': 100, 'line_end': 100}
        second = {'path': 'source-2/cards.py', 'line_start': 100, 'line_end': 100}
        rejection = {'ok': False, 'code': 'invalid_evidence', 'error': 'Unread source range'}
        for refs in ([first, second], [second, first], [first, second, first], [second, first], [first, second]):
            tracker.observe({'name': 'upsert_step', 'arguments': json.dumps({
                'id': 'step-1', 'evidence': refs})}, rejection, state)
        self.assertTrue(tracker.stalled())
        self.assertEqual(tracker.problems[tracker.current]['count'], 5)

    def test_new_read_information_releases_addressed_problem(self):
        state = StabilityMemoryTests().state()
        tracker = AddressedRejections()
        function = {'name': 'upsert_step', 'arguments': json.dumps({'id': 'step-1'})}
        rejection = {'ok': False, 'code': 'quality_error', 'error': 'Missing source justification'}
        for _ in range(5):
            tracker.observe(function, rejection, state)
        self.assertTrue(tracker.stalled())
        # An additional actual line from the source is useful research progress.
        available = next((path, number) for path in sorted(state._files) if path.endswith('.py')
                         for number in range(1, len(state.workspace.tree.read(path).splitlines()) + 1)
                         if number not in state._reads.get(path, {}))
        state.observe_read(available[0], available[1], 1)
        tracker.observe(function, rejection, state)
        self.assertFalse(tracker.stalled())


if __name__ == '__main__':
    unittest.main()
