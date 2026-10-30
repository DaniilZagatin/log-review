"""Synthetic basket hierarchy: display projection is not a new business occurrence."""
import copy
import unittest

from test_scenario_graph import graph_bundle


def basket():
    bundle = graph_bundle([('checkout', 'Оформление')],
                          [('open', 'checkout', 'operation', 'Открыть оформление'),
                           ('add', 'checkout', 'operation', 'Добавить товар'),
                           ('delete', 'checkout', 'operation', 'Удалить товар'),
                           ('quantity', 'checkout', 'operation', 'Изменить количество'),
                           ('confirm', 'checkout', 'operation', 'Подтвердить заказ')],
                          [('a', 'open', 'add', None), ('b', 'add', 'quantity', 'Количество изменено'),
                           ('c', 'quantity', 'delete', 'Товар больше не нужен'),
                           ('d', 'quantity', 'confirm', 'Оформить заказ'),
                           ('d-copy', 'quantity', 'confirm', 'Оформить заказ')], [])
    bundle['atomic_map'] = {'groups': [{'id': 'basket', 'name': 'Управление составом корзины',
                                      'stage_id': 'checkout', 'operation_ids': ['add', 'delete', 'quantity'],
                                      'request_id': 'compose'}],
                            'requests': [{'id': 'compose', 'kind': 'compose', 'outcome': 'accepted'}],
                            'compose': {'status': 'complete', 'request_id': 'compose'}}
    return bundle


def view(bundle, expanded=()):
    from process_observability.operation_hierarchy import hierarchy_view
    return hierarchy_view(bundle, expanded)


class OperationHierarchyTests(unittest.TestCase):
    def test_collapsed_block_inside_stage_preserves_children_and_edge_origins(self):
        bundle = basket()
        original = copy.deepcopy(bundle)
        result = view(bundle)
        self.assertEqual({x['id'] for x in result['nodes']}, {'op:open', 'group:basket', 'op:confirm'})
        [group] = result['groups']
        self.assertEqual(group['operation_ids'], ['add', 'delete', 'quantity'])
        self.assertEqual(group['internal_transition_ids'], ['b', 'c'])
        self.assertEqual(result['operation_count'], 5)
        self.assertEqual(len(result['edges']), 2)
        [out] = [edge for edge in result['edges'] if edge['from'] == 'group:basket']
        self.assertEqual(out['to'], 'op:confirm')
        self.assertEqual(out['transition_ids'], ['d', 'd-copy'])
        self.assertEqual(out['from_operation'], 'quantity')
        self.assertEqual(bundle, original)

    def test_expansion_restores_same_atomic_endpoints_with_no_added_parent_operation(self):
        expanded = view(basket(), ['basket'])
        self.assertEqual({x['id'] for x in expanded['nodes']},
                         {'op:open', 'op:add', 'op:delete', 'op:quantity', 'op:confirm'})
        self.assertEqual(len(expanded['edges']), 4)
        self.assertTrue(all(edge['from'].startswith('op:') and edge['to'].startswith('op:')
                            for edge in expanded['edges']))
        self.assertEqual(expanded['groups'][0]['visible_member_ids'], ['op:add', 'op:delete', 'op:quantity'])

    def test_overlap_quarantines_all_conflicting_groups_without_losing_any_action(self):
        bundle = basket()
        bundle['atomic_map']['groups'].append({'id': 'other', 'name': 'Другой блок', 'stage_id': 'checkout',
                                               'operation_ids': ['quantity', 'confirm'], 'request_id': 'compose'})
        result = view(bundle)
        self.assertFalse(result['groups'])
        self.assertEqual(len(result['nodes']), 5)
        self.assertTrue(any(x['code'] == 'overlapping_groups' for x in result['issues']))

    def test_empty_foreign_stage_unknown_child_and_unbound_groups_remain_unresolved(self):
        for edit in (lambda g: g.update(operation_ids=[]),
                     lambda g: g.update(operation_ids=['missing']),
                     lambda g: g.update(stage_id='other'),
                     lambda g: g.update(request_id='not-accepted'),
                     lambda g: g.update(request_id=['compose']),
                     lambda g: g.update(stage_id=['checkout'])):
            bundle = basket()
            edit(bundle['atomic_map']['groups'][0])
            result = view(bundle)
            self.assertFalse(result['groups'])
            self.assertEqual(len(result['nodes']), 5)
            self.assertTrue(result['issues'])

    def test_distinct_conditions_or_evidence_never_merge_while_collapsing(self):
        for key, value in [('condition', 'Другой исход'), ('evidence_ids', ['other'])]:
            bundle = basket()
            bundle['process_map']['operation_transitions'][-1][key] = value
            self.assertEqual(len(view(bundle)['edges']), 3)

    def test_multiple_business_blocks_in_same_stage_expand_independently(self):
        bundle = basket()
        bundle['atomic_map']['groups'].append({'id': 'order', 'name': 'Создание заказа', 'stage_id': 'checkout',
                                               'operation_ids': ['open', 'confirm'], 'request_id': 'compose'})
        result = view(bundle, ['basket'])
        self.assertIn('group:order', {x['id'] for x in result['nodes']})
        self.assertNotIn('group:basket', {x['id'] for x in result['nodes']})
        self.assertEqual(result['operation_count'], 5)

    def test_legacy_coarse_label_never_invents_children(self):
        bundle = basket()
        del bundle['atomic_map']
        self.assertEqual(len(view(bundle)['nodes']), 5)
        self.assertFalse(view(bundle)['groups'])

    def test_every_duplicate_anchor_is_available_to_reveal_hidden_children(self):
        bundle = basket()
        bundle['process_map']['operation_transitions'].append(
            {**bundle['process_map']['operation_transitions'][1], 'id': 'internal-copy'})
        edge = next(e for e in view(bundle, ['basket'])['edges'] if e['id'] == 'b')
        self.assertEqual(edge['anchors'], ['transition-2', 'transition-6'])

    def test_non_records_do_not_shift_links_relative_to_report_cards(self):
        bundle = basket()
        bundle['process_map']['operation_transitions'].insert(0, None)
        edge = view(bundle, ['basket'])['edges'][0]
        self.assertEqual(edge['anchor'], 'transition-1')
        self.assertEqual(edge['anchors'], ['transition-1'])

    def test_explicit_null_optional_lists_leave_available_operations_visible(self):
        for area, field in [('process_map', 'operations'), ('process_map', 'operation_transitions'),
                            ('process_map', 'stages'), ('atomic_map', 'requests')]:
            bundle = basket()
            bundle[area][field] = None
            with self.subTest(area=area, field=field):
                result = view(bundle)
                self.assertIsInstance(result['nodes'], list)

    def test_failed_compose_and_single_member_do_not_fabricate_containers(self):
        for change in (lambda b: b['atomic_map']['compose'].update(status='failed'),
                       lambda b: b['atomic_map']['compose'].update(request_id='foreign'),
                       lambda b: b['atomic_map']['groups'][0].update(operation_ids=['add'])):
            bundle = basket()
            change(bundle)
            result = view(bundle)
            self.assertFalse(result['groups'])
            self.assertEqual(len(result['nodes']), 5)
            self.assertTrue(result['issues'])


if __name__ == '__main__':
    unittest.main()
