"""Regression cases from divergent maps; no claim of live-model completeness."""
from copy import deepcopy
import unittest

import test_research_contracts as helpers
from quality_helpers import explain_args, review_payload
from pko.scenario_map.checkpoint_state import export_state, restore_state


class MapStabilityContractTests(unittest.TestCase):
    def setUp(self):
        self.h = helpers.ResearchContractTests()
        self.h.setUp()
        self.state = self.h.state

    def edge(self, source, target, **extra):
        return self.h.good('upsert_transition', **explain_args({
            'source': source, 'target': target, 'evidence': [self.h.ref],
            'evidence_status': 'observed', **extra}))['id']

    def route(self, condition='Реквизиты отсутствуют', **extra):
        return {'condition': condition, 'expected_result': 'Обращение направлено на ручную проверку',
                'evidence': [self.h.ref], **extra}

    def scope(self, start, end):
        self.h.good('set_scope', title='Обработка обращения', start='Получено обращение', end='Получен результат',
                    entry_operation_ids=[start], terminal_operation_ids=[end])

    def test_replace_terminal_and_entry_in_one_atomic_patch(self):
        old = self.h.node()
        self.scope(old, old)
        result = self.h.good('patch_fragment', remove_node_ids=[old],
            steps=[dict(self.h.step_args(name='Отправить решение'), local_ref='send'),
                   dict(self.h.step_args(name='Закрыть обращение'), local_ref='close')],
            transitions=[explain_args({'source': '$send', 'target': '$close', 'evidence_status': 'observed', 'evidence': [self.h.ref]})],
            entry_operation_ids=['$send'], terminal_operation_ids=['$close'])
        self.assertEqual(self.state.model['scenario']['entry_operation_ids'], [result['local_ids']['send']])
        self.assertEqual(self.state.model['scenario']['terminal_operation_ids'], [result['local_ids']['close']])
        self.assertEqual(len(self.state.model['nodes']), 2)

    def test_invalid_boundary_rolls_back_new_nodes_and_counters(self):
        old = self.h.node()
        self.scope(old, old)
        before, counters = deepcopy(self.state.model), self.state._counters.copy()
        result = self.state.dispatch('patch_fragment', {'steps': [self.h.step_args()],
                                                       'terminal_operation_ids': ['$missing']})
        self.assertFalse(result['ok'])
        self.assertEqual(result['details']['issues'][0]['field'], 'terminal_operation_ids')
        self.assertEqual(self.state.model, before)
        self.assertEqual(self.state._counters, counters)

    def test_boundary_only_patch_and_unknown_deleted_boundary(self):
        left, right = self.h.node(), self.h.node()
        self.scope(left, left)
        self.h.good('patch_fragment', terminal_operation_ids=[right])
        result = self.state.dispatch('patch_fragment', {'remove_node_ids': [right]})
        self.assertFalse(result['ok'])
        self.assertEqual(result['details']['issues'][0]['unknown_ids'], [right])
        self.assertEqual(self.state.model['scenario']['terminal_operation_ids'], [right])

    def test_collect_independent_patch_errors_not_dependent_alias_cascade(self):
        result = self.state.dispatch('patch_fragment', {
            'steps': [{'name': '', 'local_ref': 'bad'}, {'name': '', 'local_ref': 'other'}],
            'transitions': [{'source': '$bad', 'target': '$other'}]})
        self.assertFalse(result['ok'])
        self.assertEqual([p['field'] for p in result['details']['issues']], ['steps[0]', 'steps[1]'])
        self.assertEqual(self.state.model['nodes'], [])

    def test_omitting_found_action_does_not_erase_depth(self):
        node = self.h.node()
        review = self.h.good('review_execution', **self.h.inventory(node, ''))
        missing = review['operations'][1]['id']
        result = self.h.good('review_execution', **self.h.inventory(node, id=review['id']))
        self.assertIn(missing, result['retained_operation_ids'])
        self.assertIn(missing, self.state.snapshot()['detail_review']['unmapped_action_ids'])
        self.assertEqual(len(self.state.model['execution_reviews'][0]['operations']), 2)

    def test_omitted_mapped_action_is_not_silently_recertified(self):
        one, two = self.h.node(), self.h.node()
        old = self.h.good('review_execution', **self.h.inventory(one, two))
        self.h.good('review_execution', **self.h.inventory(one, id=old['id']))
        row = self.state.model['execution_reviews'][0]['operations'][1]
        self.assertEqual(row['node_id'], '')
        self.assertEqual(row['id'], old['operations'][1]['id'])

    def test_known_unmapped_action_can_be_repeated_without_duplication(self):
        old = self.h.good('review_execution', **self.h.inventory(''))
        new = self.h.good('review_execution', **self.h.inventory('', id=old['id']))
        self.assertEqual(old['operations'], new['operations'])

    def test_false_discovery_requires_explicit_grounded_discard(self):
        old = self.h.good('review_execution', **self.h.inventory(''))
        action = old['operations'][0]['id']
        payload = self.h.inventory(id=old['id'], discard_operations=[{'id': action, 'reason': 'Это упаковка аргументов одного действия', 'evidence': []}])
        self.assertFalse(self.state.dispatch('review_execution', payload)['ok'])
        payload['discard_operations'][0]['evidence'] = [self.h.ref]
        self.h.good('review_execution', **payload)
        saved = self.state.model['execution_reviews'][0]
        self.assertEqual(saved['operations'], [])
        self.assertEqual(saved['discarded_operations'][0]['id'], action)

    def test_missing_branch_survives_review_update_and_partial_finish(self):
        node = self.h.node()
        self.scope(node, node)
        old = self.h.good('review_execution', **self.h.inventory(node, route_expectations=[self.route()]))
        expectation = old['route_expectations'][0]['id']
        self.h.good('review_execution', **self.h.inventory(node, id=old['id']))
        self.assertIn(expectation, self.state.snapshot()['detail_review']['unmapped_route_ids'])
        inspected = self.h.good('inspect_map', section='route_expectations')
        self.assertEqual(inspected['items'][0]['condition'], 'Реквизиты отсутствуют')
        final = self.h.good('finish', summary='Один из маршрутов ещё не построен')
        self.assertEqual(final['status'], 'partial')
        self.assertEqual(self.state.validation_signals()['structural_issues'], [])

    def test_alternatives_get_stable_distinct_ids_and_real_mapping(self):
        left, refusal, manual = self.h.node(continuation_mode='choice'), self.h.node(), self.h.node()
        first = self.edge(left, refusal, kind='branch', condition='Стандартный платёж')
        second = self.edge(left, manual, kind='branch', condition='Нестандартный платёж')
        routes = [self.route('Стандартный платёж', edge_ids=[first]), self.route('Нестандартный платёж', edge_ids=[second])]
        payload = self.h.inventory(left, refusal, manual, route_expectations=routes)
        payload['checks']['branches'].update(status='present', node_ids=[left], edge_ids=[first, second])
        result = self.h.good('review_execution', **payload)
        payload['id'] = result['id']
        again = self.h.good('review_execution', **payload)
        self.assertEqual(result['route_expectations'], again['route_expectations'])
        self.assertNotEqual(result['route_expectations'][0]['id'], result['route_expectations'][1]['id'])
        self.assertEqual(self.state.snapshot()['detail_review']['unmapped_route_ids'], [])

    def test_removed_edge_reopens_found_route_and_survives_scope_change(self):
        left, right = self.h.node(), self.h.node()
        edge = self.edge(left, right)
        result = self.h.good('review_execution', **self.h.inventory(left, right, route_expectations=[self.route(edge_ids=[edge])]))
        changed = self.h.good('patch_fragment', remove_edge_ids=[edge])
        self.assertIn(result['id'], changed['stale_review_ids'])
        route = self.state.model['execution_reviews'][0]['route_expectations'][0]
        self.assertEqual(route['mapping_status'], 'unmapped')
        self.assertEqual(route['edge_ids'], [])

    def test_unchanged_name_does_not_stale_review_but_result_does(self):
        node = self.h.node()
        review = self.h.good('review_execution', **self.h.inventory(node))
        response = self.h.good('upsert_step', id=node, name='Проверка сведений')
        self.assertNotIn(review['id'], response['stale_review_ids'])
        response = self.h.good('upsert_step', id=node, output='Другой предметный результат')
        self.assertIn(review['id'], response['stale_review_ids'])

    def test_semantic_review_reports_multiple_independent_fields(self):
        left, right = self.h.node(), self.h.node()
        self.scope(left, right)
        self.edge(left, right)
        payload = review_payload(self.state)
        payload['checks']['outcomes']['node_ids'] = []
        payload['checks']['atomicity']['node_ids'] = []
        payload['checks']['routing']['edge_ids'] = []
        response = self.state.dispatch('review_semantics', payload)
        self.assertFalse(response['ok'])
        self.assertEqual({p['field'] for p in response['details']['issues']},
                         {'checks.outcomes', 'checks.atomicity', 'checks.routing'})
        self.assertIsNone(self.state.model['semantic_review'])
        self.h.good('review_semantics', **review_payload(self.state))

    def test_two_external_commands_need_two_discovered_actions_even_same_addressee(self):
        interaction = {'name': 'Обработка обращений', 'purpose': 'Передать решение',
                       'direction': 'send', 'contract': 'send', 'evidence': [self.h.ref]}
        node = self.h.node(external_interactions=[interaction])
        payload = self.h.inventory(node, node)
        for row in payload['operations']:
            row['kind'] = 'integration'
        self.assertFalse(self.state.dispatch('review_execution', payload)['ok'])
        payload['operations'][1].update(node_id='', evidence=[self.h.ref], name='Закрытие обращения',
                                        action='Закрыть обращение', effect='Обращение закрыто')
        payload['checks']['integrations'].update(status='present', node_ids=[node])
        self.h.good('review_execution', **payload)
        self.assertEqual(len(self.state.snapshot()['detail_review']['unmapped_action_ids']), 1)
        self.assertEqual(len(self.state.model['nodes']), 1)

    def test_compound_recipient_cannot_cover_two_discovered_commands(self):
        node = self.h.node(external_interactions=[{'name': 'Платежи/Обращения', 'purpose': 'Передать решение',
            'direction': 'send', 'contract': 'send', 'evidence': [self.h.ref]}])
        payload = self.h.inventory(node, node)
        for row in payload['operations']:
            row['kind'] = 'integration'
        payload['operations'][0].update(name='Передача платежа', action='Передать платёж', effect='Платёж передан')
        payload['operations'][1].update(name='Закрытие обращения', action='Закрыть обращение', effect='Обращение закрыто')
        result = self.state.dispatch('review_execution', payload)
        self.assertFalse(result['ok'])
        self.assertIn('несколько действий', result['error'])

    def test_pending_condition_keeps_mapped_task_partial(self):
        node = self.h.node()
        review = self.h.good('review_execution', **self.h.inventory(node, route_expectations=[self.route()]))
        task = self.h.task(node_ids=[node])
        coverage = next(t for t in self.state.research_coverage()['tasks'] if t['task_id'] == task)
        self.assertEqual(coverage['status'], 'partial')
        self.assertIn(review['route_expectations'][0]['id'], ' '.join(coverage['local_issues']))

    def test_slash_in_real_recipient_name_is_not_rejected_by_heuristic(self):
        node = self.h.node(external_interactions=[{'name': 'Платежи/обращения', 'purpose': 'Получить решение',
            'direction': 'request', 'contract': 'read', 'evidence': [self.h.ref]}])
        self.assertEqual(self.state.model['nodes'][0]['id'], node)

    def test_expectation_with_unread_evidence_rolls_back(self):
        before = deepcopy(self.state.model)
        route = self.route(evidence=[{**self.h.ref, 'line_end': 999}])
        result = self.state.dispatch('review_execution', self.h.inventory('', route_expectations=[route]))
        self.assertFalse(result['ok'])
        self.assertEqual(self.state.model, before)

    def test_rediscovered_condition_can_be_discarded_again_and_resumed(self):
        result = self.h.good('review_execution', **self.h.inventory('', route_expectations=[self.route()]))
        review_id = result['id']
        route_id = result['route_expectations'][0]['id']
        for reason in ('Условие относится к другому варианту', 'Подтверждено другое назначение условия'):
            self.h.good('review_execution', **self.h.inventory('', id=review_id,
                discard_route_expectations=[{'id': route_id, 'reason': reason, 'evidence': [self.h.ref]}]))
            restored = restore_state(self.state.workspace, export_state(self.state))
            self.assertEqual(restored.model, self.state.model)
            if reason == 'Условие относится к другому варианту':
                result = self.h.good('review_execution', **self.h.inventory('', id=review_id, route_expectations=[self.route()]))
                self.assertEqual(result['route_expectations'][0]['id'], route_id)
        review = self.state.model['execution_reviews'][0]
        self.assertEqual(len(review['discarded_route_expectations']), 1)
        self.assertEqual(review['discarded_route_expectations'][0]['reason'], 'Подтверждено другое назначение условия')
        self.assertEqual(review['discarded_route_expectations_omitted'], 1)

    def test_replace_node_moves_gap_explicitly_and_keeps_stale_unknown_review(self):
        node = self.h.node()
        self.scope(node, node)
        gap = self.h.good('add_gap', category='unexplored', description='Продолжение требует исследования',
                          node_ids=[node], evidence=[self.h.ref])['id']
        payload = self.h.inventory(node)
        payload['checks']['branches'].update(status='unknown', gap_ids=[gap])
        review = self.h.good('review_execution', **payload)
        previous_gap = deepcopy(self.state.model['gaps'][0])
        result = self.h.good('patch_fragment', steps=[dict(self.h.step_args(), local_ref='replacement')],
            remove_node_ids=[node], entry_operation_ids=['$replacement'], terminal_operation_ids=['$replacement'],
            gap_updates=[{'id': gap, 'node_ids': ['$replacement']}])
        saved = self.state.model['gaps'][0]
        self.assertEqual(saved, {**previous_gap, 'node_ids': [result['local_ids']['replacement']]})
        self.assertEqual(result['updated_gap_ids'], [gap])
        self.assertIn(review['id'], result['stale_review_ids'])
        self.assertEqual(self.state.validation_signals()['structural_issues'], [])
        restored = restore_state(self.state.workspace, export_state(self.state))
        self.assertEqual(restored.model, self.state.model)
        self.assertEqual(self.h.good('finish', summary='Продолжение ещё исследуется')['status'], 'partial')

    def test_replace_transition_moves_gap_to_new_transition_alias(self):
        left, right = self.h.node(), self.h.node()
        old = self.edge(left, right)
        gap = self.h.good('add_gap', category='unresolved_transition', description='Условие требует уточнения',
                          edge_ids=[old], evidence=[self.h.ref])['id']
        result = self.h.good('patch_fragment', steps=[dict(self.h.step_args(), local_ref='middle')],
            transitions=[explain_args({'source': left, 'target': '$middle', 'local_ref': 'to_middle',
                                      'evidence': [self.h.ref], 'evidence_status': 'observed'}),
                         explain_args({'source': '$middle', 'target': right,
                                      'evidence': [self.h.ref], 'evidence_status': 'observed'})],
            remove_edge_ids=[old], gap_updates=[{'id': gap, 'edge_ids': ['$to_middle']}])
        self.assertEqual(self.state.model['gaps'][0]['edge_ids'], [result['local_ids']['to_middle']])
        self.assertEqual(restore_state(self.state.workspace, export_state(self.state)).model, self.state.model)

    def test_gap_reference_kinds_and_all_anchors_are_checked_atomically(self):
        left, right = self.h.node(), self.h.node()
        gap = self.h.good('add_gap', category='unexplored', description='Продолжение неизвестно',
                          node_ids=[left], evidence=[self.h.ref])['id']
        for update in ({'node_ids': ['$link']}, {'edge_ids': ['$node']}, {'node_ids': []},
                       {'node_ids': ['$node'], 'description': 'Неявно закрыт'}):
            with self.subTest(update=update):
                before, counters = deepcopy(self.state.model), self.state._counters.copy()
                response = self.state.dispatch('patch_fragment', {
                    'steps': [dict(self.h.step_args(), local_ref='node')],
                    'transitions': [explain_args({'source': left, 'target': right, 'local_ref': 'link',
                                                 'evidence': [self.h.ref], 'evidence_status': 'observed'})],
                    'gap_updates': [{'id': gap, **update}]})
                self.assertFalse(response['ok'])
                self.assertEqual(response['details']['issues'][0]['field'], 'gap_updates[0]')
                self.assertEqual(self.state.model, before)
                self.assertEqual(self.state._counters, counters)

    def test_gap_update_preserves_task_anchor_evidence_and_category(self):
        node = self.h.node()
        task = self.h.task('open')
        gap = self.h.good('add_gap', category='unexplored', description='Исследовать дочерний обработчик',
                          node_ids=[node], task_ids=[task], evidence=[self.h.ref])['id']
        original = deepcopy(self.state.model['gaps'][0])
        self.h.good('patch_fragment', remove_node_ids=[node], gap_updates=[{'id': gap, 'node_ids': []}])
        self.assertEqual(self.state.model['gaps'][0], {**original, 'node_ids': []})

    def test_transition_alias_cannot_be_used_as_source_or_boundary(self):
        left, right = self.h.node(), self.h.node()
        edge = explain_args({'source': left, 'target': right, 'local_ref': 'edge',
                             'evidence': [self.h.ref], 'evidence_status': 'observed'})
        before = deepcopy(self.state.model)
        response = self.state.dispatch('patch_fragment', {'transitions': [edge], 'entry_operation_ids': ['$edge']})
        self.assertFalse(response['ok'])
        self.assertEqual(self.state.model, before)
        response = self.state.dispatch('patch_fragment', {'transitions': [edge,
            explain_args({'source': '$edge', 'target': right, 'evidence': [self.h.ref], 'evidence_status': 'observed'})]})
        self.assertFalse(response['ok'])
        self.assertEqual(self.state.model, before)


if __name__ == '__main__':
    unittest.main()
