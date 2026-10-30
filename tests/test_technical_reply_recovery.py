"""Technical reply diagnostics and conservative post-repair recovery (review_execution).

Synthetic source text only; it is snapshotted and never imported or run. Scripted workers read the exact brief
JSON through the real review_repository path, so every count below is a real model-call count. Model text in a
rejected reply carries a CANARY marker: it must never reach a repair request. Passing these tests shows host
wiring, bounds and provenance; it is no semantic verification of any reply.
"""

import copy
import json
import shutil
import unittest
from unittest import mock

from process_observability import review_execution
from process_observability.code_review import FINDING_KINDS, ReviewOptions, canonical_json, sha256_text
from process_observability.llm import LLMError
from process_observability.review_execution import (RECOVERY_POLICY, SUCCESS_KEYS, api_request_sha256,
                                                    review_repository)
from test_review_execution import SYSTEM, ExecutionCase, Scripted, reply_for

PATH = "src/orders.py"
SOURCE = ('raise RuntimeError("synthetic fixture: never execute")\n'   # 1
          "def submit(order):\n"                                       # 2
          "    if order.ready:\n"                                      # 3
          "        persist(order)\n"                                   # 4
          "    return order\n"                                         # 5
          "\n"                                                         # 6
          "\n"                                                         # 7
          "def persist(order):\n"                                      # 8
          "    order.saved = True\n"                                   # 9
          "    return order\n")                                        # 10
RELATIONS = ReviewOptions(relations=True)


def answer(brief, **options):
    """A conforming reply; with the relation contract it also carries an (empty) relations array."""
    reply = reply_for(brief, **options)
    if "relation_kinds" in brief:
        reply["relations"] = []
    return reply


def first_ref(brief):
    item = brief["owned_intervals"][0]
    return {"path": item["path"], "start_line": item["start_line"], "end_line": item["start_line"],
            "symbol": item["symbol"]}


def relation(brief, **changes):
    item = {"id": "r1", "kind": "call", "source": first_ref(brief), "refs": [first_ref(brief)],
            "resolution": "unresolved", "target": {"symbol_id": None, "ref": None, "label": "persist"},
            "condition": None, "data": None}
    item.update(changes)
    return item


def sha(value):
    return sha256_text(canonical_json(value))


def reseal(block):
    block["recovery_sha256"] = sha({key: value for key, value in block.items() if key != "recovery_sha256"})


class Case(ExecutionCase):
    def review(self, respond, options=None, **settings):
        _, snapshot, graph = self.scope({PATH: SOURCE})
        model = Scripted(respond)
        result = review_repository(snapshot, graph, model, system=SYSTEM, options=options or ReviewOptions(),
                                   **settings)
        self.assertEqual(result["ledger"]["summary"]["base_tasks"], 1)  # precondition: one task owns the file
        return result, model

    @staticmethod
    def row(result):
        [row] = result["ledger"]["tasks"]
        return row

    @staticmethod
    def repairs(model):
        return [json.loads(user) for _, user in model.calls if "previous_reply_rejected" in json.loads(user)]


