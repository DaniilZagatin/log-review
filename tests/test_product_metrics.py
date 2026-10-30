from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from process_observability.metrics import build_metrics, validate_metrics
from process_observability.logging_review import review_logging
from process_observability.map_input import MapView
from process_observability.source_tools import snapshot_repository


def fixture():
    with tempfile.TemporaryDirectory() as root:
        Path(root, 'app.py').write_text('def save():\n    return True\n', encoding='utf-8')
        model = {'schema_version': '2.0', 'scenario': {'title': 'Заявка', 'instance_unit': 'Одна заявка',
                 'entry_operation_ids': ['save'], 'terminal_operation_ids': ['save']},
                 'sources': [{'id': 'source-1'}], 'nodes': [{'id': 'save', 'kind': 'operation', 'name': 'Сохранить заявку',
                 'description': 'Сохранение заявки', 'input': 'Заявка', 'output': 'Сохранённая заявка',
                 'outcome': 'success', 'effect_kind': 'result', 'evidence': [{'path': 'source-1/app.py',
                 'line_start': 1, 'line_end': 2, 'quote': 'def save():\n    return True'}]}],
                 'edges': [], 'run': {'status': 'completed', 'id': 'test'}}
        return review_logging(MapView.from_dict(model), snapshot_repository(root), None)


