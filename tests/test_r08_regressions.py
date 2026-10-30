"""R08 regressions added with the implementation: negative and compatibility cases beyond the tests-first files.

Synthetic fixture of test_operation_inspector only; no model, network or private data.
"""

import copy
import re
import unittest

from process_observability import workflow
from process_observability.contracts import (ArtifactStatus, OperationDetails, ProcessMap, ProcessOperation, Stage)
from process_observability.onboarding_export import _atomic_accounting
from test_operation_evaluation import oracle
from test_operation_inspector import (REFUND_DETAILS, TASKS, WEBHOOK, InspectorCase, details, number_of, operation_ids,
                                      ref, refund_reply, views)


class DetailsBoundaryTests(InspectorCase):
    def repaired(self, bad):
        bundle = self.inspected(reply=lambda brief, attempt: refund_reply(
            brief, attempt, with_details={"refund": bad if attempt == 1 else REFUND_DETAILS}))
        first, repair = [call for call in self.business.calls if call["task"] == "TASK: map-atomic"]
        [error] = repair["brief"].pop("validation_feedback")["errors"]
        self.assertEqual(repair["brief"], first["brief"])
        return bundle, error

    def test_details_cannot_borrow_another_actions_code_supplied_on_the_same_page(self):
        # TASKS 7-10 is supplied (the release unit is on this page) but it is not the refund action's own code.
        bundle, error = self.repaired(details(input="Транзакции без заказа", refs=[ref(TASKS, 7, 10)]))
        self.assertEqual((error["code"], error["path"]), ("details_ref_outside_operation", "operations[1].details.refs[0]"))
        self.assertEqual(error["reason"], workflow._REASONS["details_ref_outside_operation"])
        [request] = [item for item in bundle["atomic_map"]["requests"] if item["kind"] == "atomic"]
        self.assertEqual(request["outcome"], "accepted_after_repair")

    def test_a_nested_borrowed_citation_is_refused_at_its_own_path(self):
        borrowed = details(external_interactions=[{"system": "Платёжный шлюз", "direction": "request", "endpoint": None,
                                                   "contract": "Отмена транзакции", "refs": [ref(TASKS, 13)]}])
        _, error = self.repaired(borrowed)
        self.assertEqual((error["code"], error["path"]),
                         ("details_ref_outside_operation", "operations[1].details.external_interactions[0].refs[0]"))

    def test_details_null_is_accepted_and_a_wrong_type_gets_one_schema_repair(self):
        bundle, error = self.repaired("нет")
        self.assertEqual((error["code"], error["path"]), ("wrong_type", "operations[1].details"))
        explicit = self.inspected(reply=lambda brief, attempt: refund_reply(brief, attempt, with_details={"refund": None}))
        self.assertComplete(explicit)
        self.assertEqual([op["details"] for op in explicit["process_map"]["operations"]], [None, None, None])


class SymbolTokenTests(unittest.TestCase):
    def test_explanation_symbols_are_whole_tokens_of_the_cited_text_without_claimed_binding(self):
        from process_observability.direct_atomic import _symbol_in
        text = "class RefundService:\n    def handle_refund(self, event):\n        return gateway.send(event)"
        for symbol in ("handle_refund", "handle_refund()", "RefundService.handle_refund", "RefundService::handle_refund",
                       "RefundService#handle_refund", "RefundService"):
            self.assertTrue(_symbol_in(symbol, text), symbol)
        for symbol in ("handle", "refund_service", "<module>", "lambda event", "issue_refund", "", "()"):
            self.assertFalse(_symbol_in(symbol, text), symbol)


