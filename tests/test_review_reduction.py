import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from process_observability import review_reduction as rr
from process_observability.contracts import (
    ArtifactStatus, Assessment, EvidenceRef, FindingState, ProcessMap, ProcessOperation, SourceAnchor,
    SourceKind, Stage,
)
from process_observability.review_reduction import ReductionError, atomic_json, reduce_findings, residual


class Graph:
    """Synthetic graph stand-in: the reducer only reads its fingerprint."""

    def __init__(self, sha="g" * 64):
        self.sha256 = sha

    def to_dict(self):
        return {"nodes": [], "calls": []}


def ref(path, start, end=None, symbol="handle"):
    return {"path": path, "start_line": start, "end_line": end or start, "symbol": symbol}


def finding(identifier, kind, path, line, text=None):
    return {"id": identifier, "kind": kind, "refs": [ref(path, line)],
            "description": text or (f"{kind} at {path}:{line}: when the request amount exceeds the configured "
                                    "approval limit the handler records the decision and returns")}


def review(findings, unknowns=()):
    return {"findings": copy.deepcopy(findings), "unknowns": copy.deepcopy(list(unknowns)), "complete": True}


def sample_findings():
    items = [finding(f"cf-a{n}", "outcome", "pkg/a/orders.py", 10 + 10 * n) for n in range(6)]
    items += [finding(f"cf-b{n}", "identifier", "pkg/b/ids.py", 5 + 10 * n) for n in range(5)]
    items.append(finding("cf-denied", "error", "pkg/a/orders.py", 200,
                         "Rare branch: approval denied when the limit check fails; raises DeniedError and stores no order"))
    return items


