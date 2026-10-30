import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from process_observability.contracts import (
    Applicability, ArtifactStatus, Assessment, Authority, ContractError,
    CoverageSummary, Decision, EventMapping, EvidenceRef, FindingState,
    InstrumentationAction, InstrumentationCard, InstrumentationPlan, LogCoverage,
    MetricCatalog, MetricSpec, Opportunities, OpportunityState, ProcessMap, ProcessScenario,
    Readiness, RequiredFact, RuleRef, SourceAnchor, SourceKind, SourceSnapshot,
    Stage, Transition, ValidationState, load_trusted_decisions,
    parse_model_proposal, strict_json_loads, validate_evidence_links,
)


def header():
    return dict(run_id="synthetic-test", input_fingerprints={"business": "synthetic-hash"},
                status=ArtifactStatus.DRAFT, evidence_ids=[])


def process_map():
    return ProcessMap(**header(), purpose="Fulfil a synthetic request", boundaries=None,
                      case_definition=None, stages=[Stage(id="receive", name="Receive"),
                                                     Stage(id="cancel", name=None)],
                      transitions=[], event_mappings=[], unknowns=["Cancellation path not established"])


def metric():
    return MetricSpec(id="duration", purpose="Locate delay", formula_id=None, formula_text=None,
                      counting_unit="case", cohort=None, window=None, time_semantics=None,
                      open_case_policy=None, late_data_policy=None, dependencies=["completion"],
                      readiness=Readiness(primary="telemetry_missing", blockers=["No runtime records", "No method"]),
                      value_reason="No supported calculation")


def fact(fact_id="completion", accepted=False, applicability=Applicability.UNKNOWN):
    return RequiredFact(id=fact_id, meaning="Request completed", rationale="Duration endpoint",
                        scope="Synthetic process", condition=None,
                        requirement=Assessment(state=FindingState.ACCEPTED if accepted else FindingState.PROPOSED,
                                               decision_id="synthetic-decision" if accepted else None),
                        applicability=applicability, metric_ids=["duration"], evidence_alternatives=["Result record"],
                        instrumentation="unknown", opportunities=Opportunities(), delivery="unknown", quality="unknown")


def coverage():
    return LogCoverage(**header(), facts=[fact(accepted=True), fact("candidate")],
                       summary=CoverageSummary(scope_provisional=True, numerator=0, denominator=1,
                                               unknown=1, candidate_fact_ids=["candidate"], excluded_fact_ids=[]))


def instrumentation():
    card = InstrumentationCard(id="map-completion", fact_id="completion", action=InstrumentationAction.MAP,
                               rationale="Try mapping existing records first", semantics="Committed completion",
                               fields={"case_id": "Business request identity"}, transaction_timing=None,
                               dependencies=[], metric_ids=["duration"], validation_plan="Check committed outcome and delivery")
    return InstrumentationPlan(**header(), cards=[card])


