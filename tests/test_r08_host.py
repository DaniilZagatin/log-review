"""Independent Codex regressions found after the author's full suite; synthetic saved data only."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from process_observability.operation_view import operation_views, card_links
from process_observability.map_evaluation import evaluate_run
from test_operation_evaluation import oracle, saved_map, MAP, EXPECTED


def mixed_map():
    return {
        'process_map': {'stages':[{'id':'s','name':'Stage'}],
            'operations':[{'id':'A','stage_id':'s','event_keys':['shared'],'name':'A'},
                          {'id':'B','stage_id':'s','event_keys':['shared'],'name':'B'}],
            'event_mappings':[{'operation_id':'A','stage_id':'s','event_key':'shared',
                               'evidence_ids':['EA'],'lifecycle_role':'result'}]},
        'atomic_map':{'operations':[{'id':'A'},{'id':'B'}], 'event_mappings':[]},
        'log_coverage':{'facts':[{'id':'FA','stage_id':'s','event_keys':['shared']},
                                 {'id':'FB','stage_id':'s','event_keys':['shared']}]},
        'instrumentation_plan':{'cards':[{'id':'change-B','fact_id':'FB'}]}}


class IndependentFactLinkChecks(unittest.TestCase):
    def test_an_unmatched_legacy_mapping_cannot_be_promoted_to_exact_atomic_facts(self):
        view=operation_views(mixed_map())['A']
        self.assertEqual(view['facts'], [])
        self.assertEqual(view['logs'][0]['linkage'],'event_key')
        self.assertFalse(view['logs'][0]['shows_new_result'])
        self.assertNotIn('A',card_links(mixed_map())['change-B']['operation_ids'])

    def test_unknown_source_support_is_not_a_confirmed_result_of_an_exact_binding(self):
        bundle=mixed_map()
        bundle['atomic_map']['event_mappings']=[{'operation_id':'A','stage_id':'s','event_key':'shared',
            'evidence_ids':['EA'],'fact_ids':['FA'],'site_id':'site-a','path':'a.py','start_line':1,'end_line':1}]
        view=operation_views(bundle)['A']
        self.assertEqual([f['id'] for f in view['facts']],['FA'])
        self.assertFalse(view['logs'][0]['shows_new_result'])

    def test_legacy_event_key_fact_links_stay_explicitly_candidates(self):
        bundle=mixed_map();bundle.pop('atomic_map')
        view=operation_views(bundle)['A']
        self.assertTrue(view['facts'])
        self.assertTrue(all(f['linkage']=='event_key' for f in view['facts']))
        self.assertFalse(view['logs'][0]['shows_new_result'])


class IndependentOraclePathChecks(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.root=Path(folder.name).resolve()
        self.run=self.root/'run';self.run.mkdir()
        self.real=self.root/'real';self.real.mkdir()
        (self.real/'oracle.json').write_text(json.dumps(oracle(EXPECTED)),encoding='utf-8')
        self.link=self.root/'alias';self.link.symlink_to(self.real,target_is_directory=True)

    def test_oracle_symlink_parent_is_rejected_before_reading_the_file(self):
        with patch('process_observability.onboarding_export.load_run',side_effect=AssertionError('no run load')):
            with self.assertRaisesRegex(ValueError,'[Ss]ymlink'):
                evaluate_run(self.run,self.link/'oracle.json',self.root/'result.json')

    def test_output_symlink_parent_is_rejected_without_writing(self):
        with patch('process_observability.onboarding_export.load_run',return_value={'bundle':saved_map(MAP)}):
            with self.assertRaisesRegex(ValueError,'[Ss]ymlink'):
                evaluate_run(self.run,self.real/'oracle.json',self.link/'result.json')
        self.assertFalse((self.real/'result.json').exists())
