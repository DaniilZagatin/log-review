"""Own synthetic regression for a real failure class; no actual reply/source used."""
import copy
import hashlib
import json
import unittest

from test_workflow import (APP, REVIEW_FINDINGS, ReviewModeTest, ScriptedCode,
                           instrumentation_response, map_response, metrics_response)


class MetricsAuxiliaryExtra(ReviewModeTest):
    def replies(self, *, invalid_dependency=False):
        primary = metrics_response()
        primary['metrics'][0]['guidance_ids'] = ['synthetic-invalid-rule']
        repair = metrics_response()
        repair['unknowns'] = ['Synthetic unresolved clock source; no observed numbers are available.']
        if invalid_dependency:
            repair['metrics'][0]['dependencies'] = ['synthetic-unknown-core-fact']
        return primary, repair

    def test_valid_metric_with_auxiliary_unknowns_after_real_repair_is_retained(self):
        primary, repair = self.replies()
        original = copy.deepcopy(repair)
        bundle, model, _, _ = self.review(
            {'app.py': APP}, ScriptedCode(REVIEW_FINDINGS[:2]),
            map_response(), instrumentation_response(), primary, repair)
        self.assertEqual(bundle['validation']['stages']['metrics']['outcome'],
                         'accepted_with_warnings_after_repair')
        record = bundle['validation']['stages']['metrics']
        self.assertEqual(len(record['attempts']), 2)
        self.assertEqual(record['attempts'][0]['code'], 'unknown_guidance_id')
        self.assertTrue(any(w['path'] in ('unknowns', '$.unknowns') and 'omit' in w['action']
                            for w in record['warnings']))
        calls = [pair for pair in model.calls if pair[0].startswith('TASK: metrics\n')]
        self.assertEqual(len(calls), 2, 'No third API call for an auxiliary field')
        accepted = record['accepted_response']
        self.assertEqual(accepted['attempt'], 2)
        self.assertEqual(accepted['api_request_sha256'], self.request_sha(calls[1]))
        expected_hash = hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True,
                                                   separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        self.assertEqual(accepted['raw_response_json_sha256'], expected_hash)
        self.assertEqual(repair, original, 'Caller-owned raw repair must stay unchanged')
        self.assertEqual(bundle['manifest']['stages']['metrics']['status'], 'completed')
        self.assertEqual({m['id'] for m in bundle['metric_catalog']['metrics']},
                         {m['id'] for m in repair['metrics']})
        parity = copy.deepcopy(repair)
        del parity['unknowns']
        clean_bundle, _, _, _ = self.review(
            {'app.py': APP}, ScriptedCode(REVIEW_FINDINGS[:2]),
            map_response(), instrumentation_response(), primary, parity)
        self.assertEqual(bundle['metric_catalog']['metrics'], clean_bundle['metric_catalog']['metrics'],
                         'Auxiliary omission must retain every accepted core definition field')

    def test_auxiliary_extra_does_not_hide_invalid_core_dependency(self):
        primary, repair = self.replies(invalid_dependency=True)
        bundle, model, _, _ = self.review(
            {'app.py': APP}, ScriptedCode(REVIEW_FINDINGS[:2]),
            map_response(), instrumentation_response(), primary, repair)
        self.assertEqual(bundle['manifest']['stages']['metrics']['status'], 'incomplete')
        self.assertNotIn(bundle['validation']['stages']['metrics']['outcome'],
                         ['accepted', 'accepted_with_warnings_after_repair'])
        self.assertEqual(sum(system.startswith('TASK: metrics\n') for system, _ in model.calls), 2)
        record = bundle['validation']['stages']['metrics']
        self.assertEqual(len(record['attempts']), 2)
        self.assertEqual(record['attempts'][0]['code'], 'unknown_guidance_id')
        self.assertIn('revalidation', record, 'Auxiliary omission must reach core validation')
        self.assertEqual(record['revalidation']['outcome'], 'rejected')
        self.assertEqual(record['revalidation']['code'], 'unknown_fact_id')
        self.assertIn('metrics[0].dependencies', record['revalidation']['path'])
        self.assertNotIn('synthetic-unknown-core-fact', json.dumps(bundle['metric_catalog']))
        parity = copy.deepcopy(repair)
        del parity['unknowns']
        clean_bundle, clean_model, _, _ = self.review(
            {'app.py': APP}, ScriptedCode(REVIEW_FINDINGS[:2]),
            map_response(), instrumentation_response(), primary, parity)
        last = clean_bundle['validation']['stages']['metrics']['attempts'][-1]
        self.assertEqual(record['revalidation']['code'], last['code'])
        self.assertEqual(record['revalidation']['path'], last['path'])
        self.assertEqual(sum(s.startswith('TASK: metrics\n') for s, _ in clean_model.calls), 2)


if __name__ == '__main__':
    unittest.main()
