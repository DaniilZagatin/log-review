"""Opt-in token accounting: total context is not an output or character limit."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from process_observability.token_budget import TokenBudget
from process_observability import workflow
from process_observability.guidance import load_guidance
from test_workflow import FakeModel, FakeSnapshot


class TokenBudgetTests(unittest.TestCase):
    def budget(self, context=1000000, output=131072, framing=8192):
        return TokenBudget(context, output, framing, 'a' * 64, lambda text: len(text.encode('utf-8')))

    def test_total_includes_output_and_framing_and_unicode_is_not_characters(self):
        budget = self.budget(context=100, output=20, framing=8)
        self.assertTrue(budget.fits('a' * 10, 'Ш' * 31))
        self.assertFalse(budget.fits('a' * 11, 'Ш' * 31))
        self.assertEqual(budget.measure('a' * 10, 'Ш' * 31)['estimated_total_tokens'], 100)
        self.assertEqual(self.budget().identity()['input_token_limit'], 860736)

    def test_repair_reserve_and_invalid_configuration(self):
        budget = self.budget(context=100, output=20, framing=8)
        self.assertTrue(budget.fits('', 'a' * 71))
        self.assertFalse(budget.fits('', 'a' * 71, extra_tokens=2))
        for args in [(True, 10, 5), (100, 100, 5), (100, 10, -1), (0, 10, 5)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.budget(*args)

    def test_loader_refuses_credential_paths_and_symlinks_before_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            secret = root / 'credentials.json'
            secret.write_text('must not be read')
            link = root / 'tokenizer.json'
            link.symlink_to(secret)
            for path in (secret, link, root / 'absent.json'):
                with self.subTest(path=path.name), self.assertRaises(ValueError):
                    TokenBudget.from_file(path, context_tokens=10000, output_tokens=100)

    def test_final_primary_and_repair_guard_before_io_without_clipping(self):
        model = FakeModel({'cards': []})
        run = workflow._Run(FakeSnapshot(), model, {'run_id': 'tokens', 'input_fingerprints': {}},
                            None, False, [], load_guidance(), None)
        system, context = workflow.INSTRUMENTATION_SYSTEM, {'full': 'Ш' * 30}
        full = run.full_system(system)
        user = json.dumps(context, ensure_ascii=False, sort_keys=True)
        size = len(full.encode()) + len(user.encode())
        budget = self.budget(context=size + 101, output=100, framing=1)
        run.review = SimpleNamespace(business_request_chars=999999, business_token_budget=budget)
        self.assertEqual(run.ask(system, context), {'cards': []})
        self.assertEqual(model.calls[0][1], user)
        run.current_attempt = {'attempt': 2, 'feedback': {'errors': []}}
        with self.assertRaises(workflow._PhaseError) as caught:
            run.ask(system, context)
        self.assertEqual(caught.exception.code, 'repair_exceeds_request_budget')
        self.assertEqual(len(model.calls), 1)
        run.current_attempt = None
        with self.assertRaises(workflow._PhaseError):
            run.ask(system, {'full': context['full'] + 'x'})
        self.assertEqual(len(model.calls), 1)

    def test_packing_obeys_token_limit_even_when_char_budget_fits(self):
        run = workflow._Run(FakeSnapshot(), FakeModel(), {'run_id': 'tokens', 'input_fingerprints': {}},
                            None, False, [], load_guidance(), None)
        run.review = SimpleNamespace(business_request_chars=999999,
                                    business_token_budget=self.budget(context=65000, output=100, framing=8))
        rows = [{'text': 'Ш' * 5000} for _ in range(10)]
        result = run.fit(workflow.MAP_SYSTEM, {'business': 'whole text'}, [('rows', rows, 'full.json')])
        count = len(result['rows'])
        self.assertGreater(count, 0)
        self.assertLess(count, len(rows))
        self.assertEqual(result['rows'], rows[:count])
        self.assertEqual(result['context_omissions']['rows']['omitted'], 10-count)


if __name__ == '__main__':
    unittest.main()