class RepairFeedbackTests(Case):
    """Every repairable rejection names its exact JSON path and host expectation; the repair still works once."""

    def feedback(self, broken, options=None):
        def respond(brief, number):
            return answer(brief) if "previous_reply_rejected" in brief else broken(brief)
        result, model = self.review(respond, options)
        row = self.row(result)
        self.assertEqual((row["state"], row["attempts"], len(model.calls)), ("completed", 2, 2))
        [repair] = self.repairs(model)
        for system, user in model.calls:
            self.assertNotIn("CANARY", user)
            self.assertLessEqual(len(system) + len(user), (options or ReviewOptions()).request_chars)
        self.assertNotIn("recovery", row)  # a successful repair is an ordinary accepted reply
        return repair["previous_reply_rejected"], repair["validation_feedback"], row

    def edited(self, change, **options):
        def broken(brief):
            reply = answer(brief, **options)
            change(reply, brief)
            return reply
        return broken

    def test_every_category_names_its_exact_path_without_model_text(self):
        def drop_requests(reply, brief):
            del reply["requests"]

        def extra_finding_key(reply, brief):
            reply["findings"][0]["CANARY"] = True

        def duplicate_id(reply, brief):
            reply["findings"].append({**reply["findings"][0], "description": "CANARY duplicate"})

        def unknown_kind(reply, brief):
            reply["findings"][0]["kind"] = "dispatch_CANARY"

        def long_description(reply, brief):
            reply["findings"][0]["description"] = "CANARY" + "x" * 1300

        def no_refs(reply, brief):
            reply["findings"][0]["refs"] = []

        def text_line(reply, brief):
            reply["findings"][0]["refs"][0]["start_line"] = "CANARY"

        def outside(reply, brief):
            reply["findings"][0]["refs"][0]["path"] = "elsewhere_CANARY.py"

        def missing_status(reply, brief):
            reply["interval_status"].pop()

        def foreign_status(reply, brief):
            reply["interval_status"].append({"interval_id": "FOREIGN_CANARY", "status": "reviewed", "note": ""})

        def two_requests(reply, brief):
            reply["requests"] = [{"type": "span", "path": PATH, "start_line": 2, "end_line": 3, "reason": "a"},
                                 {"type": "span", "path": PATH, "start_line": 4, "end_line": 5, "reason": "b"}]

        def inapplicable(reply, brief):
            reply["requests"] = [{"type": "span", "path": PATH, "start_line": 1, "end_line": 30,
                                  "symbol_id": "bogus_CANARY", "reason": "need remainder", "finding_ids": ["f0"]}]

        def bad_type(reply, brief):
            reply["requests"] = [{"type": "file_CANARY", "reason": "x"}]

        def unlinked(reply, brief):
            reply["requests"] = [{"type": "span", "path": PATH, "start_line": 2, "end_line": 3, "reason": "x",
                                  "finding_ids": ["nope_CANARY"]}]

        def blank_unknown(reply, brief):
            reply["unknowns"] = ["   "]

        owned = None

        def remember(brief):
            nonlocal owned
            owned = [item["id"] for item in brief["owned_intervals"]]
            return brief
        cases = {
            "shape": (drop_requests, "shape", "$", {"missing_keys": ["requests"]}),
            "finding_fields": (extra_finding_key, "finding_fields", "findings[0]", {"unexpected_key_count": 1}),
            "description": (long_description, "description", "findings[0].description", {"actual_length": 1306}),
            "finding_kind": (unknown_kind, "finding_kind", "findings[0].kind", {"allowed": list(FINDING_KINDS)}),
            "refs": (no_refs, "refs", "findings[0].refs", {"actual_count": 0}),
            "ref_fields": (text_line, "ref_fields", "findings[0].refs[0].start_line", {"invalid_fields": ["start_line"]}),
            "ref_outside_supplied": (outside, "ref_outside_supplied", "findings[0].refs[0]", {"supplied_paths": [PATH]}),
            "requests": (two_requests, "requests", "requests", {"actual_count": 2}),
            "request_fields (type-specific)": (inapplicable, "request_fields", "requests[0].symbol_id",
                                               {"inapplicable_fields": ["symbol_id"],
                                                "required_fields": ["path", "start_line", "end_line"]}),
            "request_fields (type)": (bad_type, "request_fields", "requests[0].type", {"allowed": ["symbol", "span", "literal"]}),
            "request_finding_ids": (unlinked, "request_finding_ids", "requests[0].finding_ids[0]", {}),
            "unknowns": (blank_unknown, "unknowns", "unknowns[0]", {"actual_type": "string"}),
        }
        for label, (change, category, path, facts) in cases.items():
            with self.subTest(case=label):
                options = ReviewOptions(max_requests_per_task=1) if category == "requests" else None
                rejected, feedback, _ = self.feedback(self.edited(change), options)
                self.assertEqual((rejected, feedback["code"], feedback["path"]), (category, category, path))
                self.assertTrue(feedback["expected"])
                for key, value in facts.items():
                    self.assertEqual(feedback[key], value, key)
        # Interval IDs: missing owned IDs by host ID, a foreign item only by its index, never by its text.
        rejected, feedback, row = self.feedback(lambda brief: self.edited(missing_status)(remember(brief)))
        self.assertEqual((rejected, feedback["path"]), ("interval_status", "interval_status"))
        self.assertEqual((feedback["missing_interval_ids"], feedback["owned_interval_ids"]), ([owned[-1]], owned))
        rejected, feedback, _ = self.feedback(self.edited(foreign_status))
        self.assertEqual((feedback["path"], feedback["foreign_item_indexes"]),
                         (f"interval_status[{len(owned)}]", [len(owned)]))
        # A duplicate finding ID names the first occurrence by index.
        rejected, feedback, _ = self.feedback(self.edited(duplicate_id))
        self.assertEqual((rejected, feedback["path"], feedback["duplicate_of_index"]),
                         ("finding_id", f"findings[{len(owned)}].id", 0))

    def test_relation_refs_and_targets_are_named_at_their_own_field(self):
        far = {"path": PATH, "start_line": 500, "end_line": 500, "symbol": "persist"}
        cases = {
            "source": (lambda brief: {"source": {**first_ref(brief), "path": "else_CANARY.py"}},
                       "ref_outside_supplied", "relations[0].source"),
            "target.ref": (lambda brief: {"resolution": "resolved",
                                          "target": {"symbol_id": None, "ref": far, "label": None}},
                           "ref_outside_supplied", "relations[0].target.ref"),
            "refs[1]": (lambda brief: {"refs": [first_ref(brief), far]}, "ref_outside_supplied", "relations[0].refs[1]"),
            "unknown symbol": (lambda brief: {"resolution": "resolved",
                                              "target": {"symbol_id": "sym-CANARY", "ref": None, "label": None}},
                               "relation_target", "relations[0].target.symbol_id"),
            "label-only resolved": (lambda brief: {"resolution": "resolved"}, "relation_target", "relations[0].target"),
            "unresolved with id": (lambda brief: {"target": {"symbol_id": "sym-CANARY", "ref": None, "label": "persist"}},
                                   "relation_target", "relations[0].target"),
            "kind": (lambda brief: {"kind": "goto_CANARY"}, "relation_kind", "relations[0].kind"),
        }
        for label, (changes, category, path) in cases.items():
            with self.subTest(case=label):
                rejected, feedback, _ = self.feedback(self.relation_case(changes), RELATIONS)
                self.assertEqual((rejected, feedback["path"]), (category, path))
        rejected, feedback, _ = self.feedback(self.relation_case(cases["unknown symbol"][0]), RELATIONS)
        self.assertIs(feedback["symbol_id_shown_to_task"], False)

    @staticmethod
    def relation_case(changes):
        def broken(brief):
            reply = answer(brief)
            reply["relations"] = [relation(brief, **changes(brief))]
            return reply
        return broken

    def test_missing_content_and_unparseable_replies_get_the_one_repair_with_a_named_reason(self):
        for label, failure, category, expected in (
                ("missing_content", LLMError("empty", category="missing_content"), "missing_content",
                 "no answer JSON content was returned; return the required object"),
                ("invalid_json", LLMError("bad", category="invalid_json"), "not_json",
                 "one JSON object with exactly the required top-level arrays; no markdown, no prose")):
            with self.subTest(case=label):
                def broken(brief, failure=failure):
                    raise failure
                rejected, feedback, row = self.feedback(broken)
                self.assertEqual((rejected, feedback), (category, {"code": category, "path": "$",
                                                                   "expected": expected}))
                self.assertEqual(row["rejections"], [category])

    def test_missing_content_after_the_repair_fails_honestly_and_other_failures_are_never_repaired(self):
        def always_empty(brief, number):
            raise LLMError("empty", category="missing_content")
        result, model = self.review(always_empty)
        row = self.row(result)
        self.assertEqual((row["state"], row["category"], row["rejections"], len(model.calls)),
                         ("failed", "invalid_reply", ["missing_content", "missing_content"], 2))
        self.assertEqual(result["findings"], [])
        for category in ("invalid_response", "http", "tls", "credentials", "response_too_large", "connection"):
            with self.subTest(category=category):
                def fails(brief, number, category=category):
                    raise LLMError("x", category=category)
                result, model = self.review(fails)
                self.assertEqual((self.row(result)["state"], self.row(result)["category"], len(model.calls)),
                                 ("failed", category, 1))

    def test_an_oversized_detailed_feedback_falls_back_to_a_smaller_located_form_or_fails_honestly(self):
        real = review_execution._repair_user
        events = []

        def padded(user, category, feedback, options):  # only the detailed form is made too large
            text = real(user, category, feedback, options)
            return text + " " * options.request_chars if feedback and set(feedback) - {"code", "path", "expected"} else text

        broken = self.edited(lambda reply, brief: reply["requests"].append(
            {"type": "span", "path": PATH, "start_line": 1, "end_line": 3, "symbol_id": "bogus", "reason": "x"}))
        with mock.patch.object(review_execution, "_repair_user", padded):
            result, model = self.review(lambda brief, number: answer(brief) if "previous_reply_rejected" in brief
                                        else broken(brief), trace=lambda event, data: events.append((event, data)))
        [repair] = self.repairs(model)
        self.assertEqual(set(repair["validation_feedback"]), {"code", "path", "expected"})
        self.assertEqual(repair["validation_feedback"]["path"], "requests[0].symbol_id")
        [request] = [data for event, data in events if event == "model.request" and data["attempt"] == 2]
        self.assertEqual(request["feedback_level"], "minimal")
        self.assertEqual(self.row(result)["state"], "completed")
        # When no located form fits, the task fails as a resource problem: no repair and no recovery.
        with mock.patch.object(review_execution, "_repair_user",
                               lambda user, category, feedback, options: user + " " * options.request_chars):
            result, model = self.review(lambda brief, number: broken(brief))
        row = self.row(result)
        self.assertEqual((row["state"], row["category"], len(model.calls)), ("failed", "repair_exceeds_request_budget", 1))
        self.assertNotIn("recovery", row)


