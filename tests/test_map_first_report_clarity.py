"""Display cleanup keeps evidence and specific uncertainty in the saved review."""
from copy import deepcopy
import unittest

from process_observability.logging_report import (
    REPORT_WIDE_LIMITATIONS, _logging_panel, _overlay_binding,
)


class ReportClarityTests(unittest.TestCase):
    def fixture(self):
        specific = 'Не найден обработчик <queue>; путь записи не установлен'
        binding = {
            'site_id': 'record-1', 'role': 'request', 'moment': 'before_action',
            'meaning': 'Запрошена выдача книги', 'conditions': 'Перед вызовом сервиса выдачи',
            'correlation': 'loan_id идентифицирует выдачу; номер попытки не записывается',
            'evidence': [],
            'chain': {'limitations': [*REPORT_WIDE_LIMITATIONS, specific]},
            'route': {'limitations': [*REPORT_WIDE_LIMITATIONS, specific]},
        }
        site = {'id': 'record-1', 'event_key': 'Запрос выдачи', 'callee': 'logger.info'}
        data = {'map': {}, 'inventory': {'sites': [site]},
                'operations': [{'operation_id': 'loan', 'state': 'P', 'bindings': [binding]}]}
        return data, binding, site, specific

    def test_generic_notes_removed_from_table_and_overlay_specific_issue_preserved(self):
        data, binding, site, specific = self.fixture()
        before = deepcopy(data)
        output = _logging_panel(data, {'loan': 'Выдать книгу'}, [])
        overlay = _overlay_binding(binding, site)
        for generic in REPORT_WIDE_LIMITATIONS:
            self.assertNotIn(generic, output)
            self.assertNotIn(generic, overlay['chain']['limitations'])
            self.assertNotIn(generic, overlay['route_limitations'])
        self.assertEqual(overlay['chain']['limitations'], [specific])
        self.assertEqual(overlay['route_limitations'], [specific])
        self.assertEqual(output.count('Не найден обработчик &lt;queue&gt;'), 1)
        self.assertNotIn('<queue>', output)
        self.assertEqual(output.count('Назначение и смысл записей оценены моделью по коду;'), 1)
        self.assertEqual(output.count('выполнение, настройки и доставка логов не проверялись.'), 1)
        self.assertEqual(data, before)

    def test_timing_and_join_information_stay_separate_and_are_not_inferred(self):
        data, binding, _, _ = self.fixture()
        output = _logging_panel(data, {'loan': 'Выдать книгу'}, [])
        self.assertIn('<th>Момент записи</th>', output)
        self.assertIn('<th>Идентификаторы и связь записей</th>', output)
        self.assertIn('Перед вызовом сервиса выдачи</p></td><td>loan_id', output)
        binding['correlation'] = ''
        output = _logging_panel(data, {'loan': 'Выдать книгу'}, [])
        self.assertIn('Способ связи не указан в оценке', output)
        self.assertNotIn('loan_id', output)
        # A saved vague judgment is preserved, never silently promoted to an ID.
        binding['correlation'] = 'Перед вызовом сервиса выдачи'
        output = _logging_panel(data, {'loan': 'Выдать книгу'}, [])
        self.assertIn('</td><td>Перед вызовом сервиса выдачи</td>', output)

    def test_generic_prefix_does_not_hide_a_specific_qualification(self):
        data, binding, site, _ = self.fixture()
        qualified = sorted(REPORT_WIDE_LIMITATIONS)[0] + '; потерян параметр loan_id'
        binding['chain']['limitations'] = [qualified]
        self.assertEqual(_overlay_binding(binding, site)['chain']['limitations'], [qualified])
        self.assertIn('потерян параметр loan_id', _logging_panel(data, {'loan': 'Выдать книгу'}, []))


if __name__ == '__main__':
    unittest.main()
