"""Reader-facing meaning of static lifecycle roles, separate from source support."""
import unittest
import re

from process_observability.report import _Run, _STATES, _hcoverage, _hexact_row


class LoggingMomentLabelsTests(unittest.TestCase):
    def moment(self, role, support="not_assessed"):
        site = {"path": "own.py", "start_line": 1, "end_line": 1,
                "event_key": "own.event", "fields": []}
        return _hexact_row(_Run({}), site, [{"lifecycle_role": role,
            "role_support": support, "occurrence_rule": "Одна запись на вызов"}])[2]

    def test_request_does_not_get_an_unexplained_result_warning(self):
        moment = self.moment("request", "emit_within_operation_sources")
        self.assertIn("запрос", moment)
        self.assertNotIn("не подтверждает новый результат", moment)
        self.assertNotIn("связь с результатом", moment)

    def test_result_with_wrong_operation_source_explains_the_link_gap(self):
        moment = self.moment("result", "emit_outside_operation_sources")
        self.assertIn("результат", moment)
        self.assertIn("связь с результатом выбранного действия не подтверждена", moment)
        self.assertNotIn("не подтверждает новый результат", moment)

    def test_result_without_a_static_emit_states_the_unconfirmed_direct_emission(self):
        self.assertIn("прямая запись в журнал здесь не подтверждена", self.moment("result", "no_static_emit"))

    def test_source_link_gap_is_not_hidden_for_a_request(self):
        self.assertIn("связь записи с выбранным действием не подтверждена",
                      self.moment("request", "emit_outside_operation_sources"))
        self.assertIn("прямая запись в журнал здесь не подтверждена", self.moment("request", "no_static_emit"))

    def test_existing_configured_wrapper_call_is_not_described_as_absent(self):
        site = {"path": "own.py", "start_line": 1, "end_line": 1, "kind": "configured_call",
                "callee": "custom.emit", "event_key": "own.event", "fields": []}
        moment = _hexact_row(_Run({}), site, [{"lifecycle_role": "result", "role_support": "no_static_emit"}])[2]
        self.assertIn("прямая запись в журнал здесь не подтверждена", moment)
        self.assertNotIn("вызов записи не найден", moment)

    def test_supported_result_does_not_claim_runtime_or_delivery(self):
        moment = self.moment("result", "emit_within_operation_sources")
        self.assertNotIn("не подтверждена", moment)
        self.assertNotIn("доставлена", moment)
        self.assertNotIn("операция выполнена", moment)

    def test_atomic_logging_explains_roles_and_counting_rule_in_place(self):
        section = _hcoverage(_Run({"atomic_map": {"event_mappings": []}}), {})
        self.assertIn("Как читать момент записи", section)
        self.assertIn("ответ сервиса", section)
        self.assertIn("повторная попытка", section)
        self.assertIn("без изменения состояния", section)
        self.assertIn("правило группировки", section)
        self.assertIn("не измеренное количество", section)
        self.assertNotIn("не подтверждает новый результат", section)

    def test_legend_headings_match_the_exact_badges_so_retry_is_not_duplicate(self):
        section = _hcoverage(_Run({"atomic_map": {"event_mappings": []}}), {})
        headings = re.findall(r"<dt>(.*?)</dt>", section)
        roles = ("request", "start", "result", "error", "refusal", "retry", "duplicate", "no_op", "ui")
        self.assertEqual(headings, [*(_STATES[role][0].capitalize() for role in roles), "Роль не установлена"])
        self.assertNotIn("Повторно полученная запись", section)
