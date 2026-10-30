"""Explicit throughput settings keep the same bounded planner and core checks."""
from test_direct_atomic import AtomicCase, Business, atomic_handlers, bulk_repository, first_attempts
from process_observability.cli import build_parser
from test_cli import invoke
from unittest.mock import patch
from pathlib import Path
import tempfile


class AtomicCapacityTests(AtomicCase):
    def test_larger_explicit_page_and_answer_budget_reduce_requests_without_losing_units(self):
        files, found = bulk_repository(70)
        business = Business(**atomic_handlers())
        bundle, _ = self.run_case(business, files=files, found=found, atomic_page_units=64,
                                 atomic_answer_tokens=40000, atomic_input_tokens=160000,
                                 atomic_input_tokens_hard=800000, refine_max_requests=300)
        pages = first_attempts(business)
        sizes = [len(call['brief']['units']) for call in pages]
        # Two pages instead of seven at the default 10 units; the exact split depends on the measured request size
        # (code-led pages also carry known operations, working groups and relation rows), never above 64 units.
        self.assertEqual((len(sizes), sum(sizes)), (2, 70))
        self.assertTrue(all(size <= 64 for size in sizes) and sizes[0] > 10)
        requests = [item for item in bundle['atomic_map']['requests'] if item['kind'] == 'atomic']
        self.assertTrue(all(item['input_tokens'] <= 160000 for item in requests))
        self.assertEqual(business.tasks().count('TASK: map-compose'), 1)
        self.assertEqual(bundle['atomic_map']['status'], 'complete')
        self.assertEqual(bundle['atomic_map']['summary']['decisions'], 70)
        self.assertEqual(bundle['atomic_map']['settings']['page_units'], 64)

    def test_cli_exposes_all_page_sizing_bounds_without_changing_defaults(self):
        parser = build_parser()
        args = parser.parse_args(['analyze', '--repo', 'own', '--out', 'out'])
        self.assertEqual((args.atomic_page_units, args.atomic_answer_tokens,
                          args.atomic_input_tokens, args.atomic_input_tokens_hard), (10, 10000, 40000, 80000))
        args = parser.parse_args(['analyze', '--repo', 'own', '--out', 'out',
                                  '--atomic-page-units', '64', '--atomic-answer-tokens', '40000',
                                  '--atomic-input-tokens', '160000', '--atomic-input-tokens-hard', '800000'])
        self.assertEqual((args.atomic_page_units, args.atomic_answer_tokens,
                          args.atomic_input_tokens, args.atomic_input_tokens_hard), (64, 40000, 160000, 800000))

    def test_invalid_page_sizing_is_rejected_before_loading_any_model_configuration(self):
        invalid = [('--atomic-answer-tokens', '0'), ('--atomic-answer-tokens', '80001'),
                   ('--atomic-input-tokens', '80001'), ('--atomic-input-tokens-hard', '39999'),
                   ('--atomic-input-tokens-hard', '1000001')]
        with tempfile.TemporaryDirectory() as directory:
            for index, (option, value) in enumerate(invalid):
                with self.subTest(option=option, value=value), patch('process_observability.cli._model_configs',
                        side_effect=AssertionError('must not load model configuration')):
                    code, _, error = invoke('analyze', '--repo', 'own', '--out',
                                            str(Path(directory) / str(index)), option, value)
                    self.assertEqual(code, 2)
                    self.assertIn('atomic', error)
