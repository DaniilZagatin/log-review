"""Lifecycle, safe output recovery, and truthful presentation without providers."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from pko.errors import LlmError
from pko.llm.client import ChatResult
from pko.render.scenario_map import render_scenario_map
from pko.scenario_map.agent import _rejection_key, run_agent
from pko.scenario_map.model_retry import call_model, ModelDeadlineExceeded
from pko.scenario_map.runner import _can_group, _publish_stages, run_scenario
from pko.scenario_map.sources import build_workspace
from scenario_script import SCENARIO, SPEC, ScriptedClient, sources


def call(name, arguments, identifier='test'):
    return {'id': identifier, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


class ModelRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.messages = [{'role': 'user', 'content': 'Selected variant'}]
        self.options = dict(deadline=time.monotonic() + 100, max_tokens=1024,
                            recovery_max_tokens=4096, context_window=8192)

    def test_length_discards_entire_batch_and_preserves_conversation(self):
        partial = ChatResult('', {'prompt_tokens': 2048}, tool_calls=[
            call('upsert_step', '{"name":"discarded"}', 'one'),
            call('upsert_step', '{"name":', 'two')], finish_reason='length')
        answer = ChatResult('A complete answer', {}, finish_reason='stop')
        client = Mock()
        client.chat.side_effect = [partial, answer]
        original = copy.deepcopy(self.messages)
        events = Mock()
        self.assertIs(call_model(client, self.messages, on_event=events, **self.options), answer)
        self.assertEqual(client.chat.call_count, 2)
        self.assertEqual(client.chat.call_args.kwargs['max_tokens'], 2048)
        self.assertEqual(self.messages, original)
        self.assertEqual([row.args[0] for row in events.call_args_list], ['model_output_recovery'])

    def test_invalid_json_with_tool_calls_finish_reason_is_recovered_once(self):
        bad = ChatResult('', {}, tool_calls=[call('upsert_step', '{')], finish_reason='tool_calls')
        client = Mock()
        client.chat.side_effect = [bad, bad]
        with self.assertRaises(LlmError):
            call_model(client, self.messages, **self.options)
        self.assertEqual(client.chat.call_count, 2)

    def test_empty_response_is_recovered_once(self):
        client = Mock()
        client.chat.side_effect = [ChatResult('', {}, finish_reason='stop'), ChatResult('ok', {})]
        self.assertEqual(call_model(client, self.messages, **self.options).text, 'ok')
        self.assertEqual(client.chat.call_count, 2)

    def test_recovery_and_transport_share_four_request_limit(self):
        bad = ChatResult('', {}, finish_reason='length')
        client = Mock()
        client.chat.side_effect = [bad, ConnectionError('offline'), ConnectionError('offline'), ConnectionError('offline')]
        with patch('pko.scenario_map.model_retry.time.sleep'), self.assertRaises(LlmError):
            call_model(client, self.messages, **self.options)
        self.assertEqual(client.chat.call_count, 4)

    def test_context_capacity_can_prevent_larger_recovery(self):
        client = Mock()
        client.chat.return_value = ChatResult('', {'prompt_tokens': 7500}, finish_reason='length')
        with self.assertRaises(LlmError):
            call_model(client, self.messages, **self.options)
        self.assertEqual(client.chat.call_count, 1)

    def test_recovery_rechecks_deadline_after_checkpoint(self):
        client = Mock()
        client.chat.return_value = ChatResult('', {}, finish_reason='length')
        clock = [1]
        with patch('pko.scenario_map.model_retry.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(ModelDeadlineExceeded):
                call_model(client, self.messages, deadline=10, max_tokens=1024, recovery_max_tokens=4096,
                           before_retry=lambda: clock.__setitem__(0, 20))
        self.assertEqual(client.chat.call_count, 1)


class ResultLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = build_workspace(sources(), Path(self.tmp.name) / 'sources')

    def test_all_early_stops_keep_final_snapshot_and_specific_reason(self):
        for exception, extra, stop in ((RuntimeError('failure'), {}, 'error'),
                                      (KeyboardInterrupt(), {}, 'cancelled'),
                                      (None, {'max_steps': 1}, 'step_limit'),
                                      (ModelDeadlineExceeded('Time exhausted'), {}, 'time_limit')):
            with self.subTest(stop=stop):
                client = Mock(spec=ScriptedClient)
                client.spec = SPEC
                client.chat.side_effect = exception
                if exception is None:
                    client = ScriptedClient()
                saved = []
                result = run_agent(self.workspace, SCENARIO, SPEC, client=client,
                                   checkpoint=saved.append, **extra)
                self.assertEqual(result['run']['stop_kind'], stop)
                self.assertTrue(saved)
                self.assertEqual(saved[-1]['run']['stop_kind'], stop)
                self.assertEqual(saved[-1]['nodes'], result['nodes'])

    def test_discarded_model_batch_never_executes_or_enters_history(self):
        class Truncated(ScriptedClient):
            first = True
            def chat(self, messages, **kwargs):
                if self.first:
                    self.first = False
                    return ChatResult('', {}, tool_calls=[
                        call('set_scope', '{"title":"SHOULD NOT EXECUTE"}', 'discard-me'),
                        call('upsert_step', '{', 'broken')], finish_reason='length')
                return super().chat(messages, **kwargs)
        client = Truncated()
        model = run_agent(self.workspace, SCENARIO, SPEC, client=client, max_steps=2)
        self.assertNotEqual(model['scenario']['title'], 'SHOULD NOT EXECUTE')
        self.assertNotIn('discard-me', json.dumps(client.seen))
        self.assertEqual(model['run']['stop_kind'], 'step_limit')

    def test_rejection_identity_ignores_cosmetic_mutations(self):
        payload = {'ok': False, 'code': 'quality_check', 'error': 'Missing branch', 'details': {'node_id': 'a'}}
        one = {'name': 'upsert_step', 'arguments': json.dumps({'id': 'a', 'name': 'First title'})}
        two = {'name': 'upsert_step', 'arguments': json.dumps({'id': 'a', 'name': 'Another title'})}
        self.assertEqual(_rejection_key(one, payload), _rejection_key(two, payload))
        other = dict(payload, details={'node_id': 'b'})
        self.assertNotEqual(_rejection_key(one, payload), _rejection_key(two, other))


class GroupingAndPresentationTests(unittest.TestCase):
    def model(self):
        return {'schema_version': '2.0', 'semantic_profile': 'variant-actions-v1',
                'scenario': {'title': 'Бизнес-этапы исходного проекта', 'entry_operation_ids': ['a'], 'terminal_operation_ids': ['a']},
                'nodes': [{'id': 'a', 'kind': 'operation', 'name': 'Read data'}], 'edges': [],
                'research_tasks': [{'id': 'task-1', 'status': 'open'}],
                'validation': {'structural_issues': [], 'coverage_issues': ['Still not read']},
                'run': {'status': 'partial', 'stop_kind': 'error', 'reason': '<offline>'}}

    def test_partial_coverage_is_groupable_but_structural_errors_are_not(self):
        model = self.model()
        self.assertTrue(_can_group(model))
        model['validation']['structural_issues'] = ['Missing transition target']
        self.assertFalse(_can_group(model))
        model['validation']['structural_issues'] = []
        model['edges'] = [{'source': 'a', 'target': 'missing'}]
        self.assertFalse(_can_group(model))
        model['edges'] = []
        model['scenario']['terminal_operation_ids'] = []
        self.assertFalse(_can_group(model))

    def test_rebuild_cannot_dispatch_grouping_for_broken_structure(self):
        model = self.model()
        model['validation']['structural_issues'] = ['Broken reference']
        with tempfile.TemporaryDirectory() as tmp, patch('pko.scenario_map.runner._stage_model') as provider:
            hierarchy = _publish_stages(model, Path(tmp), run_id='run', client=None, spec=SPEC, model_timeout=5)
        provider.assert_not_called()
        self.assertEqual(hierarchy['status']['result'], 'none')

    def test_runner_skips_grouping_after_cancellation_or_deadline(self):
        for stop in ('cancelled', 'time_limit'):
            model = self.model()
            model['run']['stop_kind'] = stop
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as tmp:
                with patch('pko.scenario_map.runner.run_agent', return_value=model), \
                     patch('pko.scenario_map.runner._publish_stages') as grouping:
                    result = run_scenario(SCENARIO, sources(), Path(tmp) / 'out', spec=SPEC, client=ScriptedClient())
                grouping.assert_not_called()
                self.assertEqual(result['run']['grouping_status'], stop)

    def test_grouping_deadline_is_rechecked_before_creating_model(self):
        with tempfile.TemporaryDirectory() as tmp, patch('pko.scenario_map.runner._stage_model') as provider:
            _publish_stages(self.model(), Path(tmp), run_id='run', client=None, spec=SPEC, model_timeout=5,
                            deadline=time.monotonic() - 1)
        provider.assert_not_called()

    def test_cancellation_during_grouping_preserves_completed_map(self):
        model = self.model()
        model['run'].update(status='completed', stop_kind='finish', reason='')
        stage_model = Mock()
        stage_model.budget.snapshot.return_value = {}
        with tempfile.TemporaryDirectory() as tmp:
            with patch('pko.scenario_map.runner._stage_model', return_value=stage_model), \
                 patch('pko.scenario_map.runner.grouping.build_hierarchy', side_effect=KeyboardInterrupt) as group:
                hierarchy = _publish_stages(model, Path(tmp), run_id='run', client=None, spec=SPEC, model_timeout=5)
            self.assertTrue((Path(tmp) / 'scenario_map.html').exists())
            html = (Path(tmp) / 'scenario_map.html').read_text(encoding='utf-8')
        group.assert_called_once()
        self.assertEqual(model['run']['status'], 'completed')
        self.assertEqual(model['run']['stop_kind'], 'finish')
        self.assertEqual(model['run']['grouping_status'], 'cancelled')
        self.assertEqual(hierarchy['status']['result'], 'none')
        self.assertIn('отменена пользователем', html)

    def test_persistent_emergency_summary_separates_dimensions(self):
        html = render_scenario_map(self.model())
        section = html.split('<section id="result-lifecycle"', 1)[1].split('</section>', 1)[0]
        self.assertNotIn(' hidden', section)
        for label in ('Аварийный снимок', 'Структура', 'Покрытие', 'Группировка', '&lt;offline&gt;'):
            self.assertIn(label, section)
        self.assertIn('незавершённых задач: 1', section)

    def test_new_labels_preserve_model_text_and_old_profile_labels(self):
        model = self.model()
        html = render_scenario_map(model)
        self.assertIn('<span class="mode-long">Действия</span>', html)
        self.assertIn('<span class="mode-long">Бизнес-операции</span>', html)
        self.assertIn('Бизнес-этапы исходного проекта', html)
        self.assertNotIn('Каждая действие', html)
        self.assertNotIn('отдельная действие', html)
        model.pop('semantic_profile')
        legacy = render_scenario_map(model)
        self.assertIn('<span class="mode-long">Атомарные операции</span>', legacy)
        self.assertIn('<span class="mode-long">Бизнес-этапы</span>', legacy)

    def test_unknown_semantic_profile_is_rejected_without_hierarchy(self):
        model = self.model()
        model['semantic_profile'] = 'future-profile'
        with self.assertRaisesRegex(ValueError, 'semantic profile'):
            render_scenario_map(model)
        for legacy_profile in (None, ''):
            model['semantic_profile'] = legacy_profile
            self.assertIn('<span class="mode-long">Атомарные операции</span>', render_scenario_map(model))


if __name__ == '__main__':
    unittest.main()
