"""Review checks stay within their fragment and recorded graph revision."""
import unittest

import test_research_contracts as contracts
from quality_helpers import explain_args


class ReviewValidationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.h = contracts.ResearchContractTests()
        self.h.setUp()
        self.state = self.h.state

    def edge(self, source, target, **extra):
        return self.h.good('upsert_transition', **explain_args({
            'source': source, 'target': target, 'evidence_status': 'observed',
            'evidence': [self.h.ref], 'condition': 'Результат получен', **extra}))['id']

    def external(self):
        return {'name': 'Сервис сведений об обращениях', 'purpose': 'Запросить сведения об обращении',
                'direction': 'request', 'endpoint': 'POST /cases',
                'contract': 'Запрос сведений по номеру обращения', 'evidence': [self.h.ref]}

    def reviewed_leaf(self):
        # Only the child owns this action. A dispatcher is reviewed over the
        # larger independent region, with no local actions of its own.
        ref = {**self.h.ref, 'line_start': 2}
        node = self.h.node(evidence=[ref])
        self.h.good('review_execution', **self.h.inventory(node, evidence=[ref]))
        task = self.h.good('save_research_task', target='Дочерний обработчик', kind='fragment', status='mapped',
                           resolution='Действие отражено на карте', evidence=[ref], node_ids=[node])['id']
        return node, task

    def assert_partial_allowed(self):
        self.assertEqual(self.state.validation_signals()['structural_issues'], [])
        result = self.h.good('finish', summary='Частичная карта; неисследованные участки сохранены')
        self.assertEqual(result['status'], 'partial')

    def test_incoming_route_does_not_claim_behaviour_inside_target_review(self):
        for kind in ('branch', 'error', 'retry', 'call', 'event', 'return'):
            with self.subTest(kind=kind):
                self.setUp()
                source, target = self.h.node(), self.h.node(name='Сохранение обращения')
                self.edge(source, target, kind=kind)
                self.h.good('review_execution', **self.h.inventory(target))
                self.assert_partial_allowed()

    def test_incoming_source_integration_does_not_belong_to_target_review(self):
        source = self.h.node(external_interactions=[self.external()])
        target = self.h.node(name='Сохранение обращения')
        self.edge(source, target)
        self.h.good('review_execution', **self.h.inventory(target))
        self.assert_partial_allowed()

    def test_terminal_integration_is_checked_without_incident_edges(self):
        node = self.h.node(external_interactions=[self.external()])
        self.h.good('review_execution', **self.h.inventory(node))
        self.assertTrue(any('.integrations:' in issue for issue in self.state.validation_signals()['structural_issues']))
        self.assertFalse(self.state.dispatch('finish', {'summary': 'Частичный результат'})['ok'])

    def test_current_review_still_blocks_outgoing_contradiction(self):
        source, target = self.h.node(), self.h.node(name='Сохранение обращения')
        self.edge(source, target, kind='branch')
        self.h.good('review_execution', **self.h.inventory(source))
        self.assertTrue(any('.branches:' in issue for issue in self.state.validation_signals()['structural_issues']))
        self.assertFalse(self.state.dispatch('finish', {'summary': 'Частичный результат'})['ok'])

    def test_new_routes_invalidate_review_without_blocking_partial(self):
        source = self.h.node(continuation_mode='choice')
        accepted, rejected = self.h.node(outcome='success'), self.h.node(outcome='refusal')
        self.h.good('review_execution', **self.h.inventory(source, accepted, rejected))
        task = self.h.task(node_ids=[source, accepted, rejected])
        self.edge(source, accepted, condition='Обращение принято')
        self.edge(source, rejected, condition='Обращение отклонено')
        self.h.good('set_scope', title='Обработка обращения', start='Получено обращение', end='Обращение обработано',
                    entry_operation_ids=[source], terminal_operation_ids=[accepted, rejected])
        signals = self.state.validation_signals()
        self.assertTrue(any('после обзора' in issue for issue in signals['coverage_issues']))
        row = next(row for row in self.state.research_coverage()['tasks'] if row['task_id'] == task)
        self.assertEqual(row['status'], 'partial')
        self.assert_partial_allowed()

    def test_changed_kind_evidence_invalidates_old_review(self):
        node = self.h.node(external_interactions=[self.external()])
        payload = self.h.inventory(node)
        payload['operations'][0]['kind'] = 'integration'
        payload['checks']['integrations'].update(status='present', node_ids=[node])
        self.h.good('review_execution', **payload)
        self.h.good('upsert_step', id=node, external_interactions=[])
        signals = self.state.validation_signals()
        self.assertTrue(any('изменена после обзора' in issue for issue in signals['coverage_issues']))
        self.assert_partial_allowed()

    def test_renaming_does_not_bypass_current_kind_contradiction(self):
        node = self.h.node()
        payload = self.h.inventory(node)
        payload['operations'][0]['kind'] = 'integration'
        payload['checks']['integrations'].update(status='present', node_ids=[node])
        self.h.good('review_execution', **payload)
        self.h.good('upsert_step', id=node, name='Получение сведений об обращении')
        self.assertTrue(any('external_interactions' in issue for issue in self.state.validation_signals()['structural_issues']))
        self.assertFalse(self.state.dispatch('finish', {'summary': 'Частичный результат'})['ok'])

    def test_refreshing_stale_review_restores_strict_checks(self):
        source, target = self.h.node(), self.h.node(name='Сохранение обращения')
        review = self.h.good('review_execution', **self.h.inventory(source))
        self.edge(source, target, kind='branch')
        self.assertEqual(self.state.validation_signals()['structural_issues'], [])
        self.h.good('review_execution', **self.h.inventory(source, id=review['id']))
        self.assertTrue(any('.branches:' in issue for issue in self.state.validation_signals()['structural_issues']))

    def test_empty_circular_delegation_is_partial_without_blocking_finish(self):
        left, right = self.h.task('open'), self.h.task('open')
        self.h.good('review_execution', **self.h.inventory())
        self.h.good('save_research_task', id=left, status='mapped', child_task_ids=[right], resolution='Работа делегирована')
        self.h.good('save_research_task', id=right, status='mapped', child_task_ids=[left], resolution='Работа делегирована')
        coverage = self.state.research_coverage()
        self.assertEqual(coverage['status'], 'partial')
        for row in coverage['tasks']:
            self.assertEqual(row['status'], 'partial')
            self.assertIn('круговая делегация без достижимого отображённого действия', row['local_issues'])
        self.assertEqual(self.state.model['nodes'], [])
        self.assert_partial_allowed()

    def test_delegation_cycle_can_reach_a_mapped_action(self):
        node, leaf = self.reviewed_leaf()
        left, right = self.h.task('open'), self.h.task('open')
        self.h.good('review_execution', **self.h.inventory())
        self.h.good('save_research_task', id=left, status='mapped', child_task_ids=[right], resolution='Работа делегирована')
        self.h.good('save_research_task', id=right, status='mapped', child_task_ids=[left, leaf], resolution='Работа делегирована')
        coverage = self.state.research_coverage()
        self.assertTrue(all(row['status'] == 'covered' for row in coverage['tasks']))
        self.assertTrue(all(not row['action_ids'] for row in coverage['tasks'] if row['task_id'] in (left, right)))
        self.assertEqual(len(self.state.model['nodes']), 1)

    def test_empty_dispatcher_chain_uses_child_action_without_wrapper(self):
        node, leaf = self.reviewed_leaf()
        middle = self.h.task(child_task_ids=[leaf])
        root = self.h.task(child_task_ids=[middle])
        self.h.good('review_execution', **self.h.inventory())
        coverage = self.state.research_coverage()
        self.assertEqual(coverage['status'], 'covered')
        self.assertTrue(all(row['status'] == 'covered' for row in coverage['tasks']))
        self.assertTrue(all(not row['action_ids'] for row in coverage['tasks'] if row['task_id'] in (root, middle)))
        self.assertEqual(next(row for row in coverage['tasks'] if row['task_id'] == root)['dependency_ids'], [middle])
        self.assertEqual(len(self.state.model['nodes']), 1)


if __name__ == '__main__':
    unittest.main()
