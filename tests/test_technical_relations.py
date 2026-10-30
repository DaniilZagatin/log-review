"""Technical relations: an opt-in, ref-checked worker contract kept apart from host static navigation.

Synthetic fixture text only; it starts with a raise so that importing or running it would fail. Scripted workers read
the exact brief JSON; nothing touches a network or runs target code. Off, the original technical request bytes and
fingerprints stay exactly as before and relations are reported as not collected; on is a new technical identity
whose relations survive checkpoints and are bound into a registry.
"""

import copy
import json
import unittest

from process_observability.code_review import (INSTRUCTIONS, RELATION_KINDS, REPLY_SCHEMA, ReviewOptions,
                                               plan_review)
from process_observability.review_execution import (ReviewResumeError, _Rejected, _Task, _range, _validate,
                                                    review_repository)
from process_observability.technical_relations import RelationError, binding, build_registry, check_registry
from test_action_refinement import Code
from test_review_execution import SYSTEM, ExecutionCase, Scripted, reply_for

PATH = "app/parcels.py"
SOURCE = (
    'raise RuntimeError("synthetic fixture: never execute")\n'   # 1
    "HANDLERS = {}\n"                                              # 2
    "\n"                                                           # 3
    "\n"                                                           # 4
    "def register(name):\n"                                        # 5
    "    def wrap(func):\n"                                        # 6
    "        HANDLERS[name] = func\n"                              # 7
    "        return func\n"                                        # 8
    "    return wrap\n"                                            # 9
    "\n"                                                           # 10
    "\n"                                                           # 11
    '@register("parcel.ready")\n'                                  # 12
    "def on_ready(parcel):\n"                                      # 13
    "    if parcel.weight > 30:\n"                                 # 14
    '        return "heavy"\n'                                     # 15
    "    return dispatch(parcel)\n"                                # 16
    "\n"                                                           # 17
    "\n"                                                           # 18
    "def dispatch(parcel):\n"                                      # 19
    "    parcel.sent = True\n"                                     # 20
    "    notify_partner(parcel, callback=on_ready)\n"              # 21
    '    return "sent"\n')                                         # 22
ON = ReviewOptions(relations=True)


def ref(first, last, symbol):
    return {"path": PATH, "start_line": first, "end_line": last, "symbol": symbol}


def owned_lines(brief):
    return {(item["path"], line) for item in brief["owned_intervals"]
            for line in range(item["start_line"], item["end_line"] + 1)}


def fixture_relations(brief):
    """Relations of the fixture that the supplied text shows: call, branch, return, callback, dispatch, external."""
    lines = owned_lines(brief)
    symbols = {row["qualname"]: row["id"] for row in brief["navigation"]["rows"] if row["t"] == "symbol"}
    rows = []

    def add(identifier, kind, source, refs, target, resolution, condition=None, data=None):
        if all((PATH, line) in lines for item in [source, *refs] for line in range(item["start_line"],
                                                                                    item["end_line"] + 1)):
            rows.append({"id": identifier, "kind": kind, "source": source, "refs": refs, "target": target,
                         "resolution": resolution, "condition": condition, "data": data})
    dispatch_id = symbols.get("dispatch")
    add("r-call", "call", ref(16, 16, "on_ready"), [ref(16, 16, "on_ready")],
        {"symbol_id": dispatch_id, "ref": None, "label": None} if dispatch_id
        else {"symbol_id": None, "ref": None, "label": "dispatch"},
        "resolved" if dispatch_id else "unresolved", data="parcel")
    add("r-branch", "branch", ref(14, 15, "on_ready"), [ref(14, 15, "on_ready")],
        {"symbol_id": None, "ref": None, "label": 'early return "heavy"'}, "unresolved", condition="parcel.weight > 30")
    add("r-return", "return", ref(22, 22, "dispatch"), [ref(22, 22, "dispatch")],
        {"symbol_id": None, "ref": None, "label": "the caller of dispatch"}, "unresolved", data='"sent"')
    add("r-callback", "callback", ref(21, 21, "dispatch"), [ref(21, 21, "dispatch")],
        {"symbol_id": None, "ref": ref(13, 16, "on_ready"), "label": "on_ready"}, "resolved")
    add("r-dispatch", "dispatch", ref(7, 7, "register.wrap"), [ref(7, 7, "register.wrap"), ref(12, 12, "on_ready")],
        {"symbol_id": None, "ref": None, "label": "HANDLERS[name]"}, "unresolved", data="func")
    add("r-external", "control_handoff", ref(21, 21, "dispatch"), [ref(21, 21, "dispatch")],
        {"symbol_id": None, "ref": None, "label": "notify_partner"}, "external", data="parcel")
    return rows


