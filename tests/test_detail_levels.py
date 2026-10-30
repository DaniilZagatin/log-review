"""R08 detail levels and addressed questions (R5, R6): Process -> Stages -> Operations is a display choice.

Tests-first, on the synthetic fixture of test_operation_inspector. New API (lazy imports):
process_observability.operation_view.detail_view(bundle, level) and question_index(bundle), the per-operation
"questions" of operation_views, the node attribute data-questions and the report switch data-detail-level.
Every level keeps all operations, facts, event mappings, logging changes, metrics, unknowns and open units;
grouping never creates an order between stages, and open work stays addressed instead of a wall of unknowns.
"""

import copy
import dataclasses
import re
import unittest

from process_observability.report import build_html
from process_observability.workflow import ReviewSettings
from test_operation_inspector import (EDGES, FACTS, GROUP, ORDER_GROUP, SITES, WEBHOOK, InspectorCase,
                                      card_of, details, enclosing, fact_id, node_attribute, number_of, operation_ids,
                                      refund_compose, refund_reply, stage_id, text_of, views, with_view_route)

LEVELS = ("process", "stages", "operations")
AMBIGUITY = "Запись об ошибке возможна и после частичного возврата"
ACCEPT_QUESTION = "Сколько раз платёжная система повторяет одно уведомление?"
# A request-record fact of the accept action, for the addressed-question test only.
REQUESTED = "Получено уведомление о возврате"


def detail(bundle, level):
    from process_observability.operation_view import detail_view
    return detail_view(bundle, level)


def questions(bundle):
    from process_observability.operation_view import question_index
    return question_index(bundle)


