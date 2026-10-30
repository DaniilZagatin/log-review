from copy import deepcopy
import re
import tempfile
from pathlib import Path
import unittest

from process_observability.product_report import build_product_html, build_pending_html, write_product_report
from process_observability.metrics import build_metrics
from test_product_metrics import fixture


class ProductReportTests(unittest.TestCase):
    def test_four_accessible_tabs_and_source_inertness(self):
        data = fixture(); metrics = build_metrics(data)
        metrics['metrics'][0]['purpose'] = '</h3><img src=x onerror=alert(1)>'
        page = build_product_html(data, metrics, {'status': 'partial', 'reason': 'Проверка логов не завершена',
                                                'stages': {'map': {'status': 'completed'}, 'logging': {'status': 'partial'}}})
        self.assertEqual(len(re.findall('role="tab"', page)), 4)
        self.assertIn('id="panel-metrics"', page)
        self.assertIn('data-report-operation="save"', page)
        self.assertIn('Проверка логов не завершена', page)
        self.assertNotIn('<img src=x onerror=alert(1)>', page)
        self.assertIn('Content-Security-Policy', page)
        self.assertIn('Фактическое значение', page)

    def test_pending_preserves_tabs_and_stop_reason(self):
        page = build_pending_html(manifest={'status': 'failed', 'reason': 'Источники недоступны'})
        self.assertEqual(len(re.findall('role="tab"', page)), 4)
        self.assertIn('Источники недоступны', page)
        self.assertIn('Результат этапа ещё не опубликован', page)

    def test_export_is_portable_and_does_not_mutate_map(self):
        data = fixture(); before = deepcopy(data); metrics = build_metrics(data)
        with tempfile.TemporaryDirectory() as root:
            result = write_product_report(data, metrics, Path(root)/'export')
            self.assertTrue(Path(result['report']).exists())
            self.assertTrue(Path(root, 'export', 'SHA256SUMS').exists())
            self.assertEqual(data, before)
            with self.assertRaises(ValueError): write_product_report(data, metrics, Path(root)/'export')

    def test_shared_outcome_and_action_terminology_rendered(self):
        from process_observability.map_presentation import scenario_map_parts
        model = {'schema_version': '2.0', 'semantic_profile': 'variant-actions-v1', 'scenario': {},
                 'nodes': [], 'edges': [], 'run': {'status': 'partial', 'reason': 'Лимит'}, 'validation': {}}
        parts = scenario_map_parts(model, embedded=True)
        self.assertIn("node.outcome_scope==='shared'", parts['script'])
        self.assertIn('Бизнес-операции', parts['body'])
        self.assertIn('id="result-lifecycle"', parts['body'])
        self.assertIn('Лимит', parts['body'])