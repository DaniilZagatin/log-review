"""Published facts come from the final map, including after publication resume."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from pko.render.scenario_map import render_scenario_map
from pko.scenario_map import runner
from pko.scenario_map.public_summary import apply_public_summary, build_public_summary
from scenario_script import SCENARIO, SPEC, ScriptedClient, research_calls, sources
from test_scenario_render import Document


class PublicSummaryTests(unittest.TestCase):
    def model(self):
        return {"schema_version": "2.0", "semantic_profile": "variant-actions-v1",
                "scenario": {"entry_operation_ids": ["a"], "terminal_operation_ids": ["b", "c"]},
                "run": {"status": "partial"},
                "nodes": [{"id": "a", "name": "Отправить запрос", "outcome": "success",
                           "effect_kind": "command", "external_interactions": [
                               {"name": "Получатель", "contract": "first", "direction": "send"},
                               {"name": "Получатель", "contract": "second", "direction": "send"}]},
                          {"id": "b", "name": "Завершить обработку", "outcome": "unknown"},
                          {"id": "c", "name": "Отказать", "outcome": "refusal", "effect_kind": "command",
                           "completion_condition": "Заявка отклонена"}],
                "edges": [], "gaps": [],
                "summary": "На карте 999 действий, 36 переходов; b завершён успешно, кредит погашен."}

    def test_counts_and_terminal_outcomes_use_only_final_structured_fields(self):
        model = self.model()
        projection = build_public_summary(model)
        facts = projection["factual_summary"]
        self.assertEqual(facts["counts"], {"nodes": 3, "edges": 0, "gaps": 0, "terminal_operations": 2,
                                         "external_operations": 1, "external_interactions": 2})
        self.assertEqual(facts["terminal_operation_ids"], ["b", "c"])
        self.assertEqual(facts["terminal_outcomes"], {"unknown": 1, "refusal": 1})
        self.assertEqual([row["id"] for row in facts["operations"]], ["a", "b", "c"])
        self.assertEqual([row["contract"] for row in facts["external_interactions"]], ["first", "second"])
        self.assertEqual(facts["terminal_operations"][1]["effect_kind"], "command")
        self.assertEqual(facts["terminal_operations"][1]["completion_condition"], "Заявка отклонена")
        self.assertNotIn("999", projection["summary"])
        self.assertNotIn("кредит погашен", projection["summary"])
        self.assertNotIn("успех —", projection["summary"])
        self.assertEqual(projection["model_commentary"]["text"], model["summary"])
        self.assertFalse(facts["semantic_completeness_verified"])

    def test_unresolved_terminal_reference_is_reported_without_inventing_an_operation(self):
        model = self.model()
        model["scenario"]["terminal_operation_ids"].append("missing")
        facts = build_public_summary(model)["factual_summary"]
        self.assertEqual(facts["unresolved_terminal_ids"], ["missing"])
        self.assertEqual(facts["counts"]["terminal_operations"], 2)
        self.assertNotIn("missing", [row["id"] for row in facts["terminal_operations"]])

    def test_shared_finalizer_is_distinguished_from_unknown_business_outcome(self):
        model = self.model()
        model["nodes"][1]["outcome_scope"] = "shared"
        projection = build_public_summary(model)
        self.assertEqual(projection["factual_summary"]["terminal_outcomes"], {"shared": 1, "refusal": 1})
        self.assertEqual(projection["factual_summary"]["terminal_operations"][0]["outcome"], "unknown")
        self.assertEqual(projection["factual_summary"]["terminal_operations"][0]["outcome_scope"], "shared")
        self.assertIn("общее завершение разных ветвей — 1", projection["summary"])
        self.assertNotIn("не установлен —", projection["summary"])

    def test_republication_recomputes_stale_facts_and_keeps_original_comment(self):
        model = self.model()
        original = model["summary"]
        apply_public_summary(model)
        snapshot = deepcopy(model)
        apply_public_summary(model)
        self.assertEqual(model, snapshot)
        model["nodes"][1]["outcome"] = "deferred"
        model["edges"].append({"id": "e", "source": "a", "target": "b"})
        apply_public_summary(model)
        self.assertEqual(model["factual_summary"]["counts"]["edges"], 1)
        self.assertEqual(model["factual_summary"]["terminal_outcomes"], {"deferred": 1, "refusal": 1})
        self.assertEqual(model["model_commentary"]["text"], original)

    def test_renderer_recomputes_facts_without_mutating_saved_legacy_map(self):
        model = self.model()
        model.pop("semantic_profile")
        model["summary"] += '</script><img src=x onerror="alert(1)">'
        original = deepcopy(model)
        document = Document(render_scenario_map(model))
        self.assertEqual(model, original)
        self.assertEqual(json.loads(document.scripts[0]["text"]), original)
        script = document.scripts[-1]["text"]
        projection = json.loads(re.search(r"const publicSummary = (.*);\n", script).group(1))
        self.assertNotIn("999", projection["summary"])
        self.assertEqual(projection["model_commentary"]["text"], original["summary"])
        self.assertNotIn("model.summary ||", script)
        self.assertIn("Комментарий исследователя", script)
        self.assertFalse(any(tag == "img" for tag, _ in document.elements))

    def test_publication_resume_rederives_summary_without_research_or_checkpoint_mutation(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"PKO_PHOENIX_ENABLED": "false"}):
            out = Path(temporary) / "out"
            calls = research_calls()
            lying = "На карте 999 действий, все конечные действия успешны."
            calls = [(name, {"summary": lying} if name == "finish" else args) for name, args in calls]
            first = runner.run_scenario(SCENARIO, sources(), out, spec=SPEC,
                                        client=ScriptedClient(calls), stages=False)
            self.assertEqual(first["run"]["stop_kind"], "finish", first["run"])
            checkpoint_path = out / "scenario_map.checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["summary"], lying)
            self.assertNotIn("factual_summary", checkpoint)
            self.assertNotIn("999", first["summary"])
            self.assertEqual(first["model_commentary"]["text"], lying)
            client = ScriptedClient(calls=[])
            resumed = runner.resume_scenario(checkpoint_path, sources(), spec=SPEC, client=client, stages=False)
            self.assertEqual(client.seen, [])
            self.assertEqual(resumed["nodes"], first["nodes"])
            self.assertEqual(resumed["factual_summary"], first["factual_summary"])
            published = json.loads((out / "scenario_map.json").read_text(encoding="utf-8"))
            self.assertEqual(published["summary"], resumed["summary"])
            self.assertEqual(published["model_commentary"]["text"], lying)
            embedded = json.loads(Document((out / "scenario_map.html").read_text(encoding="utf-8")).scripts[0]["text"])
            self.assertEqual(embedded["factual_summary"], published["factual_summary"])


if __name__ == "__main__":
    unittest.main()