class DetailLevelTests(InspectorCase):
    def test_three_levels_keep_every_operation_fact_log_change_metric_and_unknown(self):
        bundle = self.inspected()
        self.assertComplete(bundle)
        process = bundle["process_map"]
        expected = {"operation_ids": {op["id"] for op in process["operations"]},
                    "fact_ids": {item["id"] for item in bundle["log_coverage"]["facts"]},
                    "card_ids": {item["id"] for item in bundle["instrumentation_plan"]["cards"]},
                    "metric_ids": {item["id"] for item in bundle["metric_catalog"]["metrics"]}}
        totals = []
        for level in LEVELS:
            with self.subTest(level=level):
                view = detail(bundle, level)
                self.assertEqual(view["level"], level)
                for key, identifiers in expected.items():
                    self.assertEqual({value for item in view["items"] for value in item[key]}, identifiers, key)
                self.assertEqual(sum(item["event_mapping_count"] for item in view["items"]),
                                 len(process["event_mappings"]))
                self.assertEqual(view["unknowns"], process["unknowns"])
                if level != "process":  # every accepted action is shown exactly once
                    listed = [identifier for item in view["items"] for identifier in item["operation_ids"]]
                    self.assertEqual(sorted(listed), sorted(expected["operation_ids"]))
                totals.append(view["totals"])
        self.assertEqual(totals[0], totals[1])
        self.assertEqual(totals[1], totals[2])
        self.assertEqual({key: totals[0][key] for key in ("operations", "event_mappings", "facts", "cards", "metrics")},
                         {"operations": 3, "event_mappings": len(process["event_mappings"]),
                          "facts": len(expected["fact_ids"]), "cards": 4, "metrics": 1})
        [whole] = detail(bundle, "process")["items"]
        self.assertEqual(whole["kind"], "process")
        items = detail(bundle, "stages")["items"]
        stages = {item["id"]: item for item in items if item["kind"] == "stage"}
        self.assertEqual(list(stages), [stage["id"] for stage in process["stages"]])
        self.assertEqual(set(stages[stage_id(bundle)]["operation_ids"]), expected["operation_ids"])
        # The approved obligation's fact has no operation and so no stage: it and its unresolved change stay
        # together, outside every stage, never attached to an operation.
        [rest] = [item for item in items if item["kind"] == "unassigned"]
        self.assertIn("card-order", rest["card_ids"])
        self.assertIn(fact_id(bundle, "order.created"), rest["fact_ids"])
        self.assertEqual(rest["operation_ids"], [])
        ids = operation_ids(bundle)
        actions = {item["id"]: item for item in detail(bundle, "operations")["items"] if item["kind"] == "operation"}
        self.assertEqual(list(actions), [op["id"] for op in process["operations"]])
        refund = actions[ids["refund"]]
        self.assertEqual((refund["operation_ids"], refund["fact_ids"], refund["card_ids"], refund["metric_ids"],
                          refund["event_mapping_count"]),
                         ([ids["refund"]], [fact_id(bundle, "refund.done")], ["card-done-amount"],
                          ["refund-lead-time"], 3))
        with self.assertRaises(ValueError):
            detail(bundle, "units")

    def test_stage_level_never_derives_an_order_between_stages(self):
        groups = {"release": ORDER_GROUP}
        # Two stages without an accepted route between them: grouping alone gives no stage order.
        apart = self.inspected(reply=lambda brief, attempt: refund_reply(brief, attempt, groups=groups))
        self.assertComplete(apart)
        self.assertEqual(len(apart["process_map"]["stages"]), 2)
        self.assertEqual(apart["process_map"]["transitions"], [])
        self.assertEqual(detail(apart, "stages")["transitions"], [])
        # A proposed route between independently invoked commands that crosses the two stages (refund in the webhook,
        # release in a separate task, no call between them): since direct-atomic-validator/10 it stays an inactive,
        # addressed proposal and gives no stage order.
        unsupported = EDGES + (("refund", "release", "Возврат выполнен", "source_condition", [(WEBHOOK, 17, 17)]),)
        apart = self.inspected(reply=lambda brief, attempt: refund_reply(brief, attempt, edges=unsupported,
                                                                         groups=groups))
        ids = operation_ids(apart)
        self.assertEqual(apart["process_map"]["transitions"], [])
        self.assertEqual(apart["atomic_map"]["code_led"]["stages"]["stage_transitions"], [])
        self.assertEqual([(item["from_operation"], item["to_operation"])
                          for item in apart["process_map"]["operation_transitions"]], [(ids["accept"], ids["refund"])])
        [inactive] = [item for item in apart["atomic_map"]["code_led"]["transition_support"]["proposals"]
                      if item["state"] == "inactive"]
        self.assertEqual((inactive["from"], inactive["to"], inactive["reason"]),
                         (ids["refund"], ids["release"], "no_source_chain"))
        self.assertEqual(detail(apart, "stages")["transitions"], [])
        # An accepted, structurally supported action route that crosses the two stages (migrated: accept and refund
        # are one unit and the cited condition lies in it; refund now sits in the other stage): the host derives that
        # one stage order from it (with the transition's ID and evidence); the view only shows the map's own stage
        # transitions, never its own.
        groups = {"refund": ORDER_GROUP}
        bundle = self.inspected(reply=lambda brief, attempt: refund_reply(brief, attempt, edges=EDGES, groups=groups))
        self.assertComplete(bundle)
        process, ids = bundle["process_map"], operation_ids(bundle)
        [crossing] = [item for item in process["operation_transitions"]
                      if item["from_operation"] == ids["accept"] and item["to_operation"] == ids["refund"]]
        self.assertEqual(bundle["atomic_map"]["transitions"][0]["support"]["kind"], "same_unit")
        payment, order = stage_id(bundle, GROUP), stage_id(bundle, ORDER_GROUP)
        self.assertEqual([(row["from_stage"], row["to_stage"], row["transition_ids"])
                          for row in bundle["atomic_map"]["code_led"]["stages"]["stage_transitions"]],
                         [(payment, order, [crossing["id"]])])
        self.assertEqual([(item["from_stage"], item["to_stage"]) for item in process["transitions"]], [(payment, order)])
        self.assertEqual(detail(bundle, "stages")["transitions"], process["transitions"])
        self.assertNotIn((order, payment), {(item["from_stage"], item["to_stage"])
                                            for item in detail(bundle, "stages")["transitions"]})
        self.assertEqual(detail(bundle, "operations")["transitions"], process["operation_transitions"])
        self.assertEqual(detail(bundle, "process")["transitions"], [])

    def test_the_report_switches_levels_without_an_analysis_setting_or_lost_cards(self):
        bundle = self.inspected()
        self.assertComplete(bundle)
        original = copy.deepcopy(bundle)
        page = build_html(bundle)
        for level in LEVELS:
            self.assertIn(f'data-detail-level="{level}"', page)
        for number in range(1, len(bundle["process_map"]["operations"]) + 1):
            self.assertIn(f' id="op-{number}"', page)
        self.assertEqual(page.count('class="card logging-card"'), len(bundle["instrumentation_plan"]["cards"]))
        self.assertEqual(bundle, original)
        # A view choice is not an analysis setting: it cannot silently narrow review or candidate accounting.
        self.assertEqual([item.name for item in dataclasses.fields(ReviewSettings) if "detail" in item.name], [])


