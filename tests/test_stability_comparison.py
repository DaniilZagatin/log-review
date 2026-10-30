"""Matching must expose ambiguity and never turn repeatability into correctness."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from pko.audit import audit_run
from pko.scenario_map.stability import compare_maps, mutation_snapshot, record_mutation, audit_statistics


def node(id, line, name="Действие"):
    return {"id": id, "name": name, "outcome": "unknown", "evidence": [
        {"path": "source-1/process.py", "line_start": line, "line_end": line}], "external_interactions": []}


def sample():
    return {"sources": [{"id": "source-1", "digest": "sha256:example"}],
            "scenario": {"description": "Один документ", "entry_operation_ids": ["a"], "terminal_operation_ids": ["b"]},
            "nodes": [node("a", 1), node("b", 2)],
            "edges": [{"id": "e", "source": "a", "target": "b", "kind": "branch", "condition": "Допустимый формат"}],
            "run": {}, "execution_reviews": []}


class ComparisonTests(unittest.TestCase):
    def test_id_rename_is_not_a_route_change(self):
        a, b = sample(), sample()
        for n, new in zip(b["nodes"], ["x", "y"]):
            n["id"] = new
        b["scenario"].update(entry_operation_ids=["x"], terminal_operation_ids=["y"])
        b["edges"][0].update(source="x", target="y", id="new")
        b["nodes"][0]["name"] = "Получить документ"
        result = compare_maps(a, b)
        self.assertEqual(len(result["matches"]), 2)
        self.assertFalse(result["transitions"]["only_left"])
        self.assertFalse(result["scope_changes"])
        self.assertEqual(result["changed_operations"][0]["fields"], ["name"])
        self.assertIs(result["semantic_verification"], False)
        self.assertEqual(result["identity"]["configuration"], "unknown")

    def test_same_id_different_anchors_not_assumed_same_operation(self):
        a, b = sample(), sample()
        b["nodes"][0]["evidence"][0].update(line_start=9, line_end=9)
        result = compare_maps(a, b)
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(len(result["unresolved_operations"]), 2)
        self.assertTrue(result["transitions"]["unresolved"])

    def test_shared_function_range_stays_ambiguous(self):
        a, b = sample(), sample()
        for n in a["nodes"] + b["nodes"]:
            n["evidence"][0].update(line_start=1, line_end=10)
        result = compare_maps(a, b)
        self.assertFalse(result["matches"])
        self.assertTrue(all(n["status"] == "ambiguous_or_split" for n in result["unresolved_operations"]))

    def test_changed_condition_and_outcome_are_visible(self):
        a, b = sample(), sample()
        b["edges"][0]["condition"] = "Любой формат"
        b["nodes"][1]["outcome"] = "success"
        result = compare_maps(a, b)
        self.assertEqual(result["transitions"]["only_right"][0]["condition"], "Любой формат")
        self.assertIn("outcome", result["changed_operations"][0]["fields"])

    def test_configuration_compares_budgets_and_requires_build_identity(self):
        a,b=sample(),sample()
        for m in (a,b):
            m["run"].update(reproducibility={"model":"fixture"}, max_steps=300,max_seconds=7200,
                            model_timeout_seconds=900,model_mode="standard",build={"code_sha256":"same"})
        self.assertEqual(compare_maps(a,b)["identity"]["configuration"],"same")
        b["run"]["max_steps"]=200
        self.assertEqual(compare_maps(a,b)["identity"]["configuration"],"different")
        b["run"].pop("build")
        self.assertEqual(compare_maps(a,b)["identity"]["configuration"],"unknown")

    def test_source_mismatch_and_missing_source_digest_are_not_gradeable(self):
        a, b = sample(), sample()
        b["sources"][0]["digest"] = "different"
        self.assertEqual(compare_maps(a,b)["comparison_status"], "incomparable_sources")
        b["sources"][0].pop("digest")
        self.assertEqual(compare_maps(a,b)["identity"]["sources"], "unknown")

    def hierarchy(self, model, membership):
        from pko.stages.inputs import map_revision, input_fingerprint, methodology
        return {"hierarchy_version":"1.1", "map_revision":map_revision(model),
                "input_sha256":input_fingerprint(model,"full"), "input_mode":"full",
                "methodology_sha256":methodology()["sha256"], "authority_status":"proposed",
                "membership":membership, "stages":[{"id":s,"name":s,"collapsible":False}
                                                   for s in sorted(set(membership.values()))], "unassigned":[]}

    def test_group_identity_does_not_depend_on_group_id(self):
        m = sample()
        left = self.hierarchy(m,{"a":"stage-1","b":"stage-1"})
        same = compare_maps(m,m,left,self.hierarchy(m,{"a":"stage-9","b":"stage-9"}))
        self.assertFalse(same["grouping"]["different_pairs_left_ids"])
        split = compare_maps(m,m,left,self.hierarchy(m,{"a":"stage-9","b":"stage-2"}))
        self.assertEqual(split["grouping"]["different_pairs_left_ids"], [["a","b"]])

    def test_stale_grouping_input_is_not_compared(self):
        m=sample()
        hierarchy=self.hierarchy(m,{"a":"stage-1","b":"stage-1"})
        m["gaps"]=[{"id":"gap-1","description":"Недоступное продолжение","node_ids":["b"]}]
        result=compare_maps(m,m,hierarchy,hierarchy)
        self.assertEqual(result["grouping"]["status"],"unverified_input")

    def test_comparison_does_not_modify_input(self):
        a, b = sample(), sample()
        expected = deepcopy((a,b))
        self.assertEqual(compare_maps(a,b), compare_maps(a,b))
        self.assertEqual((a,b), expected)

    def test_observable_revision_separates_rejection_and_acceptance(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"audit.txt"
            m = sample()
            before = mutation_snapshot(m)
            with audit_run(path, "test"):
                record_mutation(before,m,"patch_fragment",{"ok":False,"code":"invalid_arguments"})
                m["scenario"]["terminal_operation_ids"] = ["a"]
                record_mutation(before,m,"patch_fragment",{"ok":True,"stale_review_ids":["r1"]})
            entries = []
            import re
            for part in re.split(r"^===.*===\s*$",path.read_text(encoding="utf-8"),flags=re.M):
                try: item=json.loads(part)
                except ValueError: continue
                if item.get("event")=="map_revision": entries.append(item)
            self.assertEqual(len(entries),2)
            self.assertEqual(entries[0]["revision_before"],entries[0]["revision_after"])
            self.assertNotEqual(entries[1]["revision_before"],entries[1]["revision_after"])
            self.assertIn("terminal_operation_ids",entries[1]["scope_changes"])
            self.assertEqual(entries[1]["stale_review_ids"],["r1"])


class AuditStatisticsTests(unittest.TestCase):
    def test_wrappers_are_not_double_counted_and_incomplete_tail_is_visible(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"audit.txt"
            events=[{"event":"tool_started","name":"patch_fragment"},
                    {"event":"scenario_tool","name":"patch_fragment","result":{"ok":False}},
                    {"event":"scenario_tool","name":"read_file","result":{"ok":True}},
                    {"event":"scenario_tool","name":"patch_fragment","result":{"ok":True}}]
            path.write_text("".join(f"=== {i} | stamp | event ===\n"+json.dumps(e)+"\n" for i,e in enumerate(events))+"=== 9 | stamp | tail ===\n{",encoding="utf-8")
            result=audit_statistics([path])
            self.assertEqual(result["tool_calls"],3)
            self.assertEqual(result["mutation_review_rejection_rate"],0.5)
            self.assertEqual(result["incomplete_events"],1)