def with_relations(brief, number):
    return {**reply_for(brief), "relations": fixture_relations(brief)}


def generic_relations(brief):
    """One call relation per static call row of the brief: resolved only where the brief named the target."""
    rows, lines = [], owned_lines(brief)
    symbols = {row["id"]: row["qualname"] for row in brief["navigation"]["rows"] if row["t"] == "symbol"}
    for index, row in enumerate(item for item in brief["navigation"]["rows"] if item["t"] == "call"):
        if (row["path"], row["line"]) not in lines:
            continue
        source = {"path": row["path"], "start_line": row["line"], "end_line": row["line"],
                  "symbol": symbols.get(row.get("owner_id")) or "<module>"}
        resolved = row.get("target_id")
        rows.append({"id": f"c{index}", "kind": "call", "source": source, "refs": [dict(source)],
                     "target": {"symbol_id": resolved, "ref": None, "label": None if resolved else row.get("callee")},
                     "resolution": "resolved" if resolved else "unresolved", "condition": None, "data": None})
    return rows


class RelationCode(Code):
    """The scripted code model of the workflow tests, also proposing call relations under the relation contract."""

    def complete_json(self, system, user):
        reply = super().complete_json(system, user)
        brief = json.loads(user)
        if "relation_kinds" in brief:
            reply["relations"] = generic_relations(brief)
        return reply


class ContractIdentityTests(ExecutionCase):
    def test_the_default_contract_is_byte_identical_and_relations_are_a_new_identity(self):
        _, snapshot, graph = self.scope({PATH: SOURCE})
        self.assertNotIn("relations", ReviewOptions().identity())
        self.assertNotIn("relations", ReviewOptions().to_dict())
        self.assertIs(ON.identity()["relations"], True)
        off, on = plan_review(snapshot, graph, SYSTEM, ReviewOptions()), plan_review(snapshot, graph, SYSTEM, ON)
        brief = json.loads(off.tasks[0].user)
        self.assertEqual(brief["instructions"], list(INSTRUCTIONS))
        self.assertEqual(set(brief["reply_schema"]), set(REPLY_SCHEMA))
        self.assertFalse({"relation_kinds", "relation_resolutions"} & set(brief))
        self.assertNotIn("max_relations", brief["limits"])
        brief = json.loads(on.tasks[0].user)
        self.assertEqual(brief["relation_kinds"], list(RELATION_KINDS))
        self.assertIn("relations", brief["reply_schema"])
        self.assertNotEqual(off.run_fingerprint, on.run_fingerprint)
        self.assertNotEqual(off.tasks[0].request_sha256, on.tasks[0].request_sha256)

    def test_the_worker_system_names_five_arrays_only_when_relations_are_collected(self):
        from process_observability.guidance import load_guidance
        from process_observability.review_execution import CODE_REVIEW_RELATIONS_SYSTEM, CODE_REVIEW_SYSTEM
        from process_observability.workflow import review_system
        guidance = load_guidance()
        off, _ = review_system(guidance)
        on, _ = review_system(guidance, relations=True)
        self.assertTrue(off.startswith(CODE_REVIEW_SYSTEM))  # the original worker text, byte for byte
        self.assertTrue(on.startswith(CODE_REVIEW_RELATIONS_SYSTEM))
        self.assertEqual(off[len(CODE_REVIEW_SYSTEM):], on[len(CODE_REVIEW_RELATIONS_SYSTEM):])  # same guidance
        self.assertIn("all four top-level arrays: findings, interval_status, requests, unknowns.", off)
        self.assertNotIn("relations", CODE_REVIEW_SYSTEM)
        self.assertIn("all five top-level arrays: findings, interval_status, requests, unknowns, relations.", on)
        self.assertNotIn("four", CODE_REVIEW_RELATIONS_SYSTEM)  # one unambiguous count, never four plus a fifth