class ContractTests(unittest.TestCase):
    def test_each_output_round_trips_with_explicit_unknowns(self):
        for artifact in [process_map(), coverage(), instrumentation(), MetricCatalog(**header(), metrics=[metric()])]:
            with self.subTest(artifact=type(artifact).__name__):
                self.assertEqual(type(artifact).from_json(artifact.to_json()), artifact)
                if not isinstance(artifact, LogCoverage):
                    self.assertEqual(parse_model_proposal(type(artifact), artifact.to_dict()), artifact)
                self.assertEqual(artifact.to_dict()["run_id"], "synthetic-test")
        self.assertIsNone(metric().value)
        self.assertFalse(process_map().stages[1].alternatives_complete)
        proposed = LogCoverage(**header(), facts=[fact()], summary=CoverageSummary(
            scope_provisional=True, numerator=0, denominator=0, unknown=0,
            candidate_fact_ids=["completion"], excluded_fact_ids=[]))
        self.assertEqual(parse_model_proposal(LogCoverage, proposed.to_dict()), proposed)
        with self.assertRaisesRegex(ContractError, "Model cannot"):
            parse_model_proposal(LogCoverage, coverage().to_dict())

    def test_unknown_fields_and_nested_types_are_rejected(self):
        original = process_map().to_dict()
        variants = []
        for key, value in [("extra", "ignored?"), ("run_id", "  "), ("status", "invented"),
                           ("stages", "not a list"), ("input_fingerprints", {"business": 7})]:
            payload = copy.deepcopy(original);payload[key] = value;variants.append(payload)
        payload = copy.deepcopy(original);payload["stages"][0]["surprise"] = True;variants.append(payload)
        payload = copy.deepcopy(original);payload["stages"][0]["alternatives_complete"] = "false";variants.append(payload)
        payload = copy.deepcopy(original);del payload["run_id"];variants.append(payload)
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(ContractError):
                ProcessMap.from_dict(payload)

    def test_blocked_and_incomplete_artifacts_need_reasons(self):
        for status in (ArtifactStatus.BLOCKED, ArtifactStatus.INCOMPLETE):
            with self.subTest(status=status), self.assertRaises(ContractError):
                replace(process_map(), status=status)
            artifact = replace(process_map(), status=status, blockers=["Source unavailable"])
            self.assertEqual(artifact.blockers, ["Source unavailable"])

    def test_all_known_metric_blockers_survive_round_trip(self):
        value = metric()
        self.assertEqual(MetricSpec.from_json(value.to_json()).readiness.blockers,
                         ["No runtime records", "No method"])
        for primary in ("unknown", "incomplete", "method_missing", "telemetry_missing", "partial_input"):
            with self.subTest(primary=primary), self.assertRaises(ContractError):
                Readiness(primary=primary, blockers=[])

    def test_model_cannot_supply_trusted_fields_at_root_or_nested(self):
        for key in ("authority", "verified", "accepted", "not_applicable_approved", "text_verified", "semantic_verified"):
            for nested in (False, True):
                payload = process_map().to_dict()
                target = payload["stages"][0] if nested else payload
                target[key] = False if key == "verified" else "accepted"
                with self.subTest(key=key, nested=nested), self.assertRaisesRegex(ContractError, "Model cannot"):
                    parse_model_proposal(ProcessMap, payload)

    def test_model_cannot_promote_states_via_nested_records(self):
        for state in ("verified", "accepted", "observed", "not_applicable_approved", "implemented", "ready", "usable", "complete"):
            payload = process_map().to_dict();payload["stages"][0]["required"]["state"] = state
            with self.subTest(state=state), self.assertRaisesRegex(ContractError, "Model cannot"):
                parse_model_proposal(ProcessMap, payload)
        for observed in (True, "observed", {"state": "observed", "evidence_ids": ["fabricated"]}):
            payload = process_map().to_dict();payload["stages"][0]["observed"] = observed
            with self.subTest(observed=observed), self.assertRaisesRegex(ContractError, "Model cannot"):
                parse_model_proposal(ProcessMap, payload)

    def test_model_does_not_enter_the_trusted_decisions_loader(self):
        sample = load_trusted_decisions(Path(__file__).parents[1] / "examples" / "decisions.json")[0]
        with self.assertRaises(ContractError):
            parse_model_proposal(Decision, sample.to_dict())
        with self.assertRaises(ContractError):
            parse_model_proposal(ProcessMap, {1: "non-string key"})

    def test_source_kinds_and_snapshot_identity_remain_distinct(self):
        self.assertEqual(len(set(SourceKind)), 4)
        for kind in SourceKind:
            snapshot = SourceSnapshot(id="s1", kind=kind, role="supplied", content_hash="synthetic-hash",
                                      acquisition_method="local test")
            self.assertEqual(SourceSnapshot.from_json(snapshot.to_json()).kind, kind)
        anchor = SourceAnchor(snapshot_id="s1", path="app.py", symbol="fulfil_request", start_line=1, end_line=2)
        with self.assertRaises(ContractError):
            EvidenceRef(id="e1", kind=SourceKind.CODE, snapshot_id="s2", locator="app.py", source_anchor=anchor)

    def test_non_telemetry_evidence_never_proves_runtime_observation(self):
        anchor = SourceAnchor(snapshot_id="s1", path="app.py", symbol="fulfil_request", start_line=1, end_line=2)
        for kind in (SourceKind.CODE, SourceKind.DOCUMENT, SourceKind.UPSTREAM):
            with self.subTest(kind=kind), self.assertRaises(ContractError):
                EvidenceRef(id="e1", kind=kind, snapshot_id="s1", locator="source",
                            source_anchor=anchor if kind is SourceKind.CODE else None,
                            claim_state=FindingState.OBSERVED)
        code = EvidenceRef(id="e1", kind=SourceKind.CODE, snapshot_id="s1", locator="app.py", source_anchor=anchor)
        observed = Stage(id="receive", name="Receive", observed=Assessment(state=FindingState.OBSERVED, evidence_ids=["e1"]))
        artifact = replace(process_map(), stages=[observed])
        with self.assertRaisesRegex(ContractError, "verified telemetry"):
            validate_evidence_links(artifact, [code])
        unverified = EvidenceRef(id="e1", kind=SourceKind.TELEMETRY, snapshot_id="s1", locator="logs.jsonl:1")
        with self.assertRaises(ContractError):
            validate_evidence_links(artifact, [unverified])
        verified = replace(unverified, text_validation=ValidationState.VERIFIED,
                           semantic_validation=ValidationState.VERIFIED, raw_span_hash="synthetic-span-hash")
        validate_evidence_links(artifact, [verified])
        with self.assertRaisesRegex(ContractError, "Unresolved"):
            validate_evidence_links(artifact, [])

    def test_approved_document_metadata_does_not_approve_its_rules(self):
        rule = RuleRef(id="r1", provenance="Supplied proposal", source_locator="document fragment")
        self.assertEqual(rule.authority, Authority.UNKNOWN)
        self.assertEqual(rule.data_validation, ValidationState.UNVERIFIED)
        with self.assertRaises(ContractError):
            replace(rule, authority=Authority.ACCEPTED)
        with self.assertRaises(ContractError):
            replace(rule, applicability=Applicability.NOT_APPLICABLE)

    def test_known_zero_needs_independent_scope_and_window(self):
        for kwargs in ({"state": OpportunityState.ZERO, "count": 0},
                       {"state": OpportunityState.ZERO, "count": 0, "scope_complete": True,
                        "independent_evidence_ids": ["controlled-inputs"]},
                       {"state": OpportunityState.UNKNOWN, "count": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ContractError):
                Opportunities(**kwargs)
        zero = Opportunities(state=OpportunityState.ZERO, count=0, independent_evidence_ids=["controlled-inputs"],
                             scope_complete=True, window="synthetic controlled population")
        self.assertEqual(Opportunities.from_json(zero.to_json()), zero)
        with self.assertRaises(ContractError):
            replace(zero, count=False)

    def test_unknown_accepted_requirements_stay_in_denominator(self):
        report = coverage()
        self.assertEqual(report.summary.denominator, 1)
        self.assertEqual(report.summary.candidate_fact_ids, ["candidate"])
        with self.assertRaises(ContractError):
            replace(report, summary=replace(report.summary, denominator=0, unknown=0))
        with self.assertRaises(ContractError):
            replace(report, summary=replace(report.summary, numerator=1, unknown=0))
        with self.assertRaises(ContractError):
            replace(report, summary=replace(report.summary, scope_provisional=False))

    def test_cross_references_and_instrumentation_location_are_explicit(self):
        with self.assertRaises(ContractError):
            replace(process_map(), transitions=[Transition(from_stage="receive", to_stage="invented", condition=None)])
        with self.assertRaises(ContractError):
            replace(instrumentation().cards[0], location="anchored")
        self.assertEqual({action.value for action in InstrumentationAction},
                         {"map_existing", "join", "enrich", "add_field", "add_event", "move_emit", "fix_delivery"})
        with self.assertRaises(ContractError):
            EventMapping(event_key="click", business_action="Review", operation="Review", stage_id="receive",
                         lifecycle_role="ui", occurrence_rule="")

    def test_card_dependency_cycles_are_rejected_without_recursion(self):
        def card(card_id, *dependencies):
            return replace(instrumentation().cards[0], id=card_id, dependencies=list(dependencies))
        # Branching DAG with a fact dependency: the same structure a proposal or saved run may hold.
        dag = replace(instrumentation(), cards=[card("a", "completion"), card("b", "a"), card("c", "a", "b", "completion")])
        self.assertEqual(InstrumentationPlan.from_json(dag.to_json()), dag)
        self.assertEqual(parse_model_proposal(InstrumentationPlan, dag.to_dict()), dag)
        for cards in ([card("a", "a")], [card("a", "b"), card("b", "a")],
                      [card("a", "completion", "c"), card("b", "a"), card("c", "b")],
                      [card("free"), card("b", "c"), card("c", "b")]):
            with self.subTest(cards=[(item.id, item.dependencies) for item in cards]):
                with self.assertRaisesRegex(ContractError, "cycle"):
                    replace(instrumentation(), cards=cards)
                payload = dag.to_dict()
                payload["cards"] = [item.to_dict() for item in cards]
                with self.assertRaisesRegex(ContractError, "cycle"):
                    InstrumentationPlan.from_dict(payload)
                with self.assertRaisesRegex(ContractError, "cycle"):
                    parse_model_proposal(InstrumentationPlan, payload)
        # A chain longer than the recursion limit is checked iteratively.
        chain = [card("c0")] + [card(f"c{index}", f"c{index - 1}") for index in range(1, 3000)]
        long_plan = replace(instrumentation(), cards=chain)
        self.assertEqual(InstrumentationPlan.from_json(long_plan.to_json()), long_plan)
        with self.assertRaisesRegex(ContractError, "cycle"):
            replace(long_plan, cards=[card("c0", "c2999"), *chain[1:]])

    def test_metric_formula_text_is_inert_and_planned_logs_have_no_value(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "must-not-exist"
            malicious = f"__import__('pathlib').Path({str(target)!r}).write_text('executed')"
            proposed = replace(metric(), formula_id="unregistered_method", formula_text=malicious)
            restored = MetricSpec.from_json(proposed.to_json())
            self.assertEqual(restored.formula_text, malicious)
            self.assertFalse(target.exists())
        with self.assertRaises(ContractError):
            replace(metric(), value=0)
        with self.assertRaises(ContractError):
            replace(metric(), value_reason=None)

    def test_json_rejects_duplicate_nonfinite_and_bool_as_number(self):
        for text in ('{"a": 1, "a": 2}', '{"value": NaN}', '{"value": Infinity}', '{"value": -Infinity}'):
            with self.subTest(text=text), self.assertRaises(ContractError):
                strict_json_loads(text)
        for value in (True, float("inf"), float("nan"), 10 ** 1000):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ContractError):
                replace(metric(), value=value)
        payload = metric().to_dict();payload["value"] = 1.0
        text = json.dumps(payload).replace('"value": 1.0', '"value": 1e999')
        with self.assertRaises(ContractError):
            MetricSpec.from_json(text)

    def test_optional_trusted_decisions_and_malformed_inputs(self):
        self.assertEqual(load_trusted_decisions(), [])
        example = json.loads((Path(__file__).parents[1] / "examples" / "decisions.json").read_text())
        cases = []
        missing = copy.deepcopy(example);del missing["decisions"][0]["basis"];cases.append(missing)
        duplicate = copy.deepcopy(example);duplicate["decisions"].append(copy.deepcopy(duplicate["decisions"][0]));cases.append(duplicate)
        bad_date = copy.deepcopy(example);bad_date["decisions"][0]["decided_at"] = "yesterday";cases.append(bad_date)
        empty = copy.deepcopy(example);empty["decisions"][0]["scope"] = " ";cases.append(empty)
        for payload in cases:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "decisions.json";path.write_text(json.dumps(payload))
                with self.assertRaises(ContractError):
                    load_trusted_decisions(path)

    def test_model_cannot_claim_deterministic_coverage_or_fake_decisions(self):
        base = LogCoverage(**header(), facts=[fact()], summary=CoverageSummary(
            scope_provisional=True, numerator=0, denominator=0, unknown=0,
            candidate_fact_ids=["completion"], excluded_fact_ids=[])).to_dict()
        changes = [("applicability", "not_applicable_approved"), ("decision_id", "fabricated"),
                   ("instrumentation", "found"), ("delivery", "present"), ("quality", "usable")]
        for key, value in changes:
            payload = copy.deepcopy(base);payload["facts"][0][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ContractError, "Model cannot"):
                parse_model_proposal(LogCoverage, payload)
        payload = copy.deepcopy(base)
        payload["facts"][0]["opportunities"] = dict(state="known_zero", count=0,
            independent_evidence_ids=["fabricated"], scope_complete=True, window="any")
        with self.assertRaisesRegex(ContractError, "Model cannot"):
            parse_model_proposal(LogCoverage, payload)
        payload = process_map().to_dict();payload["stages"][0]["alternatives_complete"] = True
        with self.assertRaisesRegex(ContractError, "Model cannot"):
            parse_model_proposal(ProcessMap, payload)

    def test_provenance_is_bound_to_orchestrator_context(self):
        payload = process_map().to_dict()
        parse_model_proposal(ProcessMap, payload, expected_run_id=payload["run_id"],
                             expected_fingerprints=payload["input_fingerprints"])
        for expected in ({"expected_run_id": "another"}, {"expected_fingerprints": {"business": "another"}}):
            with self.subTest(expected=expected), self.assertRaises(ContractError):
                parse_model_proposal(ProcessMap, payload, **expected)

    def test_unknown_dimensions_cannot_be_hidden_by_summary(self):
        unknown = replace(fact(accepted=True), applicability=Applicability.APPLICABLE)
        for numerator, count in [(1, 0), (0, 0)]:
            with self.subTest(numerator=numerator), self.assertRaises(ContractError):
                LogCoverage(**header(), facts=[unknown], summary=CoverageSummary(
                    scope_provisional=False, numerator=numerator, denominator=1, unknown=count,
                    candidate_fact_ids=[], excluded_fact_ids=[]))
        with self.assertRaises(ContractError):
            replace(unknown, delivery="missing", quality="usable")

    def test_deep_and_long_json_are_deliberate_contract_errors(self):
        for text in ("[" * 10000, '{"n":' + "1" * 5000 + "}", '{"n":1e999}'):
            with self.subTest(size=len(text)), self.assertRaises(ContractError):
                strict_json_loads(text)
        with self.assertRaises(ContractError):
            replace(process_map(), unknowns=[" "])
        with self.assertRaises(ContractError):
            replace(metric(), dependencies=["duplicate", "duplicate"])

    def test_contract_errors_carry_a_stable_code_and_declared_field_with_unchanged_messages(self):
        looping = replace(instrumentation().cards[0], id="a", dependencies=["a"])
        cases = {
            "duplicate list value": (lambda: replace(metric(), dependencies=["duplicate", "duplicate"]),
                                     "duplicate_values", "dependencies", "dependencies: duplicate values"),
            "lifecycle role": (lambda: EventMapping(event_key="click", business_action="Review", operation="Review",
                                                    stage_id="receive", lifecycle_role="committed",
                                                    occurrence_rule="Once"),
                               "invalid_enum", "lifecycle_role", "Unsupported event lifecycle role"),
            "unknown stage": (lambda: replace(process_map(), transitions=[
                Transition(from_stage="receive", to_stage="invented", condition=None)]),
                "unknown_reference", "transitions", "Transition references an unknown stage"),
            "empty scenario": (lambda: ProcessScenario(id="s", name="Scenario"), "empty_scenario", "operation_ids",
                               "A scenario needs at least one operation"),
            "cycle": (lambda: replace(instrumentation(), cards=[looping]), "dependency_cycle", "cards",
                      "Instrumentation card dependencies form a cycle"),
            "blank declared text": (lambda: replace(process_map(), unknowns=[" "]), "blank_text", None,
                                    "ProcessMap.unknowns[0]: unknown text must be null, not blank"),
        }
        for label, (build, code, field, message) in cases.items():
            with self.subTest(label), self.assertRaises(ContractError) as caught:
                build()
            self.assertEqual((caught.exception.code, caught.exception.field, str(caught.exception)),
                             (code, field, message))
        generic = ContractError("Invalid JSON")
        self.assertEqual((generic.code, generic.field, str(generic)), ("contract", None, "Invalid JSON"))

    def test_lifecycle_roles_and_concrete_operation_identity_with_legacy_loading(self):
        from process_observability.contracts import LIFECYCLE_ROLES, ProcessOperation, ROLE_SUPPORT

        def mapping(**changes):
            return EventMapping(**{"event_key": "refund.duplicate", "business_action": "Повтор возврата",
                                   "operation": "Возврат", "stage_id": "receive", "lifecycle_role": "duplicate",
                                   "occurrence_rule": "Не новое выполнение", **changes})
        operation = ProcessOperation(id="refund", name="Возврат", stage_id="receive")
        other = ProcessOperation(id="cancel-op", name="Отмена", stage_id="cancel")
        self.assertTrue(set(LIFECYCLE_ROLES) >= {"refusal", "retry", "duplicate", "no_op", "request", "result"})
        self.assertEqual(LIFECYCLE_ROLES[:6], ("request", "start", "result", "error", "ui", "unknown"))
        for role in LIFECYCLE_ROLES:
            self.assertEqual(mapping(lifecycle_role=role).lifecycle_role, role)
        concrete = replace(process_map(), operations=[operation, other], event_mappings=[
            mapping(operation_id="refund", role_basis="Ранний выход при уже обработанном возврате",
                    role_support="emit_within_operation_sources")])
        self.assertEqual(ProcessMap.from_json(concrete.to_json()), concrete)
        # Invalid identities never pass as validated mappings: unknown operation or another stage's operation.
        for bad in ("invented", "cancel-op"):
            with self.subTest(operation_id=bad), self.assertRaises(ContractError) as caught:
                replace(concrete, event_mappings=[mapping(operation_id=bad)])
            self.assertEqual((caught.exception.code, caught.exception.field), ("unknown_reference", "event_mappings"))
        # Blank optional text matches neither declared alternative (nonblank text or null).
        for field, value, code in (("role_support", "verified", "invalid_enum"), ("operation_id", " ", "type"),
                                   ("role_basis", "", "type"), ("lifecycle_role", "noop", "invalid_enum")):
            with self.subTest(field=field), self.assertRaises(ContractError) as caught:
                mapping(**{field: value})
            self.assertEqual(caught.exception.code, code)
        self.assertNotIn("verified", ROLE_SUPPORT)
        # A saved mapping from before this change has none of the new fields and loads as an unassessed label.
        legacy = mapping().to_dict()
        for key in ("operation_id", "role_basis", "role_support"):
            legacy.pop(key)
        legacy.pop("reconciliation")
        loaded = EventMapping.from_dict({**legacy, "lifecycle_role": "result"})
        self.assertEqual((loaded.operation_id, loaded.role_basis, loaded.role_support, loaded.reconciliation),
                         (None, None, "not_assessed", "not_assessed"))
        with self.assertRaises(ContractError) as caught:
            mapping(reconciliation="resolved")
        self.assertEqual(caught.exception.code, "invalid_enum")

    def test_fact_lifecycle_requirement_and_host_check_are_typed_and_backwards_compatible(self):
        from process_observability.contracts import LIFECYCLE_CHECKS
        stored = fact().to_dict()
        for key in ("lifecycle_roles", "lifecycle_check"):
            stored.pop(key)
        loaded = RequiredFact.from_dict(stored)  # a fact saved before this change
        self.assertEqual((loaded.lifecycle_roles, loaded.lifecycle_check), ([], "not_assessed"))
        repeat = replace(fact(), lifecycle_roles=["duplicate"], instrumentation="found", lifecycle_check="compatible")
        self.assertEqual(RequiredFact.from_json(repeat.to_json()), repeat)
        for changes, code in (({"lifecycle_roles": ["success"]}, "invalid_enum"),
                              ({"lifecycle_roles": ["result", "result"]}, "duplicate_values"),
                              ({"lifecycle_check": "found"}, "invalid_enum")):
            with self.subTest(changes=changes), self.assertRaises(ContractError) as caught:
                replace(fact(), **changes)
            self.assertEqual(caught.exception.code, code)
        for check in set(LIFECYCLE_CHECKS) - {"not_assessed", "compatible"}:
            with self.subTest(check=check), self.assertRaises(ContractError):
                replace(fact(), instrumentation="found", lifecycle_check=check)  # never "found" after a failed check
            self.assertEqual(replace(fact(), lifecycle_check=check).instrumentation, "unknown")


if __name__ == "__main__":
    unittest.main()
