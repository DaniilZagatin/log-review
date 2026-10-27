"""Horizon design system: generated tokens and offline component styles."""

import json
import re
import sys
import unittest
from pathlib import Path

from pko.render.horizon_theme import horizon_styles

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_horizon_tokens import HORIZON, build  # noqa: E402


class HorizonThemeTest(unittest.TestCase):
    def test_committed_tokens_match_tokens_json(self):
        tokens = json.loads((HORIZON / "tokens.json").read_text(encoding="utf-8"))
        committed = (HORIZON / "tokens.css").read_text(encoding="utf-8")
        self.assertEqual(committed, build(tokens), "run scripts/build_horizon_tokens.py")

    def test_styles_are_offline_and_define_used_variables(self):
        styles = horizon_styles()
        self.assertNotIn("@import", styles)
        self.assertNotIn("googleapis", styles)
        defined = set(re.findall(r"--([a-z0-9-]+):", styles))
        used = set(re.findall(r"var\(--([a-z0-9-]+)", styles))
        # cols and p are set inline by the components themselves.
        self.assertEqual(used - defined - {"cols", "p"}, set())


if __name__ == "__main__":
    unittest.main()