class RelationValidationTests(ExecutionCase):
    def setUp(self):
        _, self.snapshot, graph = self.scope({PATH: SOURCE})
        self.plan = plan_review(self.snapshot, graph, SYSTEM, ON)
        [item] = self.plan.tasks
        intervals = self.plan.interval_map()
        owned = {key: _range(intervals[key]) for key in item.interval_ids}
        self.task = _Task(id=item.id, kind="base", system=item.system, user=item.user,
                          request_sha256=item.request_sha256, owned=owned,
                          supplied=tuple(dict(owned[row["interval_id"]], id=row["interval_id"], role="owned")
                                         for row in item.supplied_ranges),
                          exposed_symbol_ids=frozenset(item.exposed_symbol_ids))
        self.brief = json.loads(item.user)
        self.good = with_relations(self.brief, 1)

    def test_accepted_relations_keep_typed_ends_refs_and_origin(self):
        _, accepted = _validate(self.good, self.task, self.snapshot, ON)
        kinds = {item["kind"] for item in accepted["relations"]}
        self.assertEqual(kinds, {"call", "branch", "return", "callback", "dispatch", "control_handoff"})
        by_kind = {item["kind"]: item for item in accepted["relations"]}
        self.assertEqual(by_kind["branch"]["condition"], "parcel.weight > 30")
        self.assertEqual((by_kind["control_handoff"]["resolution"], by_kind["control_handoff"]["target"]["label"]),
                         ("external", "notify_partner"))
        self.assertEqual(by_kind["callback"]["target"]["ref"]["start_line"], 13)
        for item in accepted["relations"]:
            self.assertEqual((item["origin"], item["basis"]), ("worker_proposal", "model_proposal"))
            self.assertTrue(item["evidence_ids"] and item["refs"])
            self.assertTrue(item["id"].startswith("cr-"))

    def test_wrong_ranges_unknown_ids_and_baseless_resolution_reject_the_whole_reply(self):
        def edit(change):
            reply = copy.deepcopy(self.good)
            change(reply["relations"])
            return reply

        def first(kind):
            return lambda rows: next(row for row in rows if row["kind"] == kind)
        cases = {
            "range outside supplied text": (lambda rows: first("branch")(rows)["source"].update(path="app/other.py"),
                                            "ref_outside_supplied"),
            "unknown local symbol id": (lambda rows: first("call")(rows).update(
                target={"symbol_id": "sym-unknown", "ref": None, "label": None}, resolution="resolved"),
                "relation_target"),
            "resolved without basis": (lambda rows: first("return")(rows).update(resolution="resolved"),
                                       "relation_target"),
            "unresolved with an id": (lambda rows: first("branch")(rows).update(
                target={"symbol_id": "sym-unknown", "ref": None, "label": "x"}), "relation_target"),
            "unknown kind": (lambda rows: first("branch")(rows).update(kind="goto"), "relation_kind"),
            "missing refs": (lambda rows: first("branch")(rows).update(refs=[]), "refs"),
            "unknown key": (lambda rows: first("branch")(rows).update(confidence="high"), "relation_fields"),
        }
        for label, (change, category) in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(_Rejected) as caught:
                    _validate(edit(change), self.task, self.snapshot, ON)
                self.assertEqual(caught.exception.category, category)
        without = {key: value for key, value in self.good.items() if key != "relations"}
        with self.assertRaises(_Rejected) as caught:
            _validate(without, self.task, self.snapshot, ON)
        self.assertEqual(caught.exception.category, "shape")
        with self.assertRaises(_Rejected) as caught:  # the original contract never accepts a relations array
            _validate(self.good, self.task, self.snapshot, ReviewOptions())
        self.assertEqual(caught.exception.category, "shape")