class MetricTests(unittest.TestCase):
    def test_templates_reference_only_map_owned_facts_and_never_compute(self):
        data = fixture(); original = deepcopy(data)
        catalog = build_metrics(data)
        self.assertEqual(data, original)
        self.assertGreaterEqual(len(catalog['metrics']), 3)
        self.assertTrue(all(m['value'] is None and m['readiness']['primary'] == 'design_only' for m in catalog['metrics']))
        self.assertTrue(all(f['operation_id'] == 'save' and not f['runtime_verified'] for f in catalog['facts']))
        validate_metrics(catalog, data)

    def test_changed_review_cannot_reuse_catalog(self):
        data = fixture(); catalog = build_metrics(data)
        data['status'] = 'completed'
        with self.assertRaises(ValueError): validate_metrics(catalog, data)

    def test_forged_references_and_numeric_values_rejected(self):
        data = fixture(); catalog = build_metrics(data)
        for key, value in [('value', 2), ('operation_ids', ['unknown']), ('dependencies', ['unknown'])]:
            edited = deepcopy(catalog); edited['metrics'][0][key] = value
            with self.assertRaises(ValueError): validate_metrics(edited, data)

    def test_shared_finalizer_has_no_success_rate(self):
        data = fixture(); data['map']['nodes'][0]['outcome_scope'] = 'shared'
        from process_observability.logging_contracts import digest
        data['map_content_sha256'] = MapView.from_dict(data['map']).sha256
        data['map_revision'] = MapView.from_dict(data['map']).revision
        catalog = build_metrics(data)
        self.assertFalse(any(m['id'] == 'outcome-success' for m in catalog['metrics']))

    def test_model_is_bounded_and_cannot_invent_fact_links(self):
        class Bad:
            count = 0
            def complete_json(self, system, user):
                self.count += 1
                return {'metrics': [{'id': 'fake'}]}
        model = Bad(); data = fixture(); result = build_metrics(data, model)
        self.assertEqual(model.count, 2)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['method'], 'template_proposal')
        validate_metrics(result, data)

    def test_transport_failure_is_not_schema_repair(self):
        class Broken:
            count = 0
            def complete_json(self, system, user):
                self.count += 1
                raise RuntimeError('offline')
        model = Broken(); result = build_metrics(fixture(), model)
        self.assertEqual(model.count, 1)
        self.assertEqual(result['status'], 'partial')

    def test_successful_proposal_retains_host_readiness(self):
        import json
        class Model:
            def complete_json(self, system, user):
                return {'metrics': json.loads(user)['candidates']}
        result = build_metrics(fixture(), Model())
        self.assertEqual(result['model_requests'], 1)
        self.assertEqual(result['method'], 'model_proposal')
        self.assertTrue(all(m['readiness']['blockers'] for m in result['metrics']))
    def test_catalog_cannot_drop_definition_or_claim_false_completion(self):
        data = fixture(); original = build_metrics(data)
        for edit in ('drop', 'status'):
            catalog = deepcopy(original)
            if edit == 'drop': catalog['metrics'] = []
            else: catalog['status'] = 'partial'
            with self.assertRaises(ValueError): validate_metrics(catalog, data)

    def test_llm_value_error_does_not_trigger_schema_repair(self):
        from process_observability.llm import LLMError
        class Provider:
            count = 0
            def complete_json(self, system, user):
                self.count += 1
                raise LLMError('budget exhausted')
        model = Provider(); result = build_metrics(fixture(), model)
        self.assertEqual(model.count, 1)
        self.assertEqual(result['status'], 'partial')

    def test_cycle_has_repeat_definition_without_counting_technical_retries(self):
        data = fixture(); data['map']['edges'] = [{'id':'retry','source':'save','target':'save','condition':'Повторить обработку'}]
        data['map_content_sha256'] = MapView.from_dict(data['map']).sha256
        data['map_revision'] = MapView.from_dict(data['map']).revision
        catalog = build_metrics(data)
        repeated = [m for m in catalog['metrics'] if m['id'].startswith('action-repeat-')]
        self.assertEqual(len(repeated), 1)
        self.assertIn('технические попытки', repeated[0]['repeat_policy'])
        self.assertIsNone(repeated[0]['value'])

    def test_storage_failure_escapes_without_repair(self):
        class Storage:
            count = 0
            def complete_json(self, system, user):
                self.count += 1
                raise OSError('checkpoint failed')
        model = Storage()
        with self.assertRaises(OSError): build_metrics(fixture(), model)
        self.assertEqual(model.count, 1)

    def test_private_fields_or_edited_host_blockers_are_rejected(self):
        data = fixture(); original = build_metrics(data)
        for change in ('top_reasoning', 'metric_reasoning', 'prose_reasoning', 'blockers', 'unit'):
            catalog = deepcopy(original)
            if change == 'top_reasoning': catalog['reasoning_content'] = 'private'
            elif change == 'metric_reasoning': catalog['metrics'][0]['thinking'] = 'private'
            elif change == 'prose_reasoning': catalog['metrics'][0]['purpose'] = '<think>private</think>'
            elif change == 'blockers': catalog['metrics'][0]['readiness']['blockers'] = ['Just a warning']
            else: catalog['metrics'][0]['counting_unit'] = 'Другая единица'
            with self.subTest(change=change), self.assertRaises(ValueError): validate_metrics(catalog, data)

    def test_private_reasoning_reply_is_not_saved_or_repeated_to_model(self):
        import json
        class Model:
            calls = []
            def complete_json(self, system, user):
                self.calls.append(user)
                rows = json.loads(user)['candidates']
                rows[0]['purpose'] = '<think>PRIVATE_SECRET_MARKER</think>'
                return {'metrics': rows}
        model = Model(); result = build_metrics(fixture(), model)
        self.assertEqual(len(model.calls), 2)
        self.assertNotIn('PRIVATE_SECRET_MARKER', json.dumps(result))
        self.assertNotIn('PRIVATE_SECRET_MARKER', model.calls[1])
        self.assertEqual(result['status'], 'partial')

    def test_model_cannot_invent_an_unknown_counting_unit(self):
        import json
        data = fixture(); data['map']['scenario']['instance_unit'] = None
        data['map_content_sha256'] = MapView.from_dict(data['map']).sha256
        data['map_revision'] = MapView.from_dict(data['map']).revision
        class Model:
            def complete_json(self, system, user):
                rows = json.loads(user)['candidates']; rows[0]['counting_unit'] = 'Платёж'
                return {'metrics': rows}
        result = build_metrics(data, Model())
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['metrics'][0]['counting_unit'])