class RecoveryCase(Case):
    """Helpers only (no tests): a run whose first reply and real repair repeat the same defect."""

    def recovered(self, defect, options=None):
        runs = self.runs()
        sent = []

        def respond(brief, number):
            reply = defect(brief)
            sent.append(copy.deepcopy(reply))
            return reply
        result, model = self.review(respond, options, checkpoint_dir=runs / "a")
        self.assertEqual(len(model.calls), 2)  # the first reply and the one repair; never a third call
        row = self.row(result)
        stored = json.loads((runs / "a" / "tasks" / f"{row['id']}.json").read_text(encoding="utf-8"))
        return result, model, sent, row, stored, runs

    def assertDerived(self, result, model, sent, row, stored, category):
        self.assertEqual((row["state"], row["attempts"], row["rejections"]), ("completed", 2, [category, category]))
        self.assertEqual((row["recovery"]["outcome"], row["recovery"]["policy"]), ("accepted", RECOVERY_POLICY))
        self.assertFalse(result["complete"])
        self.assertIn("replies_recovered_after_repair", result["incomplete_reasons"])
        self.assertEqual(result["ledger"]["summary"]["reply_recovery"]["tasks"], 1)
        # The checkpoint keeps the reply exactly as received and the derivation bound to the designated repair.
        self.assertEqual(set(stored), review_execution.RECOVERED_KEYS)
        self.assertEqual((stored["status"], stored["attempts"]), ("recovered", 2))
        self.assertEqual(stored["raw_response"], sent[1])
        block = stored["recovery"]
        self.assertEqual(block["attempts"], [
            {"attempt": 1, "api_request_sha256": api_request_sha256(*model.calls[0]), "response_sha256": sha(sent[0]),
             "rejection": category},
            {"attempt": 2, "api_request_sha256": api_request_sha256(*model.calls[1]), "response_sha256": sha(sent[1]),
             "rejection": category}])
        self.assertEqual(block["raw_response_sha256"], sha(sent[1]))
        self.assertNotEqual(block["derived_response_sha256"], block["raw_response_sha256"])
        [repair] = self.repairs(model)
        self.assertEqual(block["designated_repair"], {"previous_reply_rejected": category,
                                                     "validation_feedback": repair["validation_feedback"],
                                                     "feedback_level": "detailed"})
        self.assertEqual(block["recovery_sha256"],
                         sha({key: value for key, value in block.items() if key != "recovery_sha256"}))
        self.assertEqual(block["actions"], row["recovery"]["actions"])
        return block

    @staticmethod
    def span_with_symbol(brief):
        return answer(brief, requests=[{"type": "span", "path": PATH, "start_line": 1, "end_line": 30,
                                        "symbol_id": "bogus", "reason": "need remainder", "finding_ids": ["f0"]}])

    def resume(self, old, options=None, respond=None):
        _, snapshot, graph = self.scope({PATH: SOURCE})
        model = Scripted(respond or (lambda brief, number: answer(brief)))
        out = self.runs() / "out"
        result = review_repository(snapshot, graph, model, system=SYSTEM, options=options or ReviewOptions(),
                                   checkpoint_dir=out, resume_from=old)
        return result, model, out