class RelationPersistenceTests(ExecutionCase):
    def test_relations_survive_checkpoint_resume_and_bind_into_a_registry(self):
        root, snapshot, graph = self.scope({PATH: SOURCE})
        self.forget(root)  # any later read of the repository would fail
        runs = self.runs()
        first = review_repository(snapshot, graph, Scripted(with_relations), system=SYSTEM, options=ON,
                                  checkpoint_dir=runs / "a")
        self.assertEqual(first["relations"]["status"], "collected")
        kinds = {item["kind"] for item in first["relations"]["items"]}
        self.assertTrue({"call", "branch", "return", "callback"} <= kinds)
        resolutions = {item["resolution"] for item in first["relations"]["items"]}
        self.assertTrue({"resolved", "unresolved", "external"} <= resolutions)

        def refuse(brief, number):
            raise AssertionError("a reused checkpoint must not call the model")
        again = review_repository(snapshot, graph, Scripted(refuse), system=SYSTEM, options=ON,
                                  checkpoint_dir=runs / "b", resume_from=runs / "a")
        self.assertEqual(again["ledger"]["summary"]["model_calls"], 0)
        self.assertEqual(again["relations"], first["relations"])
        registry = build_registry(again, graph)
        self.assertEqual(registry["status"], "collected")
        self.assertEqual(len(registry["worker"]), len(first["relations"]["items"]))
        self.assertTrue(all(row["origin"] == "host_static" and row["kind"] == "static_call"
                            for row in registry["host_static"]))
        self.assertEqual(check_registry(registry, snapshot_id=snapshot.snapshot_id,
                                        review_run_fingerprint=again["run_fingerprint"]), registry)
        self.assertEqual(binding(registry)["worker"], len(registry["worker"]))
        tampered = copy.deepcopy(registry)
        tampered["worker"][0]["resolution"] = "resolved" if tampered["worker"][0]["resolution"] != "resolved" else "external"
        for label, value, arguments in (
                ("changed row", tampered, {"snapshot_id": snapshot.snapshot_id,
                                           "review_run_fingerprint": again["run_fingerprint"]}),
                ("foreign review", registry, {"snapshot_id": snapshot.snapshot_id, "review_run_fingerprint": "0" * 64}),
                ("foreign snapshot", registry, {"snapshot_id": "f" * 64,
                                                "review_run_fingerprint": again["run_fingerprint"]})):
            with self.subTest(case=label):
                with self.assertRaises(RelationError):
                    check_registry(value, **arguments)

    def test_an_original_identity_cache_stays_usable_with_relations_not_collected_and_never_rebinds(self):
        _, snapshot, graph = self.scope({PATH: SOURCE})
        runs = self.runs()
        legacy = review_repository(snapshot, graph, Scripted(), system=SYSTEM, options=ReviewOptions(),
                                   checkpoint_dir=runs / "a")
        self.assertEqual((legacy["relations"]["status"], legacy["relations"]["items"]), ("not_collected", []))
        registry = build_registry(legacy, graph)
        self.assertEqual((registry["status"], registry["worker"]), ("not_collected", []))
        self.assertTrue(registry["host_static"])  # host navigation stays available, never relabelled as worker relations
        forged = copy.deepcopy(registry)
        forged["worker"] = [dict(registry["host_static"][0], origin="worker_proposal")]
        forged["summary"]["worker"] = 1
        from process_observability.technical_relations import _sha
        forged["registry_sha256"] = _sha({key: value for key, value in forged.items() if key != "registry_sha256"})
        with self.assertRaises(RelationError):
            check_registry(forged, snapshot_id=snapshot.snapshot_id, review_run_fingerprint=legacy["run_fingerprint"])
        reused = review_repository(snapshot, graph, Scripted(), system=SYSTEM, options=ReviewOptions(),
                                   checkpoint_dir=runs / "b", resume_from=runs / "a")
        self.assertEqual(reused["ledger"]["summary"]["model_calls"], 0)
        self.assertEqual(reused["relations"]["status"], "not_collected")
        # The new relation contract is a new technical identity: the old cache is refused, never relabelled.
        with self.assertRaises(ReviewResumeError) as caught:
            review_repository(snapshot, graph, Scripted(), system=SYSTEM, options=ON, checkpoint_dir=runs / "c",
                              resume_from=runs / "a")
        self.assertIn("options", caught.exception.categories)


if __name__ == "__main__":
    unittest.main()
