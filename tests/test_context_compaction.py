"""Detailed operation reviews must fit after serializing their summary as text."""
import json
import unittest
from unittest.mock import Mock, patch

from pko.scenario_map.agent import _brief, _compact, _json


class ContextCompactionTests(unittest.TestCase):
    def test_serialized_summary_escaping_is_included_in_budget(self):
        state = Mock()
        state.snapshot.return_value = {"scenario": {"title": "Сценарий"}, "run": {"status": "running"}}
        messages = [{"role": "system", "content": "rules"},
                    {"role": "user", "content": "scenario"},
                    {"role": "assistant", "content": "x" * 10000}]
        with patch("pko.scenario_map.agent._brief", return_value='"\\\n' * 400):
            result = _compact(messages, state, 2000)
        self.assertLessEqual(sum(len(_json(m)) for m in result), 2000)
        self.assertEqual(result[:2], messages[:2])
        self.assertIn("inspect_map", result[-1]["content"])

    def test_nested_operation_quotes_stay_in_state_but_not_compact_summary(self):
        citation = {"path": "source-1/app.py", "line_start": 1, "quote": "large literal source"}
        original = {"execution_reviews": [{"operations": [{"evidence": [citation]}],
                    "checks": {"branches": {"evidence": [citation]}},
                    "node_fingerprints": {"step-1": "digest"}}]}
        state = Mock()
        state.snapshot.return_value = original
        compact = json.loads(_brief(state))
        ref = compact["execution_reviews"][0]["operations"][0]["evidence"][0]
        self.assertEqual(ref, {"path": "source-1/app.py", "line_start": 1})
        self.assertNotIn("large literal source", _brief(state))
        self.assertNotIn("node_fingerprints", compact["execution_reviews"][0])
        self.assertEqual(citation["quote"], "large literal source")
