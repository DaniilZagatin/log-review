"""Lossless map presentation: exact duplicate edges, typed clickable elements and expandable blocks."""
import copy
import re
import unittest

from process_observability.report import build_html
from test_scenario_graph import graph_bundle


def fixture():
    return graph_bundle([('a', 'Приём'), ('b', 'Обработка')],
                        [('one', 'a', 'operation', 'Принять'),
                         ('two', 'b', 'decision', 'Проверить'),
                         ('three', 'b', 'end', 'Завершить')],
                        [('t1', 'one', 'two', 'Есть платёж'),
                         ('t2', 'one', 'two', 'Есть платёж'),
                         ('t3', 'two', 'three', 'Проверка пройдена')],
                        [('s1', 'Первый путь', ['one', 'two', 'three'], ['t1', 't3']),
                         ('s2', 'Второй путь', ['one', 'two'], ['t2'])])


class MapClarityTests(unittest.TestCase):
    def test_exact_duplicates_draw_once_with_union_of_scenarios_and_raw_records(self):
        bundle = fixture()
        before = copy.deepcopy(bundle)
        page = build_html(bundle)
        self.assertEqual(page.count('class="scenario-edge"'), 2)
        self.assertIn('data-scenarios="1 2"', page)
        self.assertIn('Исходных записей: 2', page)
        for identifier in ('t1', 't2', 't3'):
            self.assertIn(identifier, page)
        self.assertEqual(bundle, before)

    def test_different_condition_or_evidence_is_never_silently_merged(self):
        for field, value in [('condition', 'Другой исход'), ('evidence_ids', ['other-source'])]:
            bundle = fixture()
            bundle['process_map']['operation_transitions'][1][field] = value
            with self.subTest(field=field):
                self.assertEqual(build_html(bundle).count('class="scenario-edge"'), 3)

    def test_conditions_and_unlabelled_edges_open_explanations(self):
        bundle = fixture()
        bundle['process_map']['operation_transitions'][2]['condition'] = None
        page = build_html(bundle)
        self.assertIn('Условие: Есть платёж', page)
        self.assertRegex(page, r'<a[^>]+href="#transition-1"[^>]*>.*?edge-caption')
        self.assertIn('id="transition-1"', page)
        self.assertIn('id="transition-3"', page)
        self.assertIn('Условие перехода не установлено', page)
        self.assertIn('Принять', page)
        self.assertIn('Проверить', page)

    def test_blocks_expand_individually_and_detail_control_expands_all_same_blocks(self):
        from test_operation_hierarchy import basket
        page = build_html(basket())
        self.assertIn('class="hierarchy-diagram"', page)
        self.assertIn('data-hierarchy=', page)
        self.assertNotIn('class="business-block"', page)  # hierarchy is ON the SVG, not separate accordions
        self.assertIn('data-level-view="stages operations"', page)
        self.assertIn('Подробно', page)
        self.assertIn('Управление составом корзины', page)
        self.assertIn('Добавить товар', page)
        self.assertIn('Удалить товар', page)
        self.assertIn('Изменить количество', page)
        for i in (1, 2, 3):
            self.assertIn(f'href="#op-{i}"', page)

    def test_unassigned_and_empty_blocks_and_unproved_order_stay_explicit(self):
        bundle = fixture()
        bundle['process_map']['stages'].append({'id': 'empty', 'name': 'Пустой этап'})
        bundle['process_map']['operations'][0]['stage_id'] = None
        page = build_html(bundle)
        self.assertIn('Этап не указан', page)
        self.assertIn('Операции пока не описаны', page)
        self.assertIn('Порядок этапов не установлен', page)
        self.assertEqual(page.count('class="node operation-node"'), 3)


if __name__ == '__main__':
    unittest.main()