class RecoveryTests(RecoveryCase):
    """Repeated auxiliary defects after the one real repair: a derived reply, explicit uncertainty or debt."""

    def test_an_inapplicable_request_field_is_removed_and_valid_findings_survive(self):
        result, model, sent, row, stored, _ = self.recovered(self.span_with_symbol)
        self.assertDerived(result, model, sent, row, stored, "request_fields")
        self.assertEqual(row["recovery"]["actions"], [
            {"action": "removed_inapplicable_request_fields", "path": "requests[0]", "code": "request_fields",
             "request_type": "span", "fields": ["symbol_id"]}])
        self.assertEqual(len(result["findings"]), len(sent[1]["findings"]))
        [request] = result["ledger"]["requests"]
        self.assertEqual((request["type"], request["start_line"], request["end_line"], request["index"]),
                         ("span", 1, 30, 0))
        self.assertNotIn("symbol_id", request)
        # The host still decides the read; nothing beyond the request as given is invented.
        self.assertEqual((request["status"], request["status_reason"]), ("invalid", "line_range_outside_file"))

    def test_an_invalid_request_is_quarantined_as_unread_debt_and_kept_whole(self):
        bad = {"type": "file", "path": PATH, "reason": "read the rest", "finding_ids": ["f0"]}
        unlinked = {"type": "span", "path": PATH, "start_line": 2, "end_line": 3, "reason": "x",
                    "finding_ids": ["no-such-finding"]}
        result, model, sent, row, stored, _ = self.recovered(lambda brief: answer(brief, requests=[bad, unlinked]))
        block = self.assertDerived(result, model, sent, row, stored, "request_fields")
        self.assertEqual([item["raw_request"] for item in block["quarantined_requests"]], [bad, unlinked])
        self.assertEqual([item["path"] for item in row["recovery"]["actions"]],
                         ["requests[0].type", "requests[1].finding_ids[0]"])
        rows = sorted(result["ledger"]["requests"], key=lambda item: item["index"])
        self.assertEqual([(item["index"], item["status"], item["status_reason"], item["type"]) for item in rows],
                         [(0, "invalid", "quarantined_after_repair", None),
                          (1, "invalid", "quarantined_after_repair", "span")])
        self.assertEqual(result["ledger"]["summary"]["request_debt"], 2)
        self.assertEqual(len(result["findings"]), len(sent[1]["findings"]))
        debts = [item for item in result["unknowns"] if item.get("recovery_action") == "quarantined_request"]
        self.assertEqual(len(debts), 2)
        self.assertNotIn("read the rest", json.dumps(result["unknowns"]))

    def test_an_unknown_finding_kind_becomes_other_with_its_original_kind_and_strict_text(self):
        def dispatch(brief):
            reply = answer(brief)
            reply["findings"][0]["kind"] = "dispatch"
            return reply
        result, model, sent, row, stored, _ = self.recovered(dispatch)
        self.assertDerived(result, model, sent, row, stored, "finding_kind")
        original = sent[1]["findings"][0]
        [finding] = [item for item in result["findings"] if item["description"] == original["description"]]
        self.assertEqual((finding["kind"], finding["recovered_kind"]["original_kinds"], finding["refs"]),
                         ("other", ["dispatch"], original["refs"]))
        self.assertIs(finding["recovered_kind"]["merged_with_unrecovered"], False)
        self.assertTrue(any(item.get("recovery_action") == "finding_kind_recorded_as_other"
                            for item in result["unknowns"]))

    def test_a_label_only_resolved_relation_stays_unresolved_and_never_becomes_proof(self):
        result, model, sent, row, stored, _ = self.recovered(
            lambda brief: {**answer(brief), "relations": [relation(brief, resolution="resolved")]}, RELATIONS)
        self.assertDerived(result, model, sent, row, stored, "relation_target")
        [item] = result["relations"]["items"]
        self.assertEqual((item["resolution"], item["target"]),
                         ("unresolved", {"symbol_id": None, "ref": None, "label": "persist"}))
        self.assertEqual((item["recovery"]["original_resolutions"], item["recovery"]["actions"]),
                         (["resolved"], ["resolved_basis"]))
        self.assertFalse([item for item in result["relations"]["items"] if item["resolution"] == "resolved"])
        self.assertTrue(any(item.get("recovery_action") == "relation_target_kept_unresolved"
                            for item in result["unknowns"]))

    def test_an_inapplicable_target_identity_of_an_unresolved_relation_is_cleared_without_upgrade(self):
        result, model, sent, row, stored, _ = self.recovered(lambda brief: {**answer(brief), "relations": [
            relation(brief, target={"symbol_id": "sym-not-applicable", "ref": first_ref(brief), "label": "persist"})]},
            RELATIONS)
        self.assertDerived(result, model, sent, row, stored, "relation_target")
        self.assertEqual(row["recovery"]["actions"], [
            {"action": "inapplicable_target_identity_cleared", "path": "relations[0].target", "code": "relation_target",
             "removed_target_fields": ["symbol_id", "ref"]}])
        [item] = result["relations"]["items"]
        self.assertEqual((item["resolution"], item["target"]),
                         ("unresolved", {"symbol_id": None, "ref": None, "label": "persist"}))

    def test_an_invalid_owned_status_becomes_host_not_understood_never_reviewed(self):
        def invalid(brief):
            reply = answer(brief)
            reply["interval_status"][0].update(status="done", note="")
            return reply
        result, model, sent, row, stored, _ = self.recovered(invalid)
        self.assertDerived(result, model, sent, row, stored, "interval_status")
        [key] = row["host_statuses"]
        self.assertEqual(key, sent[1]["interval_status"][0]["interval_id"])
        [unknown] = [item for item in result["unknowns"] if item["origin"] == "interval_status_missing"]
        self.assertIn("(invalid)", unknown["description"])
        self.assertIn("intervals_without_valid_worker_status", result["incomplete_reasons"])

    def test_recovery_after_a_first_reply_without_content_binds_both_real_attempts(self):
        runs = self.runs()
        sent = []

        def respond(brief, number):
            if "previous_reply_rejected" not in brief:
                raise LLMError("empty", category="missing_content")
            reply = self.span_with_symbol(brief)
            sent.append(copy.deepcopy(reply))
            return reply
        result, model = self.review(respond, checkpoint_dir=runs / "a")
        row = self.row(result)
        self.assertEqual((row["state"], row["rejections"], len(model.calls)),
                         ("completed", ["missing_content", "request_fields"], 2))
        stored = json.loads((runs / "a" / "tasks" / f"{row['id']}.json").read_text(encoding="utf-8"))
        first, second = stored["recovery"]["attempts"]
        self.assertEqual((first["response_sha256"], first["rejection"]), (None, "missing_content"))
        self.assertEqual((second["response_sha256"], second["rejection"]), (sha(sent[0]), "request_fields"))
        self.assertEqual(stored["recovery"]["designated_repair"]["previous_reply_rejected"], "missing_content")
        again, calls, _ = self.resume(runs / "a", respond=lambda brief, number: self.fail("no call expected"))
        self.assertEqual((calls.calls, again["ledger"]["tasks"][0]["recovery"]["source"]), ([], "checkpoint"))

    def test_a_missing_interval_status_is_never_recorded_as_reviewed(self):
        def missing(brief):
            reply = answer(brief)
            reply["interval_status"].pop()
            return reply
        result, model, sent, row, stored, _ = self.recovered(missing)
        self.assertDerived(result, model, sent, row, stored, "interval_status")
        [absent] = row["host_statuses"]
        [interval] = [item for item in result["ledger"]["intervals"] if item["id"] == absent]
        self.assertEqual((interval["state"], interval["model_status"], interval["status_origin"], interval["accepted"]),
                         ("not_understood", None, "host_derived_after_repair", True))
        self.assertNotIn(absent, row["model_statuses"])
        self.assertFalse(result["coverage_complete"])
        self.assertIn("intervals_without_valid_worker_status", result["incomplete_reasons"])
        self.assertTrue(any(item["origin"] == "interval_status_missing" and item["interval_ids"] == [absent]
                            for item in result["unknowns"]))
        self.assertFalse(any(item.get("model_status") == "not_understood" and item["interval_ids"] == [absent]
                             for item in result["unknowns"]))


