"""Control cases for execution discoveries and their graph projection."""
from copy import deepcopy
import unittest

import test_research_contracts as helpers
from quality_helpers import explain_args
from pko.scenario_map.checkpoint_state import export_state, restore_state


class ExecutionPreservationTests(unittest.TestCase):
    def setUp(self):
        self.h = helpers.ResearchContractTests()
        self.h.setUp()
        self.state = self.h.state

    def discover(self, external=False):
        args = self.h.inventory('', '')
        for row, alias, action in zip(args['operations'], ('close', 'create'), ('Закрыть текущую задачу', 'Создать ручную задачу')):
            row.update(local_ref=alias, action=action, name=action)
        if external:
            args['operations'][0].update(kind='write', external_interactions=[self.call()])
            args['checks']['integrations'].update(status='present')
            args['checks']['state_changes'].update(status='present')
        args['route_expectations'] = [{'condition': 'Текущая задача закрыта', 'expected_result': 'Создать ручную задачу',
            'from_operation_id': '$close', 'to_operation_id': '$create', 'evidence': [self.h.ref]}]
        return self.h.good('review_execution', **args)

    def call(self):
        return {'name': 'Обработка задач', 'purpose': 'Закрыть текущую задачу', 'direction': 'send',
                'contract': 'close', 'evidence': [self.h.ref]}

    def projection(self, discovery, reverse=False):
        return {'steps': [dict(self.h.step_args(name='Закрыть текущую задачу'), local_ref='close'),
                          dict(self.h.step_args(name='Создать ручную задачу'), local_ref='create')],
                'transitions': [dict(explain_args({'source': '$create' if reverse else '$close',
                    'target': '$close' if reverse else '$create', 'condition': 'Текущая задача закрыта',
                    'evidence_status': 'observed', 'evidence': [self.h.ref]}), local_ref='next')],
                'operation_mappings': [{'operation_id': discovery['local_ids'][alias], 'node_id': '$'+alias}
                                       for alias in ('close', 'create')],
                'route_mappings': [{'route_id': discovery['route_expectations'][0]['id'], 'edge_ids': ['$next']}]}

    def review_args(self):
        review = deepcopy(self.state.model['execution_reviews'][0])
        result = {k: review[k] for k in ('id', 'name', 'evidence', 'operations', 'checks', 'route_expectations')}
        for row in result['operations'] + result['route_expectations']:
            row.pop('mapping_status', None)
        return result

    def test_discovered_order_precedes_nodes_and_survives_split(self):
        found = self.discover()
        self.assertEqual(self.state.model['nodes'], [])
        old = self.h.node(name='Обработать ручную задачу')
        patch = self.projection(found)
        patch['remove_node_ids'] = [old]
        self.h.good('patch_fragment', **patch)
        review = self.state.model['execution_reviews'][0]
        self.assertEqual(review['fact_revision'], 1)
        self.assertEqual(review['mapping_revision'], 2)
        self.assertEqual(review['route_expectations'][0]['from_operation_id'], found['local_ids']['close'])
        self.assertEqual(len(self.state.model['nodes']), 2)
        self.assertEqual(self.state._projection_issues(review), [])
        self.assertEqual(restore_state(self.state.workspace, export_state(self.state)).model, self.state.model)

    def test_reversed_split_rejected_transactionally_including_ids(self):
        found = self.discover()
        before, counters = deepcopy(self.state.model), self.state._counters.copy()
        rejected = self.state.dispatch('patch_fragment', self.projection(found, reverse=True))
        self.assertFalse(rejected['ok'], rejected)
        self.assertEqual(rejected['code'], 'execution_projection')
        self.assertIn('порядок', rejected['error'])
        self.assertEqual(before, self.state.model)
        self.assertEqual(counters, self.state._counters)

    def test_cannot_reverse_existing_edge_and_recertify_order(self):
        found = self.discover()
        patch = self.h.good('patch_fragment', **self.projection(found))
        result = self.state.dispatch('upsert_transition', {'id': patch['local_ids']['next'],
            'source': patch['local_ids']['create'], 'target': patch['local_ids']['close']})
        self.assertFalse(result['ok'])
        self.assertEqual(self.state.model['edges'][0]['source'], patch['local_ids']['close'])

    def test_wrong_early_discovery_can_be_corrected_with_grounded_amendment(self):
        self.discover()
        args = self.review_args()
        route = args['route_expectations'][0]
        route['from_operation_id'], route['to_operation_id'] = route['to_operation_id'], route['from_operation_id']
        result = self.state.dispatch('review_execution', args)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'fact_amendment_required')
        args['amendment'] = {'reason': 'Предыдущее прочтение порядка было ошибочным', 'evidence': [self.h.ref]}
        self.h.good('review_execution', **args)
        review = self.state.model['execution_reviews'][0]
        self.assertEqual(review['fact_revision'], 2)
        self.assertEqual(review['fact_changes'][0]['route_ids'], [route['id']])

    def test_mapping_cannot_rewrite_discovered_effect_or_condition(self):
        self.discover()
        for field in ('effect', 'condition', 'failure'):
            args = self.review_args()
            args['operations'][0][field] = 'Другой смысл'
            result = self.state.dispatch('review_execution', args)
            self.assertFalse(result['ok'], field)
            self.assertEqual(result['code'], 'fact_amendment_required')

    def test_write_category_cannot_hide_discovered_external_command(self):
        found = self.discover(external=True)
        patch = self.projection(found)
        rejected = self.state.dispatch('patch_fragment', patch)
        self.assertFalse(rejected['ok'])
        self.assertIn('внешнее обращение', rejected['error'])
        patch['steps'][0]['external_interactions'] = [self.call()]
        self.h.good('patch_fragment', **patch)
        args = self.review_args()
        args['operations'][0].pop('external_interactions')
        for category in ('integrations', 'state_changes'):
            args['checks'][category]['node_ids'] = [args['operations'][0]['node_id']]
        self.h.good('review_execution', **args)
        self.assertEqual(len(self.state.model['execution_reviews'][0]['operations'][0]['external_interactions']), 1)

    def test_two_discovered_actions_cannot_map_to_one_wrapper(self):
        found = self.discover()
        node = self.h.node()
        result = self.state.dispatch('patch_fragment', {'operation_mappings': [
            {'operation_id': item['id'], 'node_id': node} for item in found['operations']]})
        self.assertFalse(result['ok'])
        self.assertEqual(len(self.state.snapshot()['detail_review']['unmapped_action_ids']), 2)

    def test_unmapped_discovery_stays_partial_but_does_not_block_finish(self):
        found = self.discover()
        patch = self.projection(found)
        patch.pop('route_mappings')
        created = self.h.good('patch_fragment', **patch)
        self.h.good('set_scope', title='Ручная обработка', start='Получена задача', end='Создана ручная задача',
            entry_operation_ids=[created['local_ids']['close']], terminal_operation_ids=[created['local_ids']['create']])
        result = self.h.good('finish', summary='Порядок ещё не сопоставлен')
        self.assertEqual(result['status'], 'partial')
        self.assertIn(found['route_expectations'][0]['id'], self.state.snapshot()['detail_review']['unmapped_route_ids'])

    def test_scope_boundary_edits_keep_exclusions_and_require_reason(self):
        self.h.good('set_scope', title='Обращение', start='Получено обращение', end='Обработано обращение',
                    instance_unit='Одно обращение', exclusions=['Сбор пакета'])
        node = self.h.node()
        self.h.good('set_scope', title='Обращение', start='Получено обращение', end='Обработано обращение',
                    entry_operation_ids=[node], terminal_operation_ids=[node])
        self.assertEqual(self.state.model['scenario']['exclusions'], ['Сбор пакета'])
        changed = {'title': 'Обращение', 'start': 'Получено обращение', 'end': 'Обработано обращение', 'instance_unit': 'Пакет обращений'}
        self.assertFalse(self.state.dispatch('set_scope', changed)['ok'])
        changed['amendment'] = {'reason': 'Входом служит пакет', 'evidence': [self.h.ref]}
        self.h.good('set_scope', **changed)
        self.assertEqual(self.state.model['scenario']['scope_changes'][0]['fields'], ['instance_unit'])

    def test_shared_finalizer_needs_no_invented_outcome_or_gap(self):
        node = self.h.node(outcome_scope='shared', effect_kind='command')
        self.h.good('set_scope', title='Обращение', start='Получено обращение', end='Обработка завершена',
                    entry_operation_ids=[node], terminal_operation_ids=[node])
        issues = self.state.validation_signals()['coverage_issues']
        self.assertFalse(any('бизнес-исход конечной' in issue for issue in issues), issues)
        self.assertFalse(self.state.dispatch('upsert_step', {'id': node, 'outcome': 'success'})['ok'])
        self.assertEqual(self.state.model['gaps'], [])

    def test_service_only_fragment_does_not_require_artificial_action(self):
        self.h.good('review_execution', **self.h.inventory())
        self.assertEqual(self.state.model['execution_reviews'][0]['operations'], [])
        self.assertEqual(self.state.model['nodes'], [])

    def test_found_depth_cannot_disappear_by_deleting_whole_review(self):
        found = self.discover()
        rejected = self.state.dispatch('remove_element', {'kind': 'execution_review', 'id': found['id']})
        self.assertFalse(rejected['ok'])
        self.assertEqual(len(self.state.model['execution_reviews'][0]['operations']), 2)

    def test_mapping_revision_changes_for_unmapping_but_not_noop_or_reordering(self):
        found = self.discover()
        created = self.h.good('patch_fragment', **self.projection(found))
        review = self.state.model['execution_reviews'][0]
        revision = review['mapping_revision']
        self.h.good('patch_fragment', operation_mappings=[{'operation_id': found['local_ids']['close'], 'node_id': created['local_ids']['close']}])
        self.assertEqual(self.state.model['execution_reviews'][0]['mapping_revision'], revision)
        args = self.review_args()
        args['operations'].reverse()
        self.h.good('review_execution', **args)
        self.assertEqual(self.state.model['execution_reviews'][0]['fact_revision'], 1)
        self.assertEqual(self.state.model['execution_reviews'][0]['mapping_revision'], revision)
        self.h.good('patch_fragment', remove_edge_ids=[created['local_ids']['next']])
        self.assertEqual(self.state.model['execution_reviews'][0]['mapping_revision'], revision + 1)

    def test_empty_search_alternatives_cannot_share_wrong_destination_or_condition(self):
        args = self.h.inventory('', '', '')
        for row, alias in zip(args['operations'], ('search', 'refuse', 'manual')):
            row['local_ref'] = alias
        args['route_expectations'] = [{'condition': condition, 'expected_result': result,
            'from_operation_id': '$search', 'to_operation_id': '$'+target, 'evidence': [self.h.ref]}
            for condition, result, target in [('Пустой результат для стандартного платежа', 'Отказать', 'refuse'),
                                             ('Пустой результат для нестандартного платежа', 'Направить на ручную проверку', 'manual')]]
        args['checks']['branches'].update(status='present')
        found = self.h.good('review_execution', **args)
        steps = [dict(self.h.step_args(continuation_mode='choice' if alias == 'search' else 'sequence'), local_ref=alias)
                 for alias in ('search', 'refuse', 'manual')]
        edges = [dict(explain_args({'source': '$search', 'target': '$'+target, 'condition': row['condition'],
                    'kind': 'branch', 'evidence_status': 'observed', 'evidence': [self.h.ref]}), local_ref=target+'Edge')
                 for row, target in zip(args['route_expectations'], ('refuse', 'manual'))]
        patch = {'steps': steps, 'transitions': edges,
                 'operation_mappings': [{'operation_id': found['local_ids'][alias], 'node_id': '$'+alias} for alias in ('search', 'refuse', 'manual')],
                 'route_mappings': [{'route_id': row['id'], 'edge_ids': ['$'+target+'Edge']}
                                    for row, target in zip(found['route_expectations'], ('refuse', 'manual'))]}
        wrong = deepcopy(patch)
        wrong['transitions'][0]['target'] = '$manual'
        self.assertFalse(self.state.dispatch('patch_fragment', wrong)['ok'])
        wrong = deepcopy(patch)
        wrong['transitions'][0]['condition'] = 'Результат поиска отсутствует'
        self.assertFalse(self.state.dispatch('patch_fragment', wrong)['ok'])
        created = self.h.good('patch_fragment', **patch)
        self.h.good('patch_fragment', remove_edge_ids=[created['local_ids']['refuseEdge']])
        self.assertEqual(len(self.state.snapshot()['detail_review']['unmapped_route_ids']), 1)
        self.assertEqual(len(self.state.model['execution_reviews'][0]['route_expectations']), 2)


if __name__ == '__main__':
    unittest.main()
