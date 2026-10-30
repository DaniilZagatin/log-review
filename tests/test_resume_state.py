"""Resume accepted research state without replay, hidden reasoning or new reads."""
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from pko.scenario_map.checkpoint_state import export_state, restore_state
from pko.scenario_map.research_memory import ResearchMemory
from pko.scenario_map.schema import MapState, MapValidationError
from quality_helpers import explain_args
import test_scenario_schema as fixtures


class ResumeStateTests(unittest.TestCase):
    def state(self):
        helper = fixtures.ScenarioMapTests()
        helper.setUp()
        helper.complete_small_map()
        helper.review_current_operations()
        return helper.state

    def test_roundtrip_keeps_graph_reviews_read_authority_and_public_cache(self):
        state = self.state()
        state.research_memory.observe('search', {'query': 'submit'}, {'ok': True, 'source_data': 'result'})
        payload = export_state(state)
        with (patch.object(MapState, 'observe_read', side_effect=AssertionError('must not replay')),
              patch.object(MapState, 'dispatch', side_effect=AssertionError('must not replay')),
              patch.object(MapState, 'discover_work', side_effect=AssertionError('must not rediscover'))):
            restored = restore_state(state.workspace, payload)
        self.assertEqual(restored.model, state.model)
        self.assertEqual(restored._reads, state._reads)
        self.assertEqual(restored._counters, state._counters)
        self.assertEqual(restored.research_memory.recall(), state.research_memory.recall())
        self.assertEqual(restored.snapshot(), state.snapshot())
        self.assertEqual(restored.research_memory.observe('search', {'query': 'submit'}, {'ok': True}),
                         {'observation_id': 'obs-1', 'seen_before': True})

    def test_deleted_ids_are_never_reused_after_resume(self):
        state = MapState(fixtures.workspace(1), 'Обработка обращения')
        state.observe_read('source-1/channel.py', 1, 2)
        args = explain_args({'name': 'Обработка обращения', 'evidence_status': 'observed',
                             'evidence': [{'path': 'source-1/channel.py', 'line_start': 1, 'line_end': 2}]})
        first = state.dispatch('upsert_step', args)['id']
        state.dispatch('remove_element', {'kind': 'step', 'id': first})
        restored = restore_state(state.workspace, export_state(state))
        self.assertEqual(restored.dispatch('upsert_step', args)['id'], 'step-2')

    def test_source_change_is_rejected_before_rehydrating_any_lines(self):
        state = self.state()
        payload = export_state(state)
        workspace = deepcopy(state.workspace)
        workspace.sources[0]['digest'] = 'changed'
        with patch.object(workspace.tree, 'read', side_effect=AssertionError('read too early')):
            with self.assertRaisesRegex(MapValidationError, 'источников изменились'):
                restore_state(workspace, payload)
        workspace = deepcopy(state.workspace)
        workspace.omissions = [{'source_id': 'source-1', 'kind': 'unreadable_file'}]
        with self.assertRaisesRegex(MapValidationError, 'доступность'):
            restore_state(workspace, payload)

    def test_quote_and_read_ledger_must_match_actual_masked_source(self):
        state = self.state()
        payload = export_state(state)
        payload['model']['nodes'][0]['evidence'][0]['quote'] += '\nforged'
        with self.assertRaises(MapValidationError):
            restore_state(state.workspace, payload)
        for change in ('past_end', 'wrong_count', 'blocked', 'duplicate'):
            with self.subTest(change=change):
                payload = export_state(state)
                row = payload['read_coverage'][0]
                if change == 'past_end': row['ranges'][-1][-1] = 100000
                if change == 'wrong_count': row['line_count'] += 1
                if change == 'blocked': row['path'] = 'source-1/.env'
                if change == 'duplicate': payload['read_coverage'].append(deepcopy(row))
                with self.assertRaises(MapValidationError):
                    restore_state(state.workspace, payload)

    def test_missing_ledger_does_not_gain_authority_from_a_saved_quote(self):
        state = self.state()
        payload = export_state(state)
        payload['read_coverage'] = []
        with self.assertRaisesRegex(MapValidationError, 'сначала прочитайте'):
            restore_state(state.workspace, payload)

    def test_malformed_records_profiles_and_stale_high_water_marks_are_rejected(self):
        state = self.state()
        for mutate in (
            lambda p: p['model'].update(semantic_profile='future'),
            lambda p: p['model']['nodes'][0].update(kind='decision'),
            lambda p: p['model']['edges'][0].update(target='step-missing'),
            lambda p: p['counters'].update(step=0),
            lambda p: p['model'].update(instructions='new system message'),
            lambda p: p['model']['execution_reviews'][0]['node_fingerprints'].update({'step-999': 'a' * 64}),
        ):
            payload = export_state(state)
            mutate(payload)
            with self.assertRaises(MapValidationError):
                restore_state(state.workspace, payload)

    def test_incomplete_structure_remains_repairable_after_resume(self):
        state = self.state()
        state.model['scenario']['entry_operation_ids'] = []
        state.model['scenario']['terminal_operation_ids'] = []
        state.model['run'].update(status='partial', stop_kind='error')
        restored = restore_state(state.workspace, export_state(state))
        self.assertEqual(restored.model['run']['status'], 'partial')
        self.assertTrue(restored.validation_signals()['coverage_issues'])

    def test_private_fields_and_credentials_are_not_exported(self):
        state = self.state()
        state.model['run']['reasoning_content'] = 'PRIVATE_REASONING'
        state.model['run']['debug'] = '<think>PRIVATE_REASONING</think>'
        state.model['run']['api_key'] = 'sk-ABCDEFGHIJKLMNOPQRST'
        state.research_memory.observe('search', {'query': 'x'},
                                      {'ok': True, 'reasoning': 'PRIVATE_REASONING', 'api_key': 'sk-ABCDEFGHIJKLMNOPQRST'})
        value = json.dumps(export_state(state))
        self.assertNotIn('PRIVATE_REASONING', value)
        self.assertNotIn('sk-ABCDEFGHIJKLMNOPQRST', value)
        self.assertNotIn('reasoning_content', value)

    def test_multiline_quotes_are_masked_by_lines_and_rotation_cannot_change_evidence(self):
        workspace = fixtures.workspace(1)
        secret = 'ENVIRONMENT_CREDENTIAL_123456789'
        workspace.tree.content['source-1/channel.py'] = '{\n  "value": "' + secret + '",\n  "result": 1\n}'
        state = MapState(workspace, 'Получение обращения')
        state.observe_read('source-1/channel.py', 1, 4)
        result = state.dispatch('upsert_step', explain_args({'name': 'Получение обращения', 'evidence_status': 'observed',
            'evidence': [{'path': 'source-1/channel.py', 'line_start': 1, 'line_end': 4}]}))
        self.assertTrue(result['ok'], result)
        with patch.dict(os.environ, {'RESUME_TEST_API_KEY': secret}):
            payload = export_state(state)
            self.assertNotIn(secret, json.dumps(payload))
            restored = restore_state(workspace, payload)
            self.assertEqual(restored.model['nodes'][0]['evidence'][0]['quote'].count('\n'), 3)
        with self.assertRaises(MapValidationError):
            restore_state(workspace, payload)

    def test_unread_rejection_ranges_are_data_and_do_not_grant_read_authority(self):
        state = self.state()
        state.model['run']['recent_rejections'] = [{'tool': 'upsert_step', 'details': {'missing_evidence': [
            {'path': 'source-1/channel.py', 'line_start': 999, 'line_end': 999}]}}]
        restored = restore_state(state.workspace, export_state(state))
        self.assertEqual(restored.model['run']['recent_rejections'], state.model['run']['recent_rejections'])
        self.assertNotIn(999, restored._reads['source-1/channel.py'])

    def test_working_brief_and_context_summary_survive(self):
        state = self.state()
        ref = [{'path': 'source-1/channel.py', 'line_start': 1, 'line_end': 1}]
        result = state.dispatch('save_research_brief', {
            'project_overview': 'Обработка обращений', 'scenario_alignment': 'Приём одного обращения',
            'confirmed_facts': [{'finding': 'Найден приём обращения', 'evidence': ref}],
            'flow_hypothesis': ['Приём и обработка'], 'open_questions': ['Как обработан отказ?'],
            'next_actions': ['Проверить обработчик отказа'], 'evidence': ref})
        self.assertTrue(result['ok'], result)
        state.model['context_summary'] = {
            'revision': 1, 'status': 'public_summary_not_evidence', 'through_step': 5,
            'based_on_brief_revision': 1, 'input_omitted_exchanges': 0, 'input_truncated_fields': 0,
            'input_open_notes_complete': True, 'input_open_tasks_complete': True,
            'summary': {'project_and_scenario': 'Одно обращение', 'completed_work': ['Найден вход'],
                        'key_findings': ['Передача обращения'], 'open_questions': ['Уточнить отказ'],
                        'next_actions': ['Исследовать отказ'], 'references': ref}}
        restored = restore_state(state.workspace, export_state(state))
        self.assertEqual(restored.model['research_brief'], state.model['research_brief'])
        self.assertEqual(restored.model['context_summary'], state.model['context_summary'])