class StrictTests(Case):
    """Core defects stay terminal after the repair, alone or beside auxiliary defects; no recovery before it."""

    def failed(self, defect, options=None):
        result, model = self.review(lambda brief, number: defect(brief), options)
        row = self.row(result)
        self.assertEqual((row["state"], row["category"]), ("failed", "invalid_reply"))
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result["findings"], [])
        self.assertFalse(result["complete"])
        return result, row

    def test_wrong_refs_and_core_identities_stay_terminal(self):
        def outside(brief):
            reply = answer(brief)
            reply["findings"][0]["refs"][0]["path"] = "elsewhere.py"
            return reply

        def duplicate(brief):
            reply = answer(brief)
            reply["findings"].append(dict(reply["findings"][0], description="Second claim"))
            return reply

        def foreign(brief):
            reply = answer(brief)
            reply["interval_status"][0]["interval_id"] = "iv-of-another-task"
            return reply

        def repeated(brief):
            reply = answer(brief)
            reply["interval_status"].append(dict(reply["interval_status"][0], status="partial"))
            return reply

        def unlabelled(brief):
            reply = answer(brief)
            reply["relations"] = [relation(brief, resolution="resolved",
                                           target={"symbol_id": None, "ref": None, "label": None})]
            return reply

        def bad_resolution(brief):
            reply = answer(brief)
            reply["relations"] = [relation(brief, resolution="probably")]
            return reply

        def over_cap(brief):  # never a model-chosen number of quarantined items, debt rows or unknowns
            return answer(brief, requests=[{"type": "span", "path": PATH, "start_line": 2, "end_line": 3,
                                            "reason": "read"}] * 3)
        for label, defect, options in (("ref outside", outside, None), ("duplicate id", duplicate, None),
                                       ("foreign interval", foreign, None), ("repeated interval", repeated, None),
                                       ("resolved without a label", unlabelled, RELATIONS),
                                       ("unknown resolution", bad_resolution, RELATIONS),
                                       ("more requests than the cap", over_cap, ReviewOptions(max_requests_per_task=2))):
            with self.subTest(case=label):
                _, row = self.failed(defect, options)
                self.assertNotIn("recovery", row)  # nothing auxiliary was handled before the core failure

    def test_an_auxiliary_defect_beside_a_wrong_mandatory_source_still_fails(self):
        def mixed(brief):
            reply = answer(brief)
            reply["findings"][0]["kind"] = "dispatch"
            reply["findings"].append({"id": "bad", "kind": "branch", "description": "Claim about other text",
                                      "refs": [{"path": "elsewhere.py", "start_line": 1, "end_line": 1,
                                                "symbol": "x"}]})
            return reply
        result, row = self.failed(mixed)
        self.assertEqual(row["recovery"]["outcome"], "rejected")
        self.assertEqual([item["action"] for item in row["recovery"]["actions"]], ["finding_kind_recorded_as_other"])
        self.assertEqual(row["recovery"]["final"]["code"], "ref_outside_supplied")
        # A label-only resolved relation with a wrong source is not rescued either.
        result, row = self.failed(lambda brief: {**answer(brief), "relations": [relation(
            brief, resolution="resolved", source={**first_ref(brief), "path": "elsewhere.py"})]}, RELATIONS)
        self.assertNotIn("recovery", row)

    def test_no_recovery_without_a_dispatched_repair_or_after_a_missing_reply(self):
        span = RecoveryCase.span_with_symbol
        result, model = self.review(lambda brief, number: span(brief), ReviewOptions(repair_attempts=0))
        row = self.row(result)
        self.assertEqual((row["state"], row["category"], len(model.calls)), ("failed", "invalid_reply", 1))
        self.assertNotIn("recovery", row)

        def then_empty(brief, number):
            if "previous_reply_rejected" in brief:
                raise LLMError("empty", category="missing_content")
            return span(brief)
        result, model = self.review(then_empty)
        row = self.row(result)
        self.assertEqual((row["state"], row["rejections"], len(model.calls)),
                         ("failed", ["request_fields", "missing_content"], 2))
        self.assertNotIn("recovery", row)

        def then_text(brief, number):
            return "not a JSON reply" if "previous_reply_rejected" in brief else span(brief)
        result, model = self.review(then_text)
        self.assertEqual((self.row(result)["state"], len(model.calls)), ("failed", 2))
        self.assertNotIn("recovery", self.row(result))
        # A defect only in the first reply is repaired by the model; nothing is derived.
        result, model = self.review(lambda brief, number: answer(brief) if "previous_reply_rejected" in brief
                                    else span(brief))
        row = self.row(result)
        self.assertEqual((row["state"], len(model.calls)), ("completed", 2))
        self.assertNotIn("recovery", row)
        self.assertTrue(result["complete"])


