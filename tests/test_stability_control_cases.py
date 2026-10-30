"""Control rubrics catch known losses; this is not an LLM quality measurement."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"backend"))
from pko.scenario_map.evaluation import evaluate_map

CASES=json.loads((Path(__file__).with_name("fixtures")/"stability_control_cases.json").read_text(encoding="utf-8"))["cases"]
CONDITIONS={
 "invalid-details":["", "Реквизиты валидны", "Реквизиты невалидны, платёж стандартный", "Реквизиты невалидны, платёж нестандартный"],
 "empty-search":["Договор найден", "Результат пустой, платёж стандартный", "Результат пустой, платёж нестандартный"],
 "contract-present":["договор заполнен", "договор отсутствует"],
}

def annotated_map(case):
    nodes=[{"id":op["id"],"name":op["label"],"outcome":"unknown","evidence":[{"path":case["source"]["path"],"line_start":op["evidence"]["line"],"line_end":op["evidence"]["line"]}]} for op in case["rubric"]["expected_operations"]]
    edges=[{"id":f"e{i}","source":r["source"],"target":r["target"],"condition":CONDITIONS.get(case["id"],[""]*len(case["rubric"]["expected_transitions"]))[i]} for i,r in enumerate(case["rubric"]["expected_transitions"])]
    return {"nodes":nodes,"edges":edges}


class StabilityRubricTests(unittest.TestCase):
    def test_control_examples_and_every_removed_action(self):
        for case in CASES:
            with self.subTest(case=case["id"]):
                model=annotated_map(case)
                self.assertTrue(evaluate_map(model,case["rubric"])["passed"])
                for node in model["nodes"]:
                    lost=deepcopy(model)
                    lost["nodes"]=[n for n in lost["nodes"] if n["id"]!=node["id"]]
                    self.assertFalse(evaluate_map(lost,case["rubric"])["passed"])

    def test_every_missing_branch_and_erased_condition(self):
        for case in CASES[:3]:
            for index in range(len(case["rubric"]["expected_transitions"])):
                with self.subTest(case=case["id"],edge=index):
                    model=annotated_map(case)
                    model["edges"].pop(index)
                    self.assertFalse(evaluate_map(model,case["rubric"])["passed"])
                    if "condition_pattern" in case["rubric"]["expected_transitions"][index]:
                        model=annotated_map(case)
                        model["edges"][index]["condition"]="Любой результат"
                        self.assertFalse(evaluate_map(model,case["rubric"])["passed"])

    def test_swapping_standard_and_nonstandard_routes_is_not_accepted(self):
        for case in CASES[:2]:
            model=annotated_map(case)
            a,b=model["edges"][-2:]
            a["condition"],b["condition"]=b["condition"],a["condition"]
            self.assertFalse(evaluate_map(model,case["rubric"])["passed"])
        case=CASES[0]
        model=annotated_map(case)
        model["edges"][1]["condition"]="Реквизиты невалидны"
        self.assertFalse(evaluate_map(model,case["rubric"])["passed"])

    def test_merged_external_commands_are_not_depth(self):
        for case in CASES[3:]:
            model=annotated_map(case)
            first,last=model["nodes"]
            first["evidence"][0]["line_end"]=last["evidence"][0]["line_end"]
            model["nodes"]=[first]
            model["edges"]=[]
            self.assertIn("collapsed_operations",[f["code"] for f in evaluate_map(model,case["rubric"])["findings"]])

    def test_request_is_not_verified_business_success(self):
        case=CASES[-1]
        model=annotated_map(case)
        model["nodes"][0]["outcome"]="success"
        self.assertIn("property_mismatch",[f["code"] for f in evaluate_map(model,case["rubric"])["findings"]])