class AddressedQuestionTests(InspectorCase):
    def test_questions_are_addressed_to_operation_transition_unit_and_fact_with_back_links(self):
        # The request record of line 7 is bound to both actions, so its site is contradicted; the accept action's
        # request fact observes only that record and stays ambiguous (a code-led fact links records by role).
        sites = SITES + ((WEBHOOK, 7, "refund", "request"),)
        compose = refund_compose({**FACTS, "refund.requested": ("accept", REQUESTED, ["request"], [])})
        # Migrated for direct-atomic-validator/10: the pipeline runs with the grounded route only. The earlier second
        # route accept -> release joined independently invoked commands and is now an inactive, addressed proposal
        # (test_operation_inspector negative control, which also checks that proposal's operation questions), so the
        # question about an unconditioned second route is checked on a pure synthetic view input (with_view_route).
        # Every other question below is the pipeline's own.
        grounded = self.inspected(compose=compose, reply=lambda brief, attempt: refund_reply(
            brief, attempt, edges=EDGES, sites=sites, ambiguity={(WEBHOOK, 15): AMBIGUITY},
            with_details={"accept": details(questions=[ACCEPT_QUESTION])}))
        self.assertComplete(grounded)
        bundle = with_view_route(grounded, "accept", "release")
        process, ids = bundle["process_map"], operation_ids(bundle)
        [requested] = [item for item in bundle["log_coverage"]["facts"] if item["meaning"] == REQUESTED]
        self.assertEqual(requested["lifecycle_check"], "ambiguous")  # precondition: a contradicted record
        index = questions(bundle)

        def about(kind, subject):
            return [item for item in index if item["subject_kind"] == kind and item["subject_id"] == subject]
        self.assertTrue(any(AMBIGUITY in item["text"] for item in about("operation", ids["refund"])))
        self.assertTrue(any(ACCEPT_QUESTION in item["text"] for item in about("operation", ids["accept"])))
        [open_edge] = [item["id"] for item in process["operation_transitions"]
                       if item["from_operation"] == ids["accept"] and item["condition"] is None]
        self.assertTrue(about("transition", open_edge))
        [audit] = [key for key, state in bundle["atomic_map"]["unit_states"].items() if state["symbol"] == "record_audit"]
        self.assertTrue(any("отметка аудита" in item["text"] for item in about("unit", audit)))
        self.assertTrue(about("fact", requested["id"]))
        texts = [item["text"] for item in index]
        for unknown in process["unknowns"]:  # the addressed index is lossless, not a replacement of unknowns
            self.assertTrue(any(unknown in text for text in texts), unknown)
        seen = views(bundle)
        accept = seen[ids["accept"]]["questions"]
        self.assertTrue(any(ACCEPT_QUESTION in item["text"] for item in accept))
        self.assertTrue(any(item["subject_kind"] == "transition" for item in accept))  # on its source operation
        own = seen[ids["refund"]]["questions"]
        self.assertTrue(own)
        page = build_html(bundle)
        self.assertEqual(node_attribute(page, ids["refund"], "data-questions"), str(len(own)))
        link = f'href="#op-{number_of(bundle, ids["refund"])}"'
        self.assertTrue(any(link in item for item in enclosing(page, AMBIGUITY, "li")))
        for identifier in ids.values():  # specific questions, not mass "unknown applicability/quality" columns
            self.assertNotRegex(text_of(card_of(page, number_of(bundle, identifier))), re.compile(r"применимост|качеств", re.I))

    def test_deferred_and_failed_units_stay_visible_at_every_level(self):
        runs = {"deferred": {"atomic_page_units": 1, "refine_max_requests": 2, "triage": "off"},
                "failed": {"atomic_page_units": 1, "triage": "off",
                           "reply": lambda brief, attempt: refund_reply(brief, attempt, broken=("release_funds_task",))}}
        for label, options in runs.items():
            with self.subTest(run=label):
                bundle = self.inspected(**options)
                record = bundle["atomic_map"]
                open_units = {key for key, state in record["unit_states"].items()
                              if state["state"] in ("deferred", "request_failed")}
                self.assertTrue(open_units)
                self.assertEqual(record["status"], "partial")
                index = questions(bundle)
                for key in open_units:
                    self.assertTrue(any(item["subject_kind"] == "unit" and item["subject_id"] == key for item in index),
                                    key)
                for level in LEVELS:
                    view = detail(bundle, level)
                    self.assertEqual(set(view["open_units"]), open_units, level)
                    self.assertEqual(view["totals"]["open_units"], len(open_units))
                    self.assertEqual(view["totals"]["operations"], len(record["operations"]))


if __name__ == "__main__":
    unittest.main()