class CompatibilityTests(InspectorCase):
    def test_an_earlier_atomic_record_without_details_still_validates_and_renders(self):
        bundle = self.inspected()
        self.assertComplete(bundle)
        earlier = copy.deepcopy(bundle)
        for op in earlier["atomic_map"]["operations"] + earlier["process_map"]["operations"]:
            op.pop("details")
        process = ProcessMap.from_dict(earlier["process_map"])
        self.assertTrue(all(op.details is None for op in process.operations))
        _atomic_accounting(earlier, process, earlier["atomic_map"]["catalog"]["snapshot_id"])
        self.assertTrue(all(view["details"] is None for view in views(earlier).values()))

    def test_request_views_omit_absent_details_but_keep_stated_ones(self):
        stated = OperationDetails(questions=["Подтверждает ли шлюз возврат?"])
        process = ProcessMap(run_id="r", input_fingerprints={}, status=ArtifactStatus.DRAFT, evidence_ids=[],
                             purpose=None, boundaries=None, case_definition=None, stages=[Stage(id="s", name="Этап")],
                             transitions=[], event_mappings=[], unknowns=[],
                             operations=[ProcessOperation(id="a", name="Без деталей", stage_id="s"),
                                         ProcessOperation(id="b", name="С вопросом", stage_id="s", details=stated)])
        for legacy in (True, False):
            first, second = workflow._view(process, legacy=legacy)["operations"]
            self.assertNotIn("details", first)
            self.assertEqual(second["details"]["questions"], ["Подтверждает ли шлюз возврат?"])

    def test_the_logging_tab_gives_each_emit_site_only_its_own_action(self):
        from process_observability.report import _Run, _hcoverage
        bundle = self.inspected()
        self.assertComplete(bundle)
        ids = operation_ids(bundle)
        section = _hcoverage(_Run(bundle), bundle["log_coverage"])
        rows = [row for row in re.findall(r"<tr>(.*?)</tr>", section, re.S) if "<td>" in row and "refund.event" in row]
        self.assertEqual(len(rows), 2)
        accept, refund = (f'href="#op-{number_of(bundle, ids[key])}"' for key in ("accept", "refund"))
        self.assertEqual(sorted((accept in row, refund in row) for row in rows), [(False, True), (True, False)])
        for row in rows:  # neither a duplicate nor a result row borrows the other site's meaning
            self.assertEqual("повтор уже обработанного события" in row, accept in row)
            self.assertEqual('class="b t-info">результат</span>' in row, refund in row)
            self.assertNotIn("не подтверждает новый результат", row)


class AtomicComparisonTests(InspectorCase):
    def test_atomic_maps_are_compared_by_each_actions_own_refs_and_still_only_as_candidates(self):
        from process_observability.map_evaluation import evaluate_map
        bundle = self.inspected()
        self.assertComplete(bundle)
        ids = operation_ids(bundle)
        expected = oracle([("exp-accept", "Принять уведомление", [(WEBHOOK, 9, 9)]),
                           ("exp-refund", "Выполнить возврат", [(WEBHOOK, 15, 15)])],
                          snapshot=bundle["atomic_map"]["catalog"]["snapshot_id"])
        result = evaluate_map(bundle, expected)
        self.assertEqual([(item["expected_id"], item["status"], item["operation_ids"], item["anchor_basis"])
                          for item in result["results"]],
                         [("exp-accept", "candidate_match", [ids["accept"]], ["model_refs"]),
                          ("exp-refund", "candidate_match", [ids["refund"]], ["model_refs"])])
        self.assertEqual(result["unlabelled_operation_ids"], [ids["release"]])
        self.assertIs(result["semantic_verification"], False)
        self.assertEqual(result["oracle_provenance"], "declared_human_not_verified")


class RelatedPathRobustnessTests(unittest.TestCase):
    def test_malformed_transitions_are_ignored_and_the_operation_is_never_its_own_relative(self):
        from process_observability.operation_view import related_operations
        transitions = [{"id": "t1", "from_operation": "a", "to_operation": "b"}, "broken", {"id": "t2"},
                       {"id": "t3", "from_operation": "b", "to_operation": "a"}, {"from_operation": "b", "to_operation": "c"}]
        found = related_operations(transitions, "a")
        self.assertEqual((found["upstream"], found["downstream"], found["transition_ids"]), (["b"], ["b", "c"], ["t1", "t3"]))


if __name__ == "__main__":
    unittest.main()