class CheckpointTests(RecoveryCase):
    """Reuse replays the derivation from the saved raw reply; tampering or another binding is rejected."""

    def test_a_derived_checkpoint_is_reused_without_a_model_call_and_with_the_same_result(self):
        first, _, sent, row, stored, runs = self.recovered(self.span_with_symbol)

        def refuse(brief, number):
            raise AssertionError("a reused derivation must not call the model")
        again, model, out = self.resume(runs / "a", respond=refuse)
        self.assertEqual(model.calls, [])
        [reused] = again["ledger"]["tasks"]
        self.assertEqual((reused["state"], reused["recovery"]["source"], reused["recovery"]["actions"]),
                         ("completed", "checkpoint", row["recovery"]["actions"]))
        for key in ("findings", "unknowns", "incomplete_reasons"):
            self.assertEqual(again[key], first[key], key)
        self.assertEqual(again["ledger"]["requests"], first["ledger"]["requests"])
        rewritten = json.loads((out / "tasks" / f"{row['id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(rewritten, stored)

    def test_tampered_foreign_or_relabelled_derivations_are_not_reused(self):
        first, _, sent, row, stored, runs = self.recovered(self.span_with_symbol)

        def edit_raw(data):
            data["raw_response"]["findings"][0]["description"] = "Changed after the fact"

        def edit_actions(data):
            data["recovery"]["actions"][0]["fields"] = []
            reseal(data["recovery"])

        def foreign_task(data):
            data["recovery"]["task_id"] = "task-of-another-request"
            reseal(data["recovery"])

        def first_hash(data):
            data["recovery"]["attempts"][0]["api_request_sha256"] = "0" * 64
            reseal(data["recovery"])

        def other_feedback(data):
            data["recovery"]["designated_repair"]["validation_feedback"]["path"] = "requests[1].symbol_id"
            reseal(data["recovery"])

        def old_policy(data):
            data["recovery"]["policy"] = "technical-reply-recovery/0"
            reseal(data["recovery"])

        def edited_basis(data):
            data["recovery"]["basis"] = "edited"
            reseal(data["recovery"])

        def unsealed(data):
            data["recovery"]["derived_response_sha256"] = "1" * 64

        def bool_attempt(data):
            data["recovery"]["attempts"][1]["attempt"] = True
            reseal(data["recovery"])

        def oversized_feedback(data):  # a repair this large could never have been dispatched
            data["recovery"]["designated_repair"]["validation_feedback"]["expected"] = "x" * 40000
            reseal(data["recovery"])

        def wrong_kind(data):
            data["kind"] = "followup"

        def valid_as_received(data):  # a valid raw reply is never relabelled as a derived one
            data["raw_response"]["requests"][0].pop("symbol_id")
            digest = sha(data["raw_response"])
            data["recovery"]["raw_response_sha256"] = data["recovery"]["attempts"][1]["response_sha256"] = digest
            reseal(data["recovery"])
        cases = {"recovery_raw": [edit_raw, valid_as_received], "recovery_derivation": [edit_actions],
                 "binding_mismatch": [foreign_task, wrong_kind],
                 "recovery_binding": [first_hash, other_feedback, oversized_feedback],
                 "recovery_policy": [old_policy, edited_basis], "recovery_integrity": [unsealed],
                 "recovery_schema": [bool_attempt]}
        for category, edits in cases.items():
            for edit in edits:
                with self.subTest(edit=edit.__name__):
                    copy_dir = self.runs() / "copy"
                    shutil.copytree(runs / "a", copy_dir)
                    path = copy_dir / "tasks" / f"{row['id']}.json"
                    data = json.loads(path.read_text(encoding="utf-8"))
                    edit(data)
                    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    again, model, _ = self.resume(copy_dir)
                    self.assertEqual(again["ledger"]["summary"]["checkpoints_not_reused"], {category: 1})
                    self.assertEqual(len(model.calls), 1)  # asked again for real, then accepted as an ordinary reply
                    self.assertNotIn("recovery", again["ledger"]["tasks"][0])

    def test_ordinary_success_checkpoints_keep_their_bytes_and_reuse_without_calls(self):
        runs = self.runs()
        first, _ = self.review(lambda brief, number: answer(brief), checkpoint_dir=runs / "a")
        [row] = first["ledger"]["tasks"]
        stored = json.loads((runs / "a" / "tasks" / f"{row['id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(set(stored), SUCCESS_KEYS)
        self.assertEqual(stored["status"], "succeeded")
        self.assertNotIn("reply_recovery", first["ledger"]["summary"])
        self.assertNotIn("recovery", row)

        def refuse(brief, number):
            raise AssertionError("a valid checkpoint must not call the model")
        again, model, out = self.resume(runs / "a", respond=refuse)
        self.assertEqual(model.calls, [])
        for key in ("findings", "unknowns", "complete", "incomplete_reasons"):
            self.assertEqual(again[key], first[key], key)
        self.assertEqual((out / "tasks" / f"{row['id']}.json").read_bytes(),
                         (runs / "a" / "tasks" / f"{row['id']}.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
