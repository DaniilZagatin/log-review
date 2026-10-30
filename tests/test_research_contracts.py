"""Independent inventories, dependency coverage and transactional fragment edits."""
from copy import deepcopy
import unittest

from pko.scenario_map.schema import MapState, EXECUTION_CHECKS
from test_scenario_schema import workspace
from quality_helpers import explain_args


class ResearchContractTests(unittest.TestCase):
    def setUp(self):
        self.state = MapState(workspace(1), 'Обработка одного обращения')
        self.state.observe_read('source-1/channel.py', 1, 2)
        self.ref = {'path': 'source-1/channel.py', 'line_start': 1, 'line_end': 2}

    def good(self, tool, **args):
        result = self.state.dispatch(tool, args)
        self.assertTrue(result['ok'], result)
        return result

    def step_args(self, **args):
        return explain_args({'name': 'Проверка результата', 'description': 'Обработка данных обращения',
                             'evidence': [self.ref], 'evidence_status': 'observed', **args})

    def node(self, **args):
        return self.good('upsert_step', **self.step_args(**args))['id']

    def inventory(self, *ids, **extra):
        return {'name': 'Разбор обработчика', 'evidence': [self.ref],
                'operations': [{'node_id': node_id, 'name': 'Обработка обращения', 'kind': 'action',
                                'action': 'Обработать данные', 'effect': 'Результат получен', 'failure': 'Ошибка получения',
                                **({'evidence': [self.ref]} if not node_id else {})} for node_id in ids],
                'checks': {key: {'status': 'not_found', 'explanation': 'В данном фрагменте не установлено',
                                  'node_ids': [], 'edge_ids': [], 'gap_ids': []} for key in EXECUTION_CHECKS}, **extra}

    def task(self, status='mapped', **extra):
        return self.good('save_research_task', target='Обработчик', kind='fragment', status=status,
                         resolution='Локальный участок разобран', evidence=[self.ref], **extra)['id']

    def test_unmapped_action_survives_and_cannot_be_hidden_by_overlap(self):
        node = self.node()
        review = self.good('review_execution', **self.inventory(node, ''))
        task = self.task(node_ids=[node])
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot['semantic_profile'], 'variant-actions-v1')
        self.assertEqual(len(snapshot['detail_review']['unmapped_action_ids']), 1)
        coverage = next(r for r in snapshot['research_coverage']['tasks'] if r['task_id']==task)
        self.assertEqual(coverage['status'], 'partial')
        self.assertIn('не все найденные действия отображены', coverage['local_issues'])
        inspected = self.good('inspect_map', section='operations')
        self.assertEqual(inspected['items'][1]['node_id'], '')
        second = self.node(name='Сохранение результата')
        payload = self.inventory(node, second, id=review['id'])
        payload['operations'][1]['id'] = review['operations'][1]['id']
        self.good('review_execution', **payload)
        self.assertEqual(self.state.research_coverage()['tasks'][0]['status'], 'covered')

    def test_scope_independent_and_evidence_reused_only_for_mapped_action(self):
        node = self.node()
        self.good('review_execution', **self.inventory(node))
        bad = self.inventory('')
        bad['operations'][0].pop('evidence')
        self.assertFalse(self.state.dispatch('review_execution', bad)['ok'])
        bad = self.inventory(node)
        bad['evidence'] = []
        self.assertFalse(self.state.dispatch('review_execution', bad)['ok'])

    def test_unmapped_inventory_can_address_unknown_continuation_by_task(self):
        task = self.task('open')
        gap = self.good('add_gap', category='unexplored', description='Продолжение ещё не обследовано',
                        task_ids=[task], evidence=[self.ref])['id']
        payload = self.inventory('')
        payload['checks']['integrations'].update(status='unknown', gap_ids=[gap])
        self.good('review_execution', **payload)
        self.assertEqual(self.state.model['nodes'], [])
        self.assertEqual(self.state.validation_signals()['structural_issues'], [])

    def test_parent_does_not_copy_children_and_aggregates_partial(self):
        node = self.node()
        self.good('review_execution', **self.inventory(node))
        child = self.task('open')
        parent = self.task(node_ids=[node], child_task_ids=[child])
        saved = next(t for t in self.state.model['research_tasks'] if t['id']==parent)
        self.assertEqual(saved['node_ids'], [node])
        row = next(r for r in self.state.research_coverage()['tasks'] if r['task_id']==parent)
        self.assertEqual(row['incomplete_task_ids'], [child])
        self.good('save_research_task', id=child, status='mapped', node_ids=[node], resolution='Отображено')
        self.assertEqual(next(r for r in self.state.research_coverage()['tasks'] if r['task_id']==parent)['status'], 'covered')

    def test_gap_reference_propagates_without_copy(self):
        node = self.node()
        self.good('review_execution', **self.inventory(node))
        child = self.task('open')
        gap = self.good('add_gap', category='unexplored', description='Продолжение не исследовано',
                        task_ids=[child], evidence=[self.ref])['id']
        self.good('save_research_task', id=child, status='gap', gap_ids=[gap], resolution='Оставлен адресный пробел')
        parent = self.task(node_ids=[node], child_task_ids=[child])
        row = next(r for r in self.state.research_coverage()['tasks'] if r['task_id']==parent)
        self.assertEqual(row['gap_ids'], [gap])
        self.assertEqual(len(self.state.model['gaps']), 1)

    def test_cycles_terminate_without_manufacturing_coverage(self):
        node = self.node()
        left = self.task(node_ids=[node])
        right = self.task(node_ids=[node], child_task_ids=[left])
        self.good('save_research_task', id=left, child_task_ids=[right])
        self.assertTrue(all(row['status']=='partial' for row in self.state.research_coverage()['tasks']))
        self.good('review_execution', **self.inventory(node))
        self.assertTrue(all(row['status']=='covered' for row in self.state.research_coverage()['tasks']))

    def test_empty_local_inventory_does_not_require_wrapper_node(self):
        node = self.node()
        self.good('review_execution', **self.inventory(node))
        child = self.task(node_ids=[node])
        self.good('review_execution', **self.inventory())
        parent = self.task(child_task_ids=[child])
        saved = next(t for t in self.state.model['research_tasks'] if t['id']==parent)
        self.assertEqual(saved['node_ids'], [])
        self.assertEqual(next(r for r in self.state.research_coverage()['tasks'] if r['task_id']==parent)['status'], 'covered')
        self.assertEqual(len(self.state.model['nodes']), 1)

    def test_finish_preserves_open_tasks_as_partial_but_rejects_broken_reference(self):
        node = self.node(outcome='success')
        self.task('open')
        self.good('set_scope', title='Обработка обращения', start='Получено обращение', end='Получен результат',
                  instance_unit='Одно обращение', variant_context='Выбранный способ обработки', variant_factors=['Канал'],
                  entry_operation_ids=[node], terminal_operation_ids=[node])
        result = self.good('finish', summary='Исследование не закончено')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.state.snapshot()['validation']['structural_issues'], [])
        self.state.model['scenario']['terminal_operation_ids'] = ['missing']
        self.assertFalse(self.state.dispatch('finish', {'summary': 'Завершено'})['ok'])

    def test_fragment_replaces_path_atomically_with_local_ids(self):
        left, right = self.node(), self.node(name='Завершение')
        edge_args = explain_args({'source': left, 'target': right, 'evidence_status': 'observed', 'evidence': [self.ref]})
        old = self.good('upsert_transition', **edge_args)['id']
        candidate = self.good('patch_fragment', steps=[{**self.step_args(name='Новая проверка'), 'local_ref': 'check'}],
                              transitions=[{**edge_args, 'target': '$check'}, {**edge_args, 'source': '$check'}],
                              remove_edge_ids=[old])
        check = candidate['local_ids']['check']
        self.assertEqual({(e['source'], e['target']) for e in self.state.model['edges']}, {(left,check),(check,right)})
        before = deepcopy(self.state.model)
        counters = self.state._counters.copy()
        bad = self.state.dispatch('patch_fragment', {'steps': [self.step_args()], 'transitions': [{**edge_args,'target':'$missing'}]})
        self.assertFalse(bad['ok'])
        self.assertEqual(self.state.model, before)
        self.assertEqual(self.state._counters, counters)

    def test_changed_action_invalidates_computed_coverage_but_name_does_not(self):
        node = self.node()
        self.good('review_execution', **self.inventory(node))
        self.task(node_ids=[node])
        self.good('upsert_step', id=node, name='Получение результата')
        self.assertEqual(self.state.research_coverage()['status'], 'covered')
        self.good('upsert_step', id=node, output='Другой предметный результат')
        self.assertEqual(self.state.research_coverage()['status'], 'partial')

    def test_mapped_inventory_contradiction_is_structural_but_unmapped_is_coverage(self):
        node = self.node()
        payload = self.inventory(node)
        payload['operations'][0]['kind'] = 'integration'
        payload['checks']['integrations'].update(status='present', node_ids=[node])
        self.good('review_execution', **payload)
        self.assertTrue(any('external_interactions' in issue for issue in self.state.validation_signals()['structural_issues']))
        self.assertFalse(self.state.dispatch('finish', {'summary':'Незавершено'})['ok'])

    def test_fragment_removal_preserves_unmapped_inventory_and_rolls_back_dangling_edge(self):
        left, right = self.node(), self.node(name='Сохранение результата')
        edge = self.good('upsert_transition', **explain_args({'source':left, 'target':right,
                        'evidence_status':'observed', 'evidence':[self.ref]}))['id']
        self.good('review_execution', **self.inventory(left, right))
        before = deepcopy(self.state.model)
        rejected = self.state.dispatch('patch_fragment', {'remove_node_ids':[right]})
        self.assertFalse(rejected['ok'])
        self.assertEqual(self.state.model, before)
        self.good('patch_fragment', remove_node_ids=[right], remove_edge_ids=[edge])
        review = self.state.model['execution_reviews'][0]
        self.assertEqual(review['operations'][1]['node_id'], '')
        self.assertIn(right, review['node_fingerprints'])
        self.assertEqual(self.state.validation_signals()['structural_issues'], [])
        self.assertTrue(self.state.snapshot()['detail_review']['unmapped_action_ids'])


if __name__ == '__main__':
    unittest.main()
