"""Real Belka tools and publication -> current consumer -> metrics -> portable HTML.

All model responses are scripted; this is contract integration, not model quality.
"""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# Upstream scripted researcher is kept with the imported producer regression fixtures.
FIXTURE_TESTS = Path(__file__).resolve().parents[1]/'tests_belka'
sys.path.insert(0, str(FIXTURE_TESTS))
from scenario_script import SCENARIO, SPEC, ScriptedClient, sources
from pko.scenario_map.runner import run_scenario
from process_observability.pipeline import run_product, export_product
from test_map_first_logging import Scripted, model_reply


class ProductBelkaE2E(unittest.TestCase):
    def test_real_producer_tools_keep_map_and_hierarchy_across_all_stages(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); profile = root/'profile.json'
            profile.write_text(json.dumps({'base_url': 'http://fixture.invalid/v1', 'model': 'fixture'}), encoding='utf-8')
            args = SimpleNamespace(source=[str(p) for p in sources()], map=None, out=str(root/'out'),
                inventory_only=False, resume_from=None, scenario_text=SCENARIO, process_file=None,
                config=str(profile), max_requests=100, max_steps=500, max_seconds=120,
                model_timeout=10, request_chars=180000, max_navigation_turns=4,
                max_context_lines=1600, log_call=[])
            class ProductModel:
                def complete_json(self, system, user):
                    data = json.loads(user)
                    if 'candidates' in data:
                        return {'metrics': data['candidates']}
                    return model_reply(data['packet'], key=None)
            seen = []
            def producer(description, inputs, output, **kwargs):
                seen.append(kwargs['prepared_workspace'])
                kwargs.update(spec=SPEC, client=ScriptedClient())
                return run_scenario(description, inputs, output, **kwargs)
            with patch.dict('os.environ', {'PKO_PHOENIX_ENABLED': 'false'}):
                result = run_product(args, belka_runner=producer,
                                     logging_model=ProductModel())
            out = root/'out'
            manifest = json.loads((out/'product_run.json').read_text(encoding='utf-8'))
            self.assertEqual(len(seen), 1)
            self.assertIn(result['status'], ('completed', 'partial'), manifest)
            self.assertEqual(manifest['stages']['metrics']['status'], 'completed', manifest)
            published = json.loads((out/'scenario_map.json').read_text(encoding='utf-8'))
            producer_map = json.loads((out/'belka/scenario_map.json').read_text(encoding='utf-8'))
            review = json.loads((out/'logging_review.json').read_text(encoding='utf-8'))
            hierarchy = json.loads((out/'map_hierarchy.json').read_text(encoding='utf-8'))
            self.assertEqual(published, producer_map)
            self.assertEqual(review['map'], producer_map)
            self.assertEqual(len(published['nodes']), 18)
            self.assertEqual(hierarchy['hierarchy_version'], '1.2')
            self.assertEqual(hierarchy['semantic_profile'], 'variant-actions-v1')
            self.assertFalse(review['presentation_warnings'])
            self.assertEqual(set(review['source_fingerprints']), set(seen[0].tree.files))
            self.assertEqual(set(row['operation_id'] for row in review['operations']),
                             set(row['id'] for row in published['nodes']))
            document = (out/'report.html').read_text(encoding='utf-8')
            for label in ('Карта', 'Логирование', 'Доработки', 'Метрики'):
                self.assertIn(label, document)
            with patch('pko.scenario_map.runner.run_scenario', side_effect=AssertionError('Export has no research')):
                exported = export_product(out, root/'export')
            self.assertTrue(Path(exported['report']).is_file())


if __name__ == '__main__': unittest.main()