def merge_reply(payload, text="Combined technical summary"):
    ids = [item["id"] for item in payload["items"]]
    size = -(-len(ids) // payload["max_summaries"])
    return {"summaries": [{"description": text, "source_ids": ids[start:start + size]}
                          for start in range(0, len(ids), size)]}


class ScriptModel:
    """Offline scripted model: queued replies, exceptions, or callables of the request payload."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(json.loads(user))
        return copy.deepcopy(reply)


class MergeModel:
    """Deterministic offline model that partitions every request into contiguous groups."""

    def __init__(self, text="Combined technical summary"):
        self.text = text
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return merge_reply(json.loads(user), self.text)


class AdapterJsonRepairTests(unittest.TestCase):
    def test_adapter_json_rejection_gets_one_repair_but_transport_gets_none(self):
        from process_observability.llm import LLMError
        rows=sample_findings()
        model=ScriptModel(LLMError('bad answer JSON',category='invalid_json'),merge_reply)
        result=reduce_findings(review(rows),Graph(),model,digest_chars=2400)
        self.assertEqual(len(model.calls),2)
        self.assertEqual(result['accounting']['model_calls'],2)
        failed=ScriptModel(LLMError('connection',category='connection'))
        result=reduce_findings(review(rows),Graph(),failed,digest_chars=2400)
        self.assertEqual(len(failed.calls),1)
        self.assertFalse(result['complete'])


class SummaryBudgetTests(unittest.TestCase):
    def test_description_target_is_not_a_hard_limit_and_text_is_preserved(self):
        for length in (601, 772, 2500):
            text = "С" * length
            reply = {"summaries": [{"description": text, "source_ids": ["a", "b"]}]}
            rr._validate(reply, ["a", "b"], 1, response_chars=4000)
            self.assertEqual(reply["summaries"][0]["description"], text)
        text = "Условие возврата; отдельный результат и повтор. " * 20
        items = [finding(f"f{i}", "branch", "pkg/orders.py", i + 1, "К" * 2200) for i in range(3)]
        model = ScriptModel(lambda p: {"summaries": [{"description": text,
                                                     "source_ids": [r["id"] for r in p["items"]]}]})
        result = reduce_findings(review(items), Graph(), model, digest_chars=2400, request_chars=12000)
        self.assertTrue(result["complete"], result["errors"])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(result["digest"][0]["description"], text)

    def test_whole_response_budget_rejects_without_clipping_or_echoing_text(self):
        reply = {"summaries": [{"description": "PRIVATE-MARKER" * 100, "source_ids": ["a"]},
                                {"description": "Б" * 1400, "source_ids": ["b"]}]}
        before = copy.deepcopy(reply)
        with self.assertRaises(rr._Rejected) as raised:
            rr._validate(reply, ["a", "b"], 2, response_chars=2000)
        feedback = raised.exception.feedback
        self.assertEqual(raised.exception.category, "response_budget")
        self.assertEqual(feedback["path"], "$")
        self.assertEqual(feedback["actual_chars"], len(rr.encoded(reply)))
        self.assertIn("2000", feedback["expected"])
        self.assertNotIn("PRIVATE-MARKER", json.dumps(feedback))
        self.assertEqual(reply, before)

    def test_schema_errors_identify_exact_field_and_expected_contract(self):
        cases = [
            ({"summaries": [{"description": " ", "source_ids": ["a", "b"]}]}, "description", "summaries[0].description"),
            ({"summaries": [{"description": "ok", "source_ids": ["a", "INJECTED-MARKER"]}]}, "unknown_id", "summaries[0].source_ids[1]"),
            ({"summaries": [{"description": "ok", "source_ids": ["a", "a"]}]}, "duplicate_id", "summaries[0].source_ids[1]"),
            ({"summaries": [{"description": "ok", "source_ids": ["a"]}]}, "omitted_id", "summaries"),
            ({"summaries": [{"description": "ok", "source_ids": []}]}, "source_ids", "summaries[0].source_ids"),
            ({"summaries": [{"description": "ok", "source_ids": ["a", "b"], "INJECTED-MARKER": True}]}, "fields", "summaries[0]"),
            ({"summaries": []}, "summary_count", "summaries"),
            ({"INJECTED-MARKER": []}, "shape", "$"),
        ]
        for reply, category, path in cases:
            with self.subTest(category=category), self.assertRaises(rr._Rejected) as raised:
                rr._validate(reply, ["a", "b"], 1, response_chars=4000)
            self.assertEqual(raised.exception.category, category)
            feedback = raised.exception.feedback
            self.assertEqual(feedback["path"], path)
            self.assertTrue(feedback["expected"])
            self.assertNotIn("INJECTED-MARKER", json.dumps(feedback))

    def test_repair_contains_safe_specific_feedback_and_logs_both_attempts(self):
        def bad(payload):
            ids = [item["id"] for item in payload["items"]]
            return {"summaries": [{"description": "INJECTED-MARKER", "source_ids": ids + ids[:1]}]}
        model = ScriptModel(bad, merge_reply)
        events = []
        result = reduce_findings(review(sample_findings()), Graph(), model, digest_chars=2400,
                                 trace=lambda event, payload: events.append((event, payload)))
        self.assertTrue(result["complete"], result["errors"])
        repair = json.loads(model.calls[1][1])
        self.assertEqual(repair["validation_feedback"]["code"], "duplicate_id")
        self.assertEqual(repair["validation_feedback"]["path"], "summaries[0].source_ids[12]")
        self.assertEqual(repair["max_response_chars"], 32000)
        self.assertNotIn("INJECTED-MARKER", model.calls[1][1])
        rejected = [data for event, data in events if event == "review.reduction_rejected"]
        self.assertEqual(rejected[0]["validation_feedback"], repair["validation_feedback"])
        self.assertEqual([d["attempt"] for e, d in events if e == "model.response"], [1, 2])

    def test_response_budget_error_gets_one_repair_then_remains_failed(self):
        def oversized(payload):
            return {"summaries": [{"description": "X" * 4000,
                                    "source_ids": [item["id"] for item in payload["items"]]}]}
        model = ScriptModel(oversized, oversized)
        events = []
        result = reduce_findings(review(sample_findings()[:3]), Graph(), model, digest_chars=1000,
                                 request_chars=4000, trace=lambda e, d: events.append((e, d)))
        self.assertFalse(result["complete"])
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result["errors"][0]["detail"], "response_budget")
        self.assertEqual(result["errors"][0]["validation_feedback"]["path"], "$")
        self.assertEqual([d["attempt"] for e, d in events if e == "review.reduction_rejected"], [1, 2])


class NumberedItemTests(unittest.TestCase):
    def test_model_selects_local_numbers_and_host_restores_source_ids(self):
        items = [finding('cf-' + str(i) * 16, 'branch', 'pkg/orders.py', i + 1,
                         'Distinct condition and outcome ' + str(i) + ' ' + 'data ' * 80) for i in range(3)]
        model = MergeModel()
        events = []
        result = reduce_findings(review(items), Graph(), model, digest_chars=1600,
                                 trace=lambda e,d:events.append((e,d)))
        self.assertTrue(result['complete'], result['errors'])
        payload = json.loads(model.calls[0][1])
        self.assertEqual([r['id'] for r in payload['items']], [1, 2, 3])
        self.assertTrue(all(type(r['id']) is int for r in payload['items']))
        for item in items:
            self.assertNotIn(item['id'], model.calls[0][1])
        restored = [child for node in result['tree'].values() for child in node['child_ids']]
        self.assertCountEqual(restored, [item['id'] for item in items])
        [mapping] = [d for e,d in events if e == 'review.reduction_item_map']
        self.assertEqual(mapping['items'], [{'number':i+1,'id':item['id']} for i,item in enumerate(items)])

    def test_numbers_remain_local_on_every_reduction_level(self):
        items = [finding(f'cf-{i:016x}', 'branch', 'pkg/orders.py', i+1,
                         'Condition ' + str(i) + ' detail ' * 80) for i in range(80)]
        model = MergeModel()
        result = reduce_findings(review(items), Graph(), model, digest_chars=1400)
        self.assertTrue(result['complete'], result['errors'])
        self.assertGreater(result['accounting']['rounds'], 1)
        for system, text in model.calls:
            payload = json.loads(text)
            self.assertEqual([r['id'] for r in payload['items']], list(range(1, len(payload['items'])+1)))
            self.assertNotIn('cf-', text)
            self.assertNotIn('sum-', text)
        covered = [i for row in result['digest'] for i in leaf_ids(result,row)]
        self.assertCountEqual(covered, [item['id'] for item in items])

    def test_numeric_reference_types_and_bounds_are_strict(self):
        for value in (True, 1.0, '1', 'cf-unknown'):
            with self.subTest(value=value), self.assertRaises(rr._Rejected) as raised:
                rr._decode_response({'summaries':[{'description':'Meaning','source_ids':[value,2]}]},
                                    ['host-a','host-b'], 1, response_chars=4000)
            self.assertEqual(raised.exception.category,'source_ids')
        with self.assertRaises(rr._Rejected) as raised:
            rr._decode_response({'summaries':[{'description':'Meaning','source_ids':[1,3]}]},
                                ['host-a','host-b'], 1, response_chars=4000)
        self.assertEqual(raised.exception.category,'unknown_id')
        self.assertIn('1..2',raised.exception.feedback['expected'])
        self.assertEqual(raised.exception.feedback['actual_value'],3)

    def test_unknown_number_gets_one_precise_repair_with_same_mapping(self):
        def bad(p):
            return {'summaries':[{'description':'Wrong reference','source_ids':[r['id'] for r in p['items']]+[99]}]}
        model = ScriptModel(bad,merge_reply)
        result = reduce_findings(review(sample_findings()),Graph(),model,digest_chars=2400)
        self.assertTrue(result['complete'],result['errors'])
        initial,repair = [json.loads(user) for _,user in model.calls]
        self.assertEqual(initial['items'],repair['items'])
        self.assertEqual(repair['validation_feedback']['actual_value'],99)
        self.assertIn('1..12',repair['validation_feedback']['expected'])
        self.assertNotIn('cf-a',model.calls[1][1])

    def test_cache_validation_uses_wire_budget_not_expanded_host_ids(self):
        host_ids = ['cf-' + 'a'*5000, 'cf-' + 'b'*5000]
        wire = {'summaries':[{'description':'Preserve the full condition','source_ids':[1,2]}]}
        canonical = rr._decode_response(wire,host_ids,1,response_chars=1000)
        self.assertGreater(len(rr.encoded(canonical)),1000)
        self.assertEqual(rr._encode_response(canonical,host_ids,1,response_chars=1000),wire)

    def test_identical_numbered_requests_cannot_reuse_a_different_host_mapping(self):
        items = [finding('host-a','branch','pkg/x.py',1,'Identical technical condition ' * 20),
                 finding('host-b','branch','pkg/x.py',2,'Identical technical condition ' * 20)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first = MergeModel()
            a = reduce_findings(review(items),Graph(),first,digest_chars=500,checkpoint_dir=root/'first')
            self.assertTrue(a['complete'],a['errors'])
            same = ScriptModel()
            b = reduce_findings(review(items),Graph(),same,digest_chars=500,checkpoint_dir=root/'same',resume_from=root/'first')
            self.assertTrue(b['complete'],b['errors'])
            self.assertEqual(same.calls,[])
            changed = copy.deepcopy(items)
            changed[0]['id'],changed[1]['id'] = 'other-a','other-b'
            other = MergeModel()
            c = reduce_findings(review(changed),Graph(),other,digest_chars=500,checkpoint_dir=root/'other',resume_from=root/'first')
            self.assertTrue(c['complete'],c['errors'])
            self.assertEqual(len(other.calls),1)
            self.assertEqual(first.calls[0],other.calls[0])  # Same wire text; different host map.
            self.assertEqual(c['accounting']['cache_hits'],0)
            self.assertCountEqual(c['tree'][c['digest'][0]['id']]['child_ids'],['other-a','other-b'])


def items_of(call):
    return [item["id"] for item in json.loads(call[1])["items"]]


def leaf_ids(result, row):
    return result["tree"][row["id"]]["leaf_ids"] if row["level"] == "summary" else [row["id"]]


class TemporaryRoot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Resolved so platform temp symlinks (for example /var on macOS) are not in the path.
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()


class ReductionTest(TemporaryRoot):
    def test_every_finding_and_unknown_becomes_a_leaf_with_original_fields(self):
        items = sample_findings()
        items[0]["parent_finding_ids"] = ["cf-b0"]
        unknowns = ["Retry policy not visible", "Retry policy not visible",
                    {"id": "unk-given", "description": "Queue consumer outside snapshot", "refs": [ref("pkg/c/q.py", 3)]}]
        data = review(items, unknowns)
        original = copy.deepcopy(data)
        model = ScriptModel()
        first = reduce_findings(data, Graph(), model)
        second = reduce_findings(copy.deepcopy(original), Graph(), model)

        self.assertEqual(data, original)
        self.assertEqual(model.calls, [])
        self.assertTrue(first["complete"], first["errors"])
        leaves = {leaf["id"]: leaf for leaf in first["leaves"]}
        self.assertEqual(len(leaves), 15)
        generated = [leaf["id"] for leaf in first["leaves"] if leaf["origin"] == "unknown" and leaf["id"] != "unk-given"]
        self.assertEqual(len(set(generated)), 2)
        self.assertTrue(all(identifier.startswith("unk-") for identifier in generated))
        self.assertEqual([leaf["id"] for leaf in second["leaves"]], [leaf["id"] for leaf in first["leaves"]])
        self.assertEqual(leaves["cf-a0"]["record"]["parent_finding_ids"], ["cf-b0"])
        self.assertEqual(leaves["cf-a0"]["refs"], items[0]["refs"])
        self.assertEqual(leaves["unk-given"]["refs"], [ref("pkg/c/q.py", 3)])
        self.assertEqual([row["id"] for row in first["digest"]],
                         [f"cf-a{n}" for n in range(6)] + ["cf-denied"] + [f"cf-b{n}" for n in range(5)]
                         + ["unk-given"] + generated)
        self.assertEqual(len(first["supplied_refs"]), 13)
        self.assertEqual((first["accounting"]["leaf_count"], first["accounting"]["unknown_count"]), (15, 3))

    def test_reduction_accounts_for_every_leaf_exactly_once_within_the_digest_budget(self):
        model = MergeModel()
        result = reduce_findings(review(sample_findings(), ["Retry policy not visible"]), Graph(), model,
                                 digest_chars=1500)

        self.assertTrue(result["complete"], result["errors"])
        self.assertTrue(model.calls)
        self.assertLessEqual(len(rr.encoded(result["digest"])), 1500)
        covered = [leaf for row in result["digest"] for leaf in leaf_ids(result, row)]
        self.assertEqual(len(covered), len(set(covered)))
        self.assertCountEqual(covered, [leaf["id"] for leaf in result["leaves"]])
        for node in result["tree"].values():
            below = [leaf for child in node["child_ids"]
                     for leaf in (result["tree"][child]["leaf_ids"] if child in result["tree"] else [child])]
            self.assertCountEqual(below, node["leaf_ids"])
        self.assertEqual(result["accounting"]["partitioned_leaf_count"], 13)
        unknown = next(leaf for leaf in result["leaves"] if leaf["origin"] == "unknown")
        self.assertEqual(unknown["description"], "Retry policy not visible")
        self.assertNotIn("Retry policy", rr.encoded(result["digest"]))

    def test_rare_denied_branch_stays_in_catalog_and_citable_and_is_residual_when_unmapped(self):
        model = MergeModel("Orders are approved and identifiers assigned")
        result = reduce_findings(review(sample_findings()), Graph(), model, digest_chars=1500)

        self.assertTrue(result["complete"], result["errors"])
        leaves = {leaf["id"]: leaf for leaf in result["leaves"]}
        self.assertIn("approval denied", leaves["cf-denied"]["description"])
        self.assertNotIn("denied", rr.encoded(result["digest"]))  # compression may lose meaning
        self.assertIn(ref("pkg/a/orders.py", 200), result["supplied_refs"])
        self.assertNotIn("cf-denied", result["accounting"]["salient_leaves_without_digest_ref"])
        process_map = {"stages": [{"id": "approve", "name": "Approve (cf-denied, pkg/a/orders.py:200)",
                                   "refs": [ref("pkg/a/orders.py", 10, 60)]}]}
        self.assertEqual(residual(process_map, result["leaves"]), ["cf-denied"])

    def test_batches_follow_package_and_file_locality_not_input_order(self):
        a = [finding(f"a{n}", "branch", "pkg/a/x.py", 30 - 10 * n) for n in range(2)]
        b = [finding(f"b{n}", "branch", "pkg/b/y.py", 5 + n) for n in range(3)]
        model = MergeModel()
        with mock.patch.object(rr, "BATCH_ITEMS", 3):
            result = reduce_findings(review([b[0], a[0], b[1], a[1], b[2]]), Graph(), model, digest_chars=1000)

        self.assertTrue(result["complete"], result["errors"])
        self.assertEqual(items_of(model.calls[0]), [1, 2])
        self.assertEqual(items_of(model.calls[1]), [1, 2, 3])
        self.assertEqual([r['description'] for r in json.loads(model.calls[0][1])['items']],
                         [a[1]['description'], a[0]['description']])
        self.assertEqual([r['description'] for r in json.loads(model.calls[1][1])['items']],
                         [item['description'] for item in b])

    def test_every_request_including_repair_fits_the_exact_budget(self):
        items = [finding(f"cf-{n:02d}", "branch", f"pkg/m{n % 3}/f.py", n + 1) for n in range(24)]
        bad = {"summaries": [{"description": "x", "source_ids": []}]}
        model = ScriptModel(bad, *[merge_reply] * 100)
        events = []
        result = reduce_findings(review(items), Graph(), model, digest_chars=3000, request_chars=4000,
                                 trace=lambda event, payload: events.append((event, payload)))

        self.assertTrue(result["complete"], result["errors"])
        self.assertGreater(len(model.calls), 3)
        for system, user in model.calls:
            self.assertLessEqual(len(system) + len(user), 4000)
        self.assertEqual(json.loads(model.calls[1][1])["previous_reply_rejected"], "source_ids")
        self.assertEqual(result["accounting"]["repairs"], 1)
        self.assertLessEqual(result["accounting"]["max_request_chars"], 4000)
        self.assertEqual(sum(event == "review.reduction_system" for event, _ in events), 1)
        self.assertTrue(all("system" not in payload for event, payload in events if event == "model.request"))
        requests = [payload for event, payload in events if event == "model.request"]
        responses = [payload for event, payload in events if event == "model.response"]
        expected = [hashlib.sha256((system + "\n" + user).encode("utf-8")).hexdigest()
                    for system, user in model.calls]
        self.assertEqual([row["api_request_sha256"] for row in requests], expected)
        self.assertEqual([row["api_request_sha256"] for row in responses], expected)
        self.assertTrue(all(row["cache_key_sha256"] == row["request_sha256"] for row in requests + responses))
        self.assertEqual(requests[0]["cache_key_sha256"], requests[1]["cache_key_sha256"])
        self.assertNotEqual(expected[0], expected[1])  # repair has a different wire request

    def test_summary_refs_are_host_sampled_from_descendants_salient_first(self):
        items = [finding("err", "error", "pkg/a/x.py", 50), finding("br", "branch", "pkg/a/x.py", 60)]
        items += [finding(f"id{n}", "identifier", "pkg/a/x.py", n + 1) for n in range(8)]

        def two_groups(payload):
            ids = [item["id"] for item in payload["items"]]
            return {"summaries": [{"description": "Identifiers", "source_ids": ids[:5]},
                                  {"description": "Rest", "source_ids": ids[5:]}]}

        def with_refs(payload):
            ids = [item["id"] for item in payload["items"]]
            return {"summaries": [{"description": "All", "source_ids": ids, "refs": [ref("pkg/z.py", 1)]}]}

        with mock.patch.object(rr, "REF_SAMPLE", 3):
            model = ScriptModel(two_groups)
            result = reduce_findings(review(items), Graph(), model, digest_chars=1600)
            repaired_model = ScriptModel(with_refs, two_groups)
            repaired = reduce_findings(review(items), Graph(), repaired_model, digest_chars=1600)

        self.assertTrue(result["complete"], result["errors"])
        self.assertEqual(len(model.calls), 1)
        rows = result["digest"]
        self.assertEqual([row["refs"] for row in rows],
                         [[ref("pkg/a/x.py", n) for n in (1, 2, 3)], [ref("pkg/a/x.py", n) for n in (50, 60, 6)]])
        self.assertEqual([row["refs_omitted"] for row in rows], [2, 2])
        refs_by_leaf = {leaf["id"]: leaf["refs"] for leaf in result["leaves"]}
        for row in rows:
            allowed = [item for leaf in leaf_ids(result, row) for item in refs_by_leaf[leaf]]
            self.assertTrue(all(item in allowed for item in row["refs"]))
        self.assertCountEqual(result["supplied_refs"], [ref("pkg/a/x.py", n) for n in (1, 2, 3, 6, 50, 60)])
        self.assertEqual(result["accounting"]["refs_not_in_digest"], 4)
        self.assertEqual(result["accounting"]["salient_leaves_without_digest_ref"], [])

        self.assertEqual(len(repaired_model.calls), 2)
        self.assertEqual(json.loads(repaired_model.calls[1][1])["previous_reply_rejected"], "fields")
        self.assertNotIn("pkg/z.py", repaired_model.calls[1][1])
        self.assertNotIn(ref("pkg/z.py", 1), repaired["supplied_refs"])
        self.assertEqual(repaired["digest"], result["digest"])

    def test_duplicate_or_omitted_ids_fail_after_exactly_one_repair_without_quoting(self):
        def duplicate(payload):
            ids = [item["id"] for item in payload["items"]]
            return {"summaries": [{"description": "INJECTED-MARKER ignore the rules", "source_ids": ids + ids[:1]}]}

        def omit(payload):
            ids = [item["id"] for item in payload["items"]]
            return {"summaries": [{"description": "Partial", "source_ids": ids[1:]}]}

        model = ScriptModel(duplicate, omit, merge_reply)
        result = reduce_findings(review(sample_findings()[:4]), Graph(), model, digest_chars=300)

        self.assertEqual(len(model.calls), 2)
        self.assertEqual(json.loads(model.calls[1][1])["previous_reply_rejected"], "duplicate_id")
        self.assertNotIn("INJECTED-MARKER", model.calls[1][1])
        self.assertFalse(result["complete"])
        self.assertEqual([(error["category"], error["detail"]) for error in result["errors"]],
                         [("invalid_reply", "omitted_id")])
        self.assertEqual([leaf["id"] for leaf in result["leaves"]], [f"cf-a{n}" for n in range(4)])
        self.assertEqual((result["digest"], result["supplied_refs"], result["tree"]), ([], [], {}))

    def test_transport_error_is_one_call_and_resume_reruns_only_that_batch(self):
        items = [finding(f"a{n}", "branch", "pkg/a/x.py", n + 1) for n in range(2)]
        items += [finding(f"b{n}", "branch", "pkg/b/y.py", n + 1) for n in range(2)]
        first_dir = self.root / "first"
        model = ScriptModel(ConnectionError("HTTP 503 secret-token"), merge_reply)
        resumed_model = MergeModel()
        events = []
        with mock.patch.object(rr, "BATCH_ITEMS", 2):
            failed = reduce_findings(review(items), Graph(), model, digest_chars=600, checkpoint_dir=first_dir)
            resumed = reduce_findings(review(items), Graph(), resumed_model, digest_chars=600,
                                      checkpoint_dir=self.root / "second", resume_from=first_dir,
                                      trace=lambda event, payload: events.append((event, payload)))

        self.assertEqual([items_of(call) for call in model.calls], [[1, 2], [1, 2]])
        self.assertEqual([json.loads(call[1])['items'][0]['where'] for call in model.calls],
                         [['pkg/a/x.py:handle'], ['pkg/b/y.py:handle']])
        self.assertFalse(failed["complete"])
        self.assertEqual([(error["category"], error["detail"]) for error in failed["errors"]],
                         [("transport", "ConnectionError")])
        self.assertNotIn("secret-token", json.dumps(failed))
        self.assertEqual(len(failed["leaves"]), 4)

        self.assertTrue(resumed["complete"], resumed["errors"])
        self.assertEqual(items_of(resumed_model.calls[0]), [1, 2])
        self.assertTrue(all(row['where'] == ['pkg/a/x.py:handle']
                            for row in json.loads(resumed_model.calls[0][1])['items']))
        self.assertNotIn([['pkg/b/y.py:handle'], ['pkg/b/y.py:handle']],
                         [[row['where'] for row in json.loads(call[1])['items']] for call in resumed_model.calls])
        self.assertEqual(resumed["accounting"]["cache_hits"], 1)
        self.assertIn(("review.reduction_checkpoint_invalid", "previous_failure"),
                      [(event, payload.get("category")) for event, payload in events])

    def test_unreachable_digest_budget_stops_explicitly_without_further_calls(self):
        def bloated(payload):
            return {"summaries": [{"description": "x" * 600, "source_ids": [item["id"] for item in payload["items"]]}]}

        model = ScriptModel(bloated, *[merge_reply] * 5)
        result = reduce_findings(review([], ["a", "b"]), Graph(), model, digest_chars=150)
        self.assertEqual(len(model.calls), 1)
        self.assertFalse(result["complete"])
        # One row's host-added overhead alone nearly fills 150 characters: no compaction target is feasible.
        [error] = result["errors"]
        self.assertEqual((error["category"], error["digest_budget"]), ("compaction_infeasible", 150))
        self.assertEqual(sorted(leaf["description"] for leaf in result["leaves"]), ["a", "b"])

        single = ScriptModel()
        result = reduce_findings(review(sample_findings()[:1]), Graph(), single, digest_chars=50)
        self.assertEqual(single.calls, [])
        self.assertEqual([error["category"] for error in result["errors"]], ["compaction_infeasible"])
        self.assertEqual(result["digest"], [])

    def test_item_or_skeleton_over_budget_fails_before_any_call_and_keeps_the_catalog(self):
        items = sample_findings()[:3]
        items[1]["description"] = "y" * 5000
        model = ScriptModel()
        result = reduce_findings(review(items), Graph(), model, digest_chars=500, request_chars=3000)
        self.assertEqual(model.calls, [])
        self.assertFalse(result["complete"])
        self.assertEqual([(error["category"], error.get("item_id")) for error in result["errors"]],
                         [("item_exceeds_request_budget", "cf-a1")])
        self.assertEqual(next(leaf for leaf in result["leaves"] if leaf["id"] == "cf-a1")["description"], "y" * 5000)

        result = reduce_findings(review(sample_findings()[:2]), Graph(), model, system="z" * 4000,
                                 digest_chars=500, request_chars=3000)
        self.assertEqual(model.calls, [])
        self.assertEqual([error["category"] for error in result["errors"]], ["skeleton_exceeds_request_budget"])
        self.assertEqual(len(result["leaves"]), 2)

    def test_invalid_review_input_is_refused_before_any_call(self):
        model = ScriptModel()
        cases = [
            review([finding("same", "branch", "a.py", 1), finding("same", "error", "a.py", 2)]),
            review([dict(finding("x", "branch", "a.py", 1), refs=[{"path": "a.py", "start_line": 3, "end_line": 2}])]),
            review([dict(finding("x", "branch", "a.py", 1), refs=[dict(ref("a.py", 1), verified=True)])]),
            review([], ["valid", {"refs": []}]),
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ReductionError):
                reduce_findings(data, Graph(), model, digest_chars=10)
        self.assertEqual(model.calls, [])


class CheckpointTest(TemporaryRoot):
    def test_valid_checkpoints_are_reused_and_bound_to_model_prompt_and_graph(self):
        data = review(sample_findings())
        run1, run2 = self.root / "run1", self.root / "run2"
        first = MergeModel()
        base = reduce_findings(data, Graph(), first, digest_chars=1500, checkpoint_dir=run1, model_fingerprint="glm-a")
        self.assertTrue(base["complete"], base["errors"])
        files = sorted(run1.glob("*.json"))
        self.assertEqual(len(files), len(first.calls))
        before = {path.name: path.read_bytes() for path in files}

        again, events = MergeModel(), []
        resumed = reduce_findings(data, Graph(), again, digest_chars=1500, checkpoint_dir=run2, resume_from=run1,
                                  model_fingerprint="glm-a", trace=lambda event, payload: events.append(event))
        self.assertEqual(again.calls, [])
        self.assertEqual((resumed["digest"], resumed["tree"]), (base["digest"], base["tree"]))
        self.assertIn("review.reduction_cache_hit", events)
        self.assertEqual(sorted(path.name for path in run2.glob("*.json")), [path.name for path in files])
        self.assertEqual({path.name: path.read_bytes() for path in sorted(run1.glob("*.json"))}, before)

        for name, options in (("model", {"model_fingerprint": "glm-b"}), ("prompt", {"system": "Extra guidance"}),
                              ("graph", {"graph": Graph("h" * 64)})):
            with self.subTest(name):
                model = MergeModel()
                graph = options.pop("graph", Graph())
                changed = reduce_findings(data, graph, model, digest_chars=1500, resume_from=run1,
                                          **{"model_fingerprint": "glm-a", **options})
                self.assertEqual(len(model.calls), len(first.calls))
                self.assertEqual(changed["accounting"]["cache_hits"], 0)
                self.assertTrue(changed["complete"], changed["errors"])

    def test_corrupt_tampered_or_linked_checkpoint_is_rejected_and_reexecuted(self):
        data = review(sample_findings()[:4])
        run1 = self.root / "run1"
        first = MergeModel()
        base = reduce_findings(data, Graph(), first, digest_chars=1000, checkpoint_dir=run1)
        self.assertTrue(base["complete"], base["errors"])
        self.assertEqual(len(first.calls), 1)
        [stored_path] = run1.glob("*.json")
        text = stored_path.read_text(encoding="utf-8")
        valid = self.root / "valid.json"
        valid.write_text(text, encoding="utf-8")

        def tampered():
            stored = json.loads(text)
            rows = stored["response"]["summaries"]
            rows[1]["source_ids"].append(rows[0]["source_ids"][0])
            return json.dumps(stored)

        def rebound():
            stored = json.loads(text)
            stored["binding"]["model_fingerprint"] = "other"
            return json.dumps(stored)

        cases = {"corrupt": lambda path: path.write_text("{not json", encoding="utf-8"),
                 "invalid_reply": lambda path: path.write_text(tampered(), encoding="utf-8"),
                 "binding_mismatch": lambda path: path.write_text(rebound(), encoding="utf-8"),
                 "unsafe_path": lambda path: path.symlink_to(valid)}
        for category, plant in cases.items():
            with self.subTest(category):
                directory = self.root / category
                directory.mkdir()
                plant(directory / stored_path.name)
                model, events = MergeModel(), []
                result = reduce_findings(data, Graph(), model, digest_chars=1000, resume_from=directory,
                                         trace=lambda event, payload: events.append((event, payload)))
                self.assertEqual(len(model.calls), 1)
                self.assertTrue(result["complete"], result["errors"])
                self.assertEqual(result["digest"], base["digest"])
                self.assertIn(("review.reduction_checkpoint_invalid", category),
                              [(event, payload.get("category")) for event, payload in events])
                self.assertEqual(result["accounting"]["checkpoints_rejected"], {category: 1})

    def test_unsafe_or_reused_checkpoint_directories_are_refused_before_calls(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        model = ScriptModel()
        data = review(sample_findings()[:4])
        for options in ({"checkpoint_dir": link}, {"checkpoint_dir": link / "nested"}, {"resume_from": link},
                        {"checkpoint_dir": real, "resume_from": real}):
            with self.subTest(options=options), self.assertRaises(ReductionError):
                reduce_findings(data, Graph(), model, digest_chars=100, **options)
        self.assertEqual(model.calls, [])
        self.assertEqual(list(real.iterdir()), [])

    def test_atomic_json_ignores_a_stale_fixed_temp_file_and_cleans_up_on_error(self):
        target, stale = self.root / "state.json", self.root / "state.json.tmp"
        stale.write_text("stale", encoding="utf-8")
        atomic_json(target, {"b": 1, "a": [1, 2]})
        atomic_json(target, {"b": 2})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"b": 2})
        self.assertEqual(stale.read_text(encoding="utf-8"), "stale")
        expected = ["state.json", "state.json.tmp"]
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), expected)

        with self.assertRaises(ValueError):
            atomic_json(self.root / "nan.json", {"x": float("nan")})
        with mock.patch.object(rr.os, "fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
            atomic_json(self.root / "new.json", {"x": 1})
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), expected)

    def test_atomic_json_refuses_a_symlinked_target_or_ancestor(self):
        real = self.root / "real"
        real.mkdir()
        other = real / "other.json"
        other.write_text("{}", encoding="utf-8")
        (real / "target.json").symlink_to(other)
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        for path in (real / "target.json", link / "new.json", link / ".." / "real" / "new.json"):
            with self.subTest(path=str(path)), self.assertRaises(ReductionError):
                atomic_json(path, {"x": 1})
        self.assertEqual(other.read_text(encoding="utf-8"), "{}")
        self.assertEqual(sorted(path.name for path in real.iterdir()), ["other.json", "target.json"])


def anchored(identifier, path, start, end, snapshot="snap-1"):
    return EvidenceRef(id=identifier, kind=SourceKind.CODE, snapshot_id=snapshot, locator=f"{path}:{start}-{end}",
                       source_anchor=SourceAnchor(snapshot_id=snapshot, path=path, symbol="handle",
                                                  start_line=start, end_line=end))


def normalized_map(stage_evidence, operation_evidence):
    stage = Stage(id="approve", name="Approve order",
                  implemented=Assessment(state=FindingState.PROPOSED, evidence_ids=stage_evidence, reason="Code proposal"))
    return ProcessMap(run_id="synthetic", input_fingerprints={"source": "synthetic"}, status=ArtifactStatus.DRAFT,
                      evidence_ids=[], purpose=None, boundaries=None, case_definition=None, stages=[stage],
                      transitions=[], event_mappings=[], unknowns=["cf-deny at pkg/a/x.py:40-44"],
                      operations=[ProcessOperation(id="op-deny", name="Deny order cf-deny", stage_id="approve",
                                                   evidence_ids=operation_evidence)])


class ResidualTest(unittest.TestCase):
    leaves = [
        {"id": "cf-approve", "kind": "outcome", "refs": [ref("pkg/a/x.py", 10, 12)]},
        {"id": "cf-deny", "kind": "error", "refs": [ref("pkg/a/x.py", 40, 44)]},
        {"id": "cf-id", "kind": "identifier", "refs": [ref("pkg/a/x.py", 90)]},
        {"id": "unk-1", "kind": "unknown", "refs": []},
    ]

    def test_normalized_map_ranges_resolve_through_the_registry_including_operations(self):
        registry = [anchored("src-approve", "pkg/a/x.py", 11, 11), anchored("src-deny", "pkg/a/x.py", 44, 50)]
        process_map = normalized_map(["src-approve"], ["src-deny"])
        self.assertEqual(residual(process_map, self.leaves, registry), ["unk-1"])
        self.assertEqual(residual(process_map.to_dict(), self.leaves, {item.id: item for item in registry}), ["unk-1"])

    def test_text_ids_and_unanchored_evidence_never_match(self):
        telemetry = EvidenceRef(id="tel-1", kind=SourceKind.TELEMETRY, snapshot_id="logs", locator="pkg/a/x.py:10-44")
        process_map = normalized_map(["cf-approve", "sum-0123456789abcdef0123", "tel-1"], ["missing-id"])
        expected = ["cf-approve", "cf-deny", "unk-1"]
        self.assertEqual(residual(process_map, self.leaves, [telemetry]), expected)
        self.assertEqual(residual(process_map, self.leaves), expected)

    def test_unrelated_or_revision_mismatched_ranges_remain_residual(self):
        process_map = {"stages": [{"id": "approve", "refs": [dict(ref("pkg/a/x.py", 10, 12), revision="r2")]}],
                       "event_mappings": [{"event_key": "order.denied", "refs": [ref("pkg/a/other.py", 40, 44)]}],
                       "facts": [{"id": "deny", "ref": ref("pkg/a/x.py", 45, 60)}]}
        leaves = copy.deepcopy(self.leaves)
        leaves[0]["refs"][0]["revision"] = "r1"
        self.assertEqual(residual(process_map, leaves), ["cf-approve", "cf-deny", "unk-1"])
        leaves[0]["refs"][0]["revision"] = "r2"
        self.assertEqual(residual(process_map, leaves), ["cf-deny", "unk-1"])

        registry = [anchored("src-deny", "pkg/a/x.py", 40, 44, snapshot="snap-old")]
        stale = normalized_map([], ["src-deny"])
        self.assertEqual(residual(stale, self.leaves, registry, snapshot_id="snap-new"), ["cf-approve", "cf-deny", "unk-1"])
        self.assertEqual(residual(stale, self.leaves, registry, snapshot_id="snap-old"), ["cf-approve", "unk-1"])


if __name__ == "__main__":
    unittest.main()


class ParallelReductionTests(unittest.TestCase):
    def test_ten_workers_overlap_but_preserve_order_levels_and_resume(self):
        import threading
        import time
        items = [finding(f"cf-{n}", "branch", "pkg/a/x.py", n + 1) for n in range(24)]
        class ConcurrentModel:
            def __init__(self):
                self.lock = threading.Lock()
                self.barrier = threading.Barrier(10)
                self.active = self.peak = self.started = self.first_done = 0
            def complete_json(self, system, user):
                payload = json.loads(user)
                initial = payload['items'][0]['description'].startswith('branch at')
                with self.lock:
                    self.started += 1
                    number = self.started
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    if not initial:
                        assert self.first_done == 12, 'Next level started before its dependencies completed'
                try:
                    if number <= 10:
                        self.barrier.wait(timeout=5)
                    time.sleep((10 - number % 10) / 1000)
                    return merge_reply(payload)
                finally:
                    with self.lock:
                        self.active -= 1
                        if initial:
                            self.first_done += 1
        model = ConcurrentModel()
        events, trace_lock = [], threading.Lock()
        def trace(event, payload):
            assert trace_lock.acquire(blocking=False), 'Concurrent callback on shared trace'
            try:
                time.sleep(.0001)
                events.append((event, payload))
            finally:
                trace_lock.release()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rr, 'BATCH_ITEMS', 2):
            root = Path(tmp).resolve()
            sequential = reduce_findings(review(items), Graph(), MergeModel(), digest_chars=2000,
                                         checkpoint_dir=root/'seq')
            parallel = reduce_findings(review(items), Graph(), model, digest_chars=2000,
                                       workers=10, trace=trace, checkpoint_dir=root/'parallel')
            cached = ScriptModel()
            resumed = reduce_findings(review(items), Graph(), cached, digest_chars=2000,
                                      workers=10, resume_from=root/'seq', checkpoint_dir=root/'resumed')
        self.assertEqual(model.peak, 10)
        self.assertTrue(parallel['complete'], parallel['errors'])
        for key in ('digest', 'tree', 'leaves', 'supplied_refs', 'errors'):
            self.assertEqual(parallel[key], sequential[key], key)
            self.assertEqual(resumed[key], sequential[key], key)
        self.assertEqual(parallel['accounting']['model_calls'], sequential['accounting']['model_calls'])
        self.assertEqual(parallel['accounting']['batches'], sequential['accounting']['batches'])
        self.assertEqual(parallel['accounting']['workers'], 10)
        self.assertEqual(cached.calls, [])
        self.assertEqual(resumed['accounting']['cache_hits'], sequential['accounting']['model_calls'])
        self.assertEqual(sum(event == 'review.reduction_system' for event, _ in events), 1)

    def test_parallel_failure_is_checkpointed_and_only_failed_batch_is_repeated(self):
        items = [finding(f'a{n}', 'branch', 'pkg/a/x.py', n+1) for n in range(2)]
        items += [finding(f'b{n}', 'branch', 'pkg/b/y.py', n+1) for n in range(2)]
        class FailA:
            def complete_json(self, system, user):
                payload=json.loads(user)
                if payload['items'][0]['where'][0].startswith('pkg/a/'):
                    raise ConnectionError('do not expose secret-token')
                return merge_reply(payload)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rr, 'BATCH_ITEMS', 2):
            root=Path(tmp).resolve()
            first=reduce_findings(review(items),Graph(),FailA(),digest_chars=600,workers=10,
                                  checkpoint_dir=root/'first')
            model=MergeModel()
            resumed=reduce_findings(review(items),Graph(),model,digest_chars=600,workers=10,
                                    checkpoint_dir=root/'second',resume_from=root/'first')
        self.assertFalse(first['complete'])
        self.assertEqual(first['accounting']['model_calls'],2)
        self.assertEqual(first['accounting']['repairs'],0)
        self.assertEqual(first['errors'][0]['category'],'transport')
        self.assertNotIn('secret-token',json.dumps(first))
        self.assertTrue(resumed['complete'],resumed['errors'])
        self.assertEqual(resumed['accounting']['cache_hits'],1)
        self.assertTrue(all(row['where'][0].startswith('pkg/a/')
                            for row in json.loads(model.calls[0][1])['items']))

    def test_worker_bound_is_strict_and_rejected_before_calls(self):
        for workers in (0, -1, 11, True, '10', 1.5):
            with self.subTest(workers=workers):
                model=ScriptModel()
                with self.assertRaisesRegex(ReductionError,'workers'):
                    reduce_findings(review(sample_findings()),Graph(),model,workers=workers)
                self.assertEqual(model.calls,[])


    def test_interruption_drains_only_started_work_and_preserves_success_checkpoints(self):
        import threading
        import time
        class Interrupt:
            def __init__(self):
                self.lock=threading.Lock()
                self.barrier=threading.Barrier(10)
                self.calls=0
            def complete_json(self,system,user):
                with self.lock:
                    self.calls+=1
                    number=self.calls
                self.barrier.wait(timeout=5)
                if number==1:
                    raise KeyboardInterrupt()
                time.sleep(.03)
                return merge_reply(json.loads(user))
        model=Interrupt()
        items=[finding(f'cf-{n}','branch','pkg/a/x.py',n+1) for n in range(40)]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(rr,'BATCH_ITEMS',2):
            root=Path(tmp).resolve()
            with self.assertRaises(KeyboardInterrupt):
                reduce_findings(review(items),Graph(),model,digest_chars=2000,workers=10,
                                checkpoint_dir=root/'checkpoints')
            saved=[json.loads(p.read_text()) for p in (root/'checkpoints').glob('*.json')]
        self.assertEqual(model.calls,10)
        self.assertEqual(len(saved),9)
        self.assertTrue(all(row['status']=='succeeded' for row in saved))


class BoundedSplitRecoveryTests(unittest.TestCase):
    def test_numeric_feedback_names_exact_missing_and_duplicate_values(self):
        for ids, code, field, expected in [([1,3,4],'omitted_id','missing_values',[2]),
                                          ([1,2,2,3,4],'duplicate_id','duplicate_values',[2])]:
            with self.subTest(code=code):
                with self.assertRaises(rr._Rejected) as ctx:
                    rr._decode_response({'summaries':[{'description':'summary','source_ids':ids}]},
                                        ['a','b','c','d'],2,response_chars=4000)
                self.assertEqual(ctx.exception.category,code)
                self.assertEqual(ctx.exception.feedback[field],expected)

    def test_split_recovery_preserves_failed_parent_and_real_child_provenance(self):
        class SplitModel:
            def complete_json(self,system,user):
                payload=json.loads(user)
                if len(payload['items'])>=4:
                    ids=[r['id'] for r in payload['items']]
                    return {'summaries':[{'description':'invalid overlap','source_ids':ids+ids[:1]}]}
                return merge_reply(payload)
        items=[finding(f'cf-{n}','branch','pkg/a/x.py',n+1) for n in range(24)]
        with tempfile.TemporaryDirectory() as tmp,mock.patch.object(rr,'BATCH_ITEMS',8):
            root=Path(tmp).resolve()
            sequential=reduce_findings(review(items),Graph(),SplitModel(),digest_chars=2400,
                split_invalid_batches=True,checkpoint_dir=root/'seq')
            parallel=reduce_findings(review(items),Graph(),SplitModel(),digest_chars=2400,
                split_invalid_batches=True,workers=10,checkpoint_dir=root/'parallel')
            saved={p.stem:json.loads(p.read_text()) for p in (root/'parallel').glob('*.json')}
        self.assertTrue(parallel['complete'],parallel['errors'])
        self.assertTrue(parallel['accounting']['recovered_failures'])
        self.assertTrue(any(r['status']=='failed' for r in saved.values()))
        self.assertEqual(parallel['errors'],[])
        for node in parallel['tree'].values():
            self.assertEqual(saved[node['request_sha256']]['status'],'succeeded')
        for key in ('digest','tree','leaves','supplied_refs','errors'):
            self.assertEqual(parallel[key],sequential[key],key)
        self.assertEqual(parallel['accounting']['batches'],sequential['accounting']['batches'])
        self.assertEqual(parallel['accounting']['recovered_failures'],sequential['accounting']['recovered_failures'])

    def test_split_depth_is_bounded_and_transport_is_not_retried_or_split(self):
        class AlwaysInvalid:
            calls=0
            def complete_json(self,system,user):
                self.calls+=1
                return {'summaries':[{'description':'Missing values','source_ids':[1]}]}
        model=AlwaysInvalid();items=sample_findings()[:8]
        failed=reduce_findings(review(items),Graph(),model,digest_chars=500,split_invalid_batches=True)
        self.assertFalse(failed['complete'])
        self.assertLessEqual(model.calls,14)
        self.assertTrue(failed['errors'])
        transport=ScriptModel(ConnectionError('secret-marker'))
        no_retry=reduce_findings(review(items),Graph(),transport,digest_chars=500,split_invalid_batches=True)
        self.assertFalse(no_retry['complete'])
        self.assertEqual(len(transport.calls),1)
        self.assertEqual(no_retry['accounting']['recovered_failures'],[])
        self.assertNotIn('secret-marker',json.dumps(no_retry))


    def test_transport_metadata_is_safe_specific_and_not_retried(self):
        from process_observability.llm import LLMError
        model=ScriptModel(LLMError('not echoed secret-marker',category='http',status=429))
        result=reduce_findings(review(sample_findings()[:4]),Graph(),model,digest_chars=300,
                               split_invalid_batches=True)
        self.assertEqual(len(model.calls),1)
        self.assertEqual(result['errors'][0]['transport'],{'category':'http','http_status':429})
        self.assertNotIn('secret-marker',json.dumps(result))


class RecoveredTreeResumeTests(unittest.TestCase):
    def test_recovered_parent_replays_valid_children_without_calling_model_again(self):
        class SplitModel:
            def complete_json(self,system,user):
                payload=json.loads(user);ids=[r['id'] for r in payload['items']]
                if len(ids)>=4:
                    return {'summaries':[{'description':'overlap','source_ids':ids+ids[:1]}]}
                return merge_reply(payload)
        items=[finding(f'cf-{n}','branch','pkg/a/x.py',n+1) for n in range(24)]
        with tempfile.TemporaryDirectory() as tmp,mock.patch.object(rr,'BATCH_ITEMS',8):
            root=Path(tmp).resolve()
            first=reduce_findings(review(items),Graph(),SplitModel(),digest_chars=2400,
                split_invalid_batches=True,checkpoint_dir=root/'first',workers=10)
            model=ScriptModel()
            resumed=reduce_findings(review(items),Graph(),model,digest_chars=2400,
                split_invalid_batches=True,resume_from=root/'first',checkpoint_dir=root/'second',workers=10)
            failed=[json.loads(p.read_text()) for p in (root/'second').glob('*.json') if json.loads(p.read_text())['status']=='failed']
        self.assertTrue(first['complete'],first['errors'])
        self.assertTrue(resumed['complete'],resumed['errors'])
        self.assertEqual(model.calls,[])
        self.assertEqual(resumed['accounting']['model_calls'],0)
        self.assertEqual(resumed['tree'],first['tree'])
        self.assertEqual(resumed['digest'],first['digest'])
        self.assertTrue(failed)
        self.assertTrue(all(r['category']=='invalid_reply' and r['attempts']==2 for r in failed))


# Compaction: the Saleor run 07 deadlock and its bounded, budget-driven resolution.

ROOT_CHARS = {"saleor/a/": 49058, "saleor/b/": 22025, "saleor/c/": 46888}  # run 07 final root descriptions


def prose(limit, tag="x"):
    """Whole synthetic sentences totalling at most limit characters: model stand-in text, never cut."""
    parts, size = [], 0
    while True:
        sentence = (f"Case {tag}-{len(parts)}: when the checkout state differs the handler records "
                    f"outcome {len(parts)} and returns. ")
        if size + len(sentence) > limit:
            return "".join(parts)
        parts.append(sentence)
        size += len(sentence)


def saleor_shaped_review(packages=tuple(ROOT_CHARS)):
    """82 leaves in three packages; one merge per package yields the three un-packable run 07 roots."""
    counts = {"saleor/a/": 34, "saleor/b/": 16, "saleor/c/": 32}
    return review([finding(f"cf-{prefix[7]}{n:02d}", "error" if n % 5 == 0 else "branch", prefix + "flow.py",
                           10 + 10 * n, prose(1500, f"{prefix[7]}{n}"))
                   for prefix in packages for n in range(counts[prefix])])


def big_leaves(count=12):
    """Leaves of ~5.4k characters: each fits a 12000-character request alone, no two fit together."""
    return review([finding(f"cf-{n:02d}", "branch", "pkg/big/x.py", n + 1, prose(5500, f"big{n}"))
                   for n in range(count)])


class SaleorShapedModel:
    """Offline code model: a merge returns one long root per package; compaction asks a policy."""

    def __init__(self, compact=None):
        self.compact = compact or (lambda payload, where: prose(payload["max_description_chars"], where))
        self.lock, self.calls, self.texts = threading.Lock(), [], []

    def complete_json(self, system, user):
        payload = json.loads(user)
        where = payload["items"][0]["where"][0][:len("saleor/a/")]
        with self.lock:
            self.calls.append((system, user))
        if system.startswith("TASK: code-compact"):
            text = self.compact(payload, where)  # may raise, like a transport
        else:
            text = prose(ROOT_CHARS[where], where)
        with self.lock:
            self.texts.append(text)
        return {"summaries": [{"description": text, "source_ids": [item["id"] for item in payload["items"]]}]}


class CompactingModel:
    """Offline model: merge_reply for merges; whole sentences up to a stated limit for compaction."""

    def __init__(self, limit="max_description_chars", barrier=None):
        self.limit, self.barrier = limit, barrier
        self.lock, self.calls, self.active, self.peak = threading.Lock(), [], 0, 0

    def complete_json(self, system, user):
        payload = json.loads(user)
        with self.lock:
            self.calls.append((system, user))
            number = len(self.calls)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.barrier is not None and number <= self.barrier.parties:
                self.barrier.wait(timeout=5)
            if system.startswith("TASK: code-compact"):
                return {"summaries": [{"description": prose(payload[self.limit], "compact"), "source_ids": [1]}]}
            return merge_reply(payload)
        finally:
            with self.lock:
                self.active -= 1


class CompactionTests(TemporaryRoot):
    def run_reduction(self, model, name=None, resume=None, data=None, **options):
        events = []
        options = {"digest_chars": 24000, "request_chars": 64000, **options}
        result = reduce_findings(data or saleor_shaped_review(), Graph(), model,
                                 checkpoint_dir=None if name is None else self.root / name,
                                 resume_from=None if resume is None else self.root / resume,
                                 trace=lambda event, payload: events.append((event, payload)), **options)
        return result, events

    def saved(self, name):
        return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in (self.root / name).glob("*.json")}

    @staticmethod
    def compactions(model):
        return [json.loads(user) for system, user in model.calls if system.startswith("TASK: code-compact")]

    def assert_exact_coverage(self, result):
        covered = [leaf for row in result["digest"] for leaf in leaf_ids(result, row)]
        self.assertEqual(sorted(covered), sorted(leaf["id"] for leaf in result["leaves"]))

    def test_a_fitting_catalog_passes_unchanged_without_a_model_call(self):
        model = SaleorShapedModel()
        first, _ = self.run_reduction(model, "wide", digest_chars=180000)
        self.assertTrue(first["complete"], first["errors"])
        self.assertEqual((len(model.calls), first["accounting"]["compactions"]), (0, 0))
        self.assertGreater(first["accounting"]["digest_chars"], 120000)
        self.assertEqual([row["description"] for row in first["digest"]],
                         [row["description"] for row in first["leaves"]])
        self.assert_exact_coverage(first)
        no_calls = ScriptModel()
        resumed, _ = self.run_reduction(no_calls, "wide-resume", resume="wide", digest_chars=180000)
        self.assertTrue(resumed["complete"], resumed["errors"])
        self.assertEqual(no_calls.calls, [])
        self.assertEqual(resumed["digest"], first["digest"])

    def test_saleor_shaped_three_root_deadlock_compacts_to_the_digest_budget(self):
        model = SaleorShapedModel()
        result, events = self.run_reduction(model, "run")
        self.assertTrue(result["complete"], result["errors"])
        self.assertLessEqual(len(rr.encoded(result["digest"])), 24000)
        # Real shape: one merge gives three roots that no request can pair; a compaction keeps 3 nodes.
        progress = result["accounting"]["round_progress"]
        self.assertEqual([(row["mode"], row["input_nodes"], row["output_nodes"], row["progress"]) for row in progress],
                         [("merge", 82, 3, "nodes"), ("compact", 3, 3, "chars")])
        self.assertGreater(progress[1]["input_chars"], 115000)  # run 07: 121910 against 24000
        self.assertEqual((result["accounting"]["compactions"], result["accounting"]["compaction_rounds"]), (3, 1))
        requests = self.compactions(model)
        self.assertEqual((len(model.calls), len(requests)), (6, 3))
        self.assertEqual(sorted(request["current_description_chars"] for request in requests),
                         sorted(len(prose(chars, prefix)) for prefix, chars in ROOT_CHARS.items()))
        for request in requests:
            self.assertEqual((request["mode"], len(request["items"]), request["items"][0]["id"]), ("digest_fit", 1, 1))
            self.assertEqual(request["current_description_chars"], len(request["items"][0]["description"]))
            self.assertLessEqual(request["target_description_chars"], request["max_description_chars"])
            self.assertLess(request["max_description_chars"], request["current_description_chars"])
            self.assertEqual(request["budget_basis"]["digest_chars"], 24000)
        for system, user in model.calls:
            self.assertLessEqual(len(system) + len(user), 64000)
        # Every reply used its full limit, yet the digest fits: the host limits are jointly feasible.
        self.assertEqual([row["description"] for row in result["digest"]], model.texts[3:])
        self.assert_exact_coverage(result)
        saved = self.saved("run")
        for row in result["digest"]:
            node = result["tree"][row["id"]]
            [child] = node["child_ids"]
            root = result["tree"][child]
            self.assertEqual((node["leaf_ids"], row["refs"]), (root["leaf_ids"], root["refs"]))
            self.assertEqual((saved[node["request_sha256"]]["status"], saved[root["request_sha256"]]["status"]),
                             ("succeeded", "succeeded"))
            self.assertNotEqual(node["request_sha256"], root["request_sha256"])
        compacted = [payload for event, payload in events if event == "review.reduction_compacted"]
        self.assertEqual(len(compacted), 3)
        self.assertTrue(all(row["output_chars"] <= row["max_description_chars"] < row["input_chars"] for row in compacted))
        [planned] = [payload for event, payload in events if event == "review.reduction_compaction_planned"]
        self.assertEqual((planned["mode"], planned["requests"]), ("digest_fit", 3))

    def test_one_oversized_final_summary_shrinks_with_the_same_node_count(self):
        model = SaleorShapedModel()
        result, events = self.run_reduction(model, data=saleor_shaped_review(("saleor/a/",)))
        self.assertTrue(result["complete"], result["errors"])
        self.assertEqual(len(model.calls), 2)
        merge, compact = result["accounting"]["round_progress"]
        self.assertEqual((merge["input_nodes"], merge["output_nodes"]), (34, 1))
        self.assertEqual((compact["mode"], compact["input_nodes"], compact["output_nodes"], compact["progress"]),
                         ("compact", 1, 1, "chars"))
        self.assertGreater(compact["input_chars"], 24000)
        self.assertLessEqual(compact["output_chars"], 24000)
        self.assertLess(compact["output_text_chars"], compact["input_text_chars"])
        self.assertEqual([payload["shrank"] for event, payload in events if event == "review.reduction_round_finished"],
                         [True, True])

    def test_an_expanding_merge_counts_as_fewer_nodes_and_compaction_finishes_in_budget(self):
        def bloated(payload):
            return {"summaries": [{"description": prose(1200, "bloat"),
                                   "source_ids": [item["id"] for item in payload["items"]]}]}

        def shorter(payload):
            return {"summaries": [{"description": prose(payload["target_description_chars"], "short"),
                                   "source_ids": [1]}]}

        model = ScriptModel(bloated, shorter)
        result = reduce_findings(review([], [prose(400, "u1"), prose(400, "u2")]), Graph(), model, digest_chars=600)
        self.assertTrue(result["complete"], result["errors"])
        self.assertEqual(len(model.calls), 2)
        merge, compact = result["accounting"]["round_progress"]
        self.assertGreater(merge["output_chars"], merge["input_chars"])
        self.assertEqual([(merge["mode"], merge["progress"]), (compact["mode"], compact["progress"])],
                         [("merge", "nodes"), ("compact", "chars")])
        self.assertLessEqual(len(rr.encoded(result["digest"])), 600)

    def test_non_shrinking_or_expanding_compaction_is_refused_after_one_repair(self):
        behaviours = {"echo": lambda payload, where: payload["items"][0]["description"],
                      "expand": lambda payload, where: (payload["items"][0]["description"]
                                                        + " REJECTED-MARKER restated context." * 40)}
        for name, behaviour in behaviours.items():
            with self.subTest(name):
                model = SaleorShapedModel(behaviour)
                result, _ = self.run_reduction(model, name)
                self.assertFalse(result["complete"])
                self.assertEqual((result["digest"], result["supplied_refs"]), ([], []))
                requests = self.compactions(model)
                self.assertEqual((len(model.calls), len(requests)), (9, 6))  # each row: one request, one repair
                self.assertEqual([(error["category"], error["detail"], error["task"]) for error in result["errors"]],
                                 [("invalid_reply", "description_budget", "code-compact")] * 3)
                for first, repair in zip(requests[::2], requests[1::2]):
                    self.assertNotIn("previous_reply_rejected", first)
                    self.assertEqual(repair["previous_reply_rejected"], "description_budget")
                    self.assertEqual(repair["items"], first["items"])
                    feedback = repair["validation_feedback"]
                    self.assertEqual(feedback["max_description_chars"], first["max_description_chars"])
                    self.assertGreaterEqual(feedback["actual_chars"], first["current_description_chars"])
                for _, user in model.calls:
                    self.assertNotIn("REJECTED-MARKER", user)
                self.assertNotIn("REJECTED-MARKER", json.dumps(result["errors"]))
                self.assertEqual((result["accounting"]["rounds"], result["accounting"]["round_progress"][-1]["failed"]),
                                 (2, True))
                self.assertEqual(sorted(stored["status"] for stored in self.saved(name).values()),
                                 ["failed"] * 3 + ["succeeded"] * 3)
                self.assertEqual(len(result["leaves"]), 82)

    def test_single_repair_states_the_exact_escaped_size_rule(self):
        def over_then_fit(payload, where):
            if "previous_reply_rejected" in payload:
                return prose(payload["target_description_chars"], where)
            return prose(payload["max_description_chars"] + 200, "first") + 'REJECTED-MARKER "quoted"\nline'

        model = SaleorShapedModel(over_then_fit)
        result, events = self.run_reduction(model, data=saleor_shaped_review(("saleor/a/",)))
        self.assertTrue(result["complete"], result["errors"])
        first, repair = self.compactions(model)
        rejected = model.texts[1]
        exact = len(json.dumps(rejected, ensure_ascii=False)) - 2
        self.assertEqual(exact, len(rejected) + 3)  # two quotes and a line break are escaped
        feedback = repair["validation_feedback"]
        self.assertEqual((feedback["code"], feedback["path"]), ("description_budget", "summaries[0].description"))
        self.assertEqual((feedback["actual_chars"], feedback["max_description_chars"], feedback["over_by_chars"]),
                         (exact, first["max_description_chars"], exact - first["max_description_chars"]))
        self.assertIn(str(first["max_description_chars"]), feedback["expected"])
        self.assertEqual({key: value for key, value in repair.items()
                          if key not in ("previous_reply_rejected", "validation_feedback")}, first)
        self.assertNotIn("REJECTED-MARKER", model.calls[2][1])
        self.assertEqual(result["accounting"]["repairs"], 1)
        sent = [payload for event, payload in events if event == "model.request" and payload["task"] == "code-compact"]
        self.assertEqual(sent[0]["request_sha256"], sent[1]["request_sha256"])
        self.assertNotEqual(sent[0]["api_request_sha256"], sent[1]["api_request_sha256"])

    def test_description_limit_counts_escapes_and_ordinary_replies_have_no_such_limit(self):
        text = 'say "x"\n' * 10  # 80 characters, 110 inside a JSON string
        reply = {"summaries": [{"description": text, "source_ids": [1]}]}
        with self.assertRaises(rr._Rejected) as raised:
            rr._decode_response(reply, ["host"], 1, response_chars=4000, description_chars=100)
        self.assertEqual(raised.exception.category, "description_budget")
        self.assertEqual((raised.exception.feedback["actual_chars"], raised.exception.feedback["over_by_chars"]), (110, 10))
        self.assertEqual(rr._decode_response(reply, ["host"], 1, response_chars=4000,
                                             description_chars=110)["summaries"][0]["description"], text)
        long = {"summaries": [{"description": "Д" * 5000, "source_ids": [1]}]}
        self.assertEqual(rr._decode_response(long, ["host"], 1, response_chars=6000)["summaries"][0]["source_ids"],
                         ["host"])

    def test_completed_compaction_resumes_with_zero_calls(self):
        first, _ = self.run_reduction(SaleorShapedModel(), "first")
        self.assertTrue(first["complete"], first["errors"])
        before = {path.name: path.read_bytes() for path in (self.root / "first").glob("*.json")}
        model = ScriptModel()
        resumed, _ = self.run_reduction(model, "second", resume="first")
        self.assertEqual(model.calls, [])
        self.assertTrue(resumed["complete"], resumed["errors"])
        for key in ("digest", "tree", "leaves", "supplied_refs"):
            self.assertEqual(resumed[key], first[key], key)
        self.assertEqual((resumed["accounting"]["cache_hits"], resumed["accounting"]["model_calls"]), (6, 0))
        self.assertEqual(before, {path.name: path.read_bytes() for path in (self.root / "first").glob("*.json")})

    def test_failed_compaction_keeps_valid_checkpoints_and_resume_reruns_only_it(self):
        def transport_b(payload, where):
            if where == "saleor/b/":
                raise ConnectionError("HTTP 503 secret-token")
            return prose(payload["max_description_chars"], where)

        broken = SaleorShapedModel(transport_b)
        first, _ = self.run_reduction(broken, "first")
        self.assertFalse(first["complete"])
        self.assertEqual(len(self.compactions(broken)), 3)  # the transport failure is not retried
        self.assertEqual([(error["category"], error["task"]) for error in first["errors"]],
                         [("transport", "code-compact")])
        self.assertNotIn("secret-token", json.dumps(first))
        self.assertEqual(sorted(stored["status"] for stored in self.saved("first").values()),
                         ["failed"] + ["succeeded"] * 5)

        model = SaleorShapedModel()
        resumed, events = self.run_reduction(model, "second", resume="first")
        self.assertTrue(resumed["complete"], resumed["errors"])
        self.assertEqual([json.loads(user)["items"][0]["where"][0][:9] for _, user in model.calls], ["saleor/b/"])
        self.assertEqual(resumed["accounting"]["cache_hits"], 5)
        self.assertIn(("review.reduction_checkpoint_invalid", "previous_failure"),
                      [(event, payload.get("category")) for event, payload in events])
        kept = {key: node for key, node in first["tree"].items() if node.get("task") == "code-compact"}
        self.assertEqual(len(kept), 2)
        self.assertEqual({key: resumed["tree"][key] for key in kept}, kept)
        self.assert_exact_coverage(resumed)

    def test_a_new_target_is_a_new_request_never_a_cache_hit(self):
        wide, _ = self.run_reduction(SaleorShapedModel(), "wide")
        model = SaleorShapedModel()
        narrow, _ = self.run_reduction(model, "narrow", resume="wide", digest_chars=20000)
        self.assertTrue(narrow["complete"], narrow["errors"])
        self.assertLessEqual(len(rr.encoded(narrow["digest"])), 20000)
        self.assertEqual(len(model.calls), 3)
        self.assertTrue(all(system.startswith("TASK: code-compact") for system, _ in model.calls))
        self.assertEqual(narrow["accounting"]["cache_hits"], 3)  # the unchanged merges only

        def batches(result, compact):
            return [row for row in result["accounting"]["batches"] if (row.get("task") == "code-compact") is compact]

        self.assertEqual([row["request_sha256"] for row in batches(wide, False)],
                         [row["request_sha256"] for row in batches(narrow, False)])
        self.assertFalse({row["request_sha256"] for row in batches(wide, True)}
                         & {row["request_sha256"] for row in batches(narrow, True)})
        wide_limits = {row["max_description_chars"] for row in batches(wide, True)}
        narrow_limits = {row["max_description_chars"] for row in batches(narrow, True)}
        self.assertLess(max(narrow_limits), min(wide_limits))
        stored = [item for item in self.saved("narrow").values() if item["binding"].get("task") == "code-compact"]
        self.assertEqual(len(stored), 3)
        self.assertTrue(all(item["binding"]["max_description_chars"] in narrow_limits for item in stored))

    def test_sequential_and_ten_worker_compaction_are_equivalent_and_bounded(self):
        data, sizes = big_leaves(), {"digest_chars": 12000, "request_chars": 12000}
        sequential, _ = self.run_reduction(CompactingModel(), "seq", data=data, **sizes)
        model = CompactingModel(barrier=threading.Barrier(10))
        parallel, events = self.run_reduction(model, "par", data=data, workers=10, **sizes)
        self.assertTrue(sequential["complete"], sequential["errors"])
        self.assertEqual((len(model.calls), model.peak), (12, 10))
        self.assertEqual([row["mode"] for row in parallel["accounting"]["round_progress"]], ["compact"])
        for key in ("digest", "tree", "leaves", "supplied_refs", "errors"):
            self.assertEqual(parallel[key], sequential[key], key)
        for key in ("batches", "model_calls", "round_progress", "compactions"):
            self.assertEqual(parallel["accounting"][key], sequential["accounting"][key], key)
        self.assertEqual(sum(event == "review.reduction_system" for event, _ in events), 1)
        cached = ScriptModel()
        resumed, _ = self.run_reduction(cached, "resumed", resume="seq", data=data, workers=10, **sizes)
        self.assertEqual(cached.calls, [])
        self.assertEqual(resumed["digest"], sequential["digest"])

    def test_rows_too_many_for_the_digest_are_compacted_until_neighbours_merge(self):
        model = CompactingModel(limit="target_description_chars")
        result, _ = self.run_reduction(model, data=big_leaves(), digest_chars=1500, request_chars=12000)
        self.assertTrue(result["complete"], result["errors"])
        self.assertLessEqual(len(rr.encoded(result["digest"])), 1500)
        modes = [row["mode"] for row in result["accounting"]["round_progress"]]
        self.assertEqual(modes[:2], ["compact", "merge"])
        self.assertEqual(result["accounting"]["compaction_rounds"], 1)
        requests = self.compactions(model)
        self.assertEqual(len(requests), 12)
        self.assertTrue(all(request["mode"] == "merge_fit" and len(request["items"]) == 1 for request in requests))
        self.assert_exact_coverage(result)

    def test_compaction_rounds_are_bounded_by_policy(self):
        model = SaleorShapedModel()
        with mock.patch.object(rr, "MAX_COMPACTION_ROUNDS", 0):
            result, _ = self.run_reduction(model)
        self.assertEqual(len(model.calls), 3)  # the merges only
        self.assertEqual([error["category"] for error in result["errors"]], ["compaction_round_limit"])
        self.assertEqual(result["digest"], [])
