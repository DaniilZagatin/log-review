"""Source-bound execution survives compaction and resume independently of the graph."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from pko.scenario_map.agent import _brief
from pko.scenario_map.checkpoint_state import export_state, restore_state
from pko.scenario_map.compaction import compact_research_context, summary_request
from pko.scenario_map.context import _manifest, execution_memory, graph_identity
from pko.scenario_map.schema import MapValidationError
from scenario_script import scripted_summary
import test_scenario_schema as fixtures


class ExecutionMemoryTests(unittest.TestCase):
    def state(self):
        helper = fixtures.ScenarioMapTests()
        helper.setUp()
        helper.complete_small_map()
        evidence = helper.cite(start=1, end=2)
        for node in helper.state.model['nodes']:
            node['evidence'] = deepcopy(evidence)
        helper.review_current_operations()
        state = helper.state
        review = state.model['execution_reviews'][0]
        edge = state.model['edges'][0]
        operations = {row['node_id']: row for row in review['operations']}
        first, following = operations[edge['source']], operations[edge['target']]
        interaction = {'name': 'Банк', 'purpose': 'Передача заявки на выпуск карты',
                       'direction': 'request', 'endpoint': '/cards', 'contract': '',
                       'evidence': deepcopy(evidence)}
        first['external_interactions'] = [deepcopy(interaction)]
        next(node for node in state.model['nodes'] if node['id'] == first['node_id'])['external_interactions'] = [interaction]
        route_id = review['id'] + '/route-order'
        review.update(fact_revision=3, mapping_revision=5, fact_changes=[
            {'revision': 2, 'reason': 'Уточнён самостоятельный запрос', 'evidence': deepcopy(evidence),
             'operation_ids': [first['id']], 'route_ids': []},
            {'revision': 3, 'reason': 'Прочитана последовательность выполнения', 'evidence': deepcopy(evidence),
             'operation_ids': [], 'route_ids': [route_id]},
        ], route_expectations=[{
            'id': route_id, 'condition': 'Заявка передана', 'expected_result': 'Продолжить обработку заявки',
            'from_operation_id': first['id'], 'to_operation_id': following['id'],
            'evidence': deepcopy(evidence), 'edge_ids': [edge['id']], 'completion_node_id': '',
            'mapping_status': 'mapped',
        }])
        state.model['scenario'].update(scope_revision=2, scope_changes=[{
            'revision': 2, 'reason': 'Установлена единица исполнения', 'evidence': deepcopy(evidence),
            'fields': ['instance_unit'],
        }])
        state.model['run']['unresolved_rejections'] = [{
            'problem_id': 'a' * 64, 'tool': 'review_semantics', 'object_ids': [first['id']],
            'code': 'quality_check', 'error': 'Не удалось уточнить результат',
            'repair_attempts': 4, 'status': 'open',
        }]
        return state

    @staticmethod
    def messages():
        call = {'id': 'read-1', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}
        return [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'scenario'},
                {'role': 'assistant', 'content': '', 'tool_calls': [call]},
                {'role': 'tool', 'tool_call_id': 'read-1', 'content': 'source data ' * 8000}]

    def test_mapped_order_and_external_call_remain_visible_to_researcher(self):
        state = self.state()
        manifest = _manifest(state.snapshot())
        current = json.loads(summary_request(self.messages(), state, 24000)[1]['content'])['current_state']
        for view in (manifest, current):
            self.assertEqual(view['unmapped_route_count'], 0)
            self.assertEqual(view['execution_relation_count'], 1)
            relation = view['execution_relations'][0]
            self.assertEqual(relation['mapping_status'], 'mapped')
            self.assertEqual(relation['fact_revision'], 3)
            self.assertEqual(relation['mapping_revision'], 5)
            self.assertNotEqual(relation['from_operation_id'], relation['to_operation_id'])
            self.assertEqual(view['execution_interaction_action_count'], 1)
            self.assertEqual(view['execution_interactions'][0]['interactions'][0]['endpoint'], '/cards')
            self.assertEqual(view['unresolved_rejection_count'], 1)
            self.assertEqual(view['unresolved_rejections'][0]['repair_attempts'], 4)
        smallest = _manifest(state.snapshot(), 0)
        self.assertEqual(smallest['execution_relation_count'], 1)
        self.assertFalse(smallest['execution_relations_listed_all'])
        self.assertIn('route_expectations', smallest['recover_relations'])

    def test_successful_summary_and_fallback_preserve_exact_execution_and_scope(self):
        for fails in (False, True):
            with self.subTest(fallback=fails):
                state = self.state()
                before = deepcopy(state.model)
                callback = Mock(side_effect=RuntimeError('offline')) if fails else Mock(return_value=scripted_summary())
                compact_research_context(self.messages(), state, 24000, summarize=callback, brief=_brief)
                callback.assert_called_once()
                self.assertEqual(state.model['execution_reviews'], before['execution_reviews'])
                self.assertEqual(state.model['scenario'], before['scenario'])
                restored = restore_state(state.workspace, export_state(state))
                self.assertEqual(restored.model['execution_reviews'], before['execution_reviews'])
                self.assertEqual(restored.model['scenario'], before['scenario'])
                self.assertEqual(restored.model['run']['unresolved_rejections'], before['run']['unresolved_rejections'])
                if not fails:
                    self.assertTrue(restored.model['context_summary']['input_execution_relations_complete'])
                    self.assertTrue(restored.model['context_summary']['input_execution_interactions_complete'])

    def test_bounded_relations_and_calls_explicitly_report_omissions(self):
        state = self.state()
        review = state.model['execution_reviews'][0]
        route = review['route_expectations'][0]
        review['route_expectations'] = [{**deepcopy(route), 'id': review['id'] + '/route-' + str(i)} for i in range(15)]
        memory = execution_memory(state.model, take=2)
        self.assertEqual(len(memory['execution_relations']), 2)
        self.assertEqual(memory['execution_relation_count'], 15)
        self.assertFalse(memory['execution_relations_listed_all'])
        self.assertEqual(len(review['route_expectations']), 15)

    def test_resume_rejects_corrupted_fact_relations_revisions_and_call_evidence(self):
        state = self.state()
        mutations = [
            lambda p: p['model']['execution_reviews'][0]['route_expectations'][0].update(to_operation_id='op-999'),
            lambda p: p['model']['execution_reviews'][0]['route_expectations'][0].update(from_operation_id=''),
            lambda p: p['model']['execution_reviews'][0].update(fact_revision=True),
            lambda p: p['model']['execution_reviews'][0]['fact_changes'][0].update(revision=4),
            lambda p: p['model']['execution_reviews'][0]['operations'][0]['external_interactions'][0]['evidence'][0].update(quote='forged'),
            lambda p: p['model']['scenario'].update(scope_revision=0),
            lambda p: p['model']['scenario']['scope_changes'][0].update(fields=['entry_operation_ids']),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                payload = export_state(state)
                mutate(payload)
                with self.assertRaises((MapValidationError, ValueError)):
                    restore_state(state.workspace, payload)

    def test_shared_terminal_roundtrips_without_inventing_one_outcome(self):
        state = self.state()
        last = next(node for node in state.model['nodes'] if node['id'] in state.model['scenario']['terminal_operation_ids'])
        last.update(outcome='unknown', outcome_scope='shared')
        restored = restore_state(state.workspace, export_state(state))
        restored_last = next(node for node in restored.model['nodes'] if node['id'] == last['id'])
        self.assertEqual(restored_last['outcome_scope'], 'shared')
        self.assertEqual(restored_last['outcome'], 'unknown')
        registry_last = next(row for row in graph_identity(restored.model)['nodes'] if row['id'] == last['id'])
        self.assertEqual(registry_last['outcome_scope'], 'shared')
        payload = export_state(state)
        next(node for node in payload['model']['nodes'] if node['id'] == last['id'])['outcome'] = 'success'
        with self.assertRaisesRegex(MapValidationError, 'финализатор'):
            restore_state(state.workspace, payload)

    def test_absent_additive_fields_are_not_invented_during_state_restore(self):
        state = self.state()
        payload = export_state(state)
        scenario = payload['model']['scenario']
        scenario.pop('scope_revision', None)
        scenario.pop('scope_changes', None)
        for node in payload['model']['nodes']:
            node.pop('outcome_scope', None)
        for review in payload['model']['execution_reviews']:
            for field in ('fact_revision', 'mapping_revision', 'fact_changes'):
                review.pop(field, None)
            for action in review['operations']:
                action.pop('external_interactions', None)
            for route in review['route_expectations']:
                route.pop('from_operation_id', None)
                route.pop('to_operation_id', None)
        restored = restore_state(state.workspace, payload)
        self.assertEqual(restored.model, payload['model'])


if __name__ == '__main__':
    unittest.main()