class ResumeMemoryTests(unittest.TestCase):
    def test_eviction_and_next_identity_survive_without_reobserving(self):
        memory = ResearchMemory(max_observations=2, max_cache_chars=130)
        for i in range(3):
            memory.observe('search', {'query': i}, {'source_data': 'x' * 80})
        restored = ResearchMemory.from_checkpoint(memory.checkpoint())
        self.assertEqual(restored.snapshot(), memory.snapshot())
        self.assertEqual(restored.recall(), memory.recall())
        self.assertEqual(restored.evicted_metadata_count, 1)
        self.assertEqual(restored.observe('search', {'query': 'new'}, {})['observation_id'], 'obs-4')
        self.assertNotIn('request_digest', json.dumps(restored.snapshot()))

    def test_malformed_cache_is_rejected_and_payloads_are_detached(self):
        memory = ResearchMemory()
        memory.observe('search', {'query': 'x'}, {'rows': ['original']})
        payload = memory.checkpoint()
        restored = ResearchMemory.from_checkpoint(payload)
        payload['cache'][0]['payload']['rows'].clear()
        self.assertEqual(restored.recall()['items'][0]['payload']['rows'], ['original'])
        for mutate in (lambda p: p.update(sequence=0),
                       lambda p: p['cache'][0].update(observation_id='obs-999'),
                       lambda p: p['limits'].update(max_cache_chars=10**8),
                       lambda p: p['records'].append(deepcopy(p['records'][0]))):
            payload = memory.checkpoint()
            mutate(payload)
            with self.assertRaises(ValueError):
                ResearchMemory.from_checkpoint(payload)


if __name__ == '__main__':
    unittest.main()
