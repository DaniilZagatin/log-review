"""Codex takeover controls: Russian requests and source-interpreted Java adapters; no network/application execution."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from process_observability.logging_review import review_logging, REVIEW_PROFILE
from process_observability.logging_contracts import LoggingReviewError
from process_observability.logging_navigation import LoggingNavigation
from process_observability.map_input import MapView
from process_observability.operation_coverage import validate_saved_rows
from process_observability.source_tools import snapshot_repository
from test_map_first_logging import CODE, Scripted, model_reply, operation
from test_map_first_wrapper_review import FILES, GATEWAY, node, bind, reply

BASE = 'src/com/example/journal/BaseJournal.java'
PORT = 'src/com/example/journal/RecordingPort.java'


class TakeoverControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_java(self, mutate=None, resume=None):
        files = {**FILES, PORT: 'package com.example.journal;\npublic interface RecordingPort {\n'
                               '    /** Записать текст в журнал. */\n    void write(String text, int level);\n}\n'}
        files[BASE] = files[BASE].replace('class BaseJournal {', 'class BaseJournal implements RecordingPort {')
        files[BASE] = files[BASE].replace('protected void write', 'public void write')
        for path, text in files.items():
            p = self.root/path; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
        view = MapView.from_dict({'schema_version': '2.0', 'sources': [{'id': 'source-1'}], 'scenario': {},
            'nodes': [node(GATEWAY, files[GATEWAY], 10, 15)], 'edges': [], 'run': {'id': 'synthetic', 'status': 'completed'}})
        snap = snapshot_repository(self.root, max_files=None, max_chars=None, logging_calls=['journal.record'])
        def callback(packet, n):
            bindings = []
            for line, role, moment in [(11, 'request', 'before_action'), (13, 'result', 'after_confirmation')]:
                row = bind(packet, line, role=role, moment=moment)
                site = next(s for s in packet['candidates'] if s['id'] == row['site_id'])
                candidates = site['logging_chain']['adapter_candidates']
                self.assertEqual(len(candidates), 1)
                plan = candidates[0]; contract = plan['contract_basis']
                row['source_contract'] = {'candidate_id': contract['candidate_id'],
                    'meaning': 'Интерфейс принимает текст для записи в журнал, реализация передаёт его SDK'}
                row['emission_evidence'] = [{k: r[k] for k in ('path', 'start_line', 'end_line')}
                    for r in plan['hops'] + [plan['terminal'], contract['declaration'], contract['implementation']]]
                if mutate: mutate(row)
                bindings.append(row)
            return reply(packet, bindings)
        result = review_logging(view, snap, Scripted(callback), resume=resume)
        return view, result

    def test_source_interface_adapter_is_semantic_interpretation_with_exact_witnesses(self):
        view, result = self.run_java()
        row = result['operations'][0]
        self.assertEqual(row['state'], 'C', row['review_tasks'])
        contract = row['bindings'][1]['chain']['source_contract']
        self.assertEqual(contract['authority'], 'model_interpretation_of_source')
        self.assertTrue(contract['declaration']['quote'])
        self.assertTrue(any('оценивает модель' in x for x in row['limitations']))
        validate_saved_rows(view, result)

    def test_arbitrary_or_foreign_source_contract_id_does_not_bind(self):
        _, result = self.run_java(lambda row: row['source_contract'].update(candidate_id='adapter-made-up'))
        self.assertEqual(result['operations'][0]['state'], 'X')

    def test_unwitnessed_interface_does_not_bind(self):
        def omit(row): row['emission_evidence'] = [r for r in row['emission_evidence'] if r['path'] != PORT]
        _, result = self.run_java(omit)
        self.assertEqual(result['operations'][0]['state'], 'X')

    def test_missing_contract_semantic_reason_does_not_bind(self):
        _, result = self.run_java(lambda row: row['source_contract'].update(meaning=''))
        self.assertEqual(result['operations'][0]['state'], 'X')

    def test_product_system_and_human_examples_are_russian(self):
        (self.root/'app.py').write_text(CODE)
        view = MapView.from_dict({'schema_version': '2.0', 'scenario': {}, 'sources': [{'id': 'source-1'}],
                                 'nodes': [operation()], 'edges': [], 'run': {'id': 'ru', 'status': 'completed'}})
        class Capture(Scripted):
            def complete_json(self, system, user):
                self.system = system
                return super().complete_json(system, user)
        model = Capture(lambda p, n: model_reply(p))
        review_logging(view, snapshot_repository(self.root, max_files=None, max_chars=None), model)
        self.assertIn('Все объяснения для пользователя пиши на русском', model.system)
        self.assertIn('"what":"Конкретное изменение"', model.system)
        self.assertNotIn('concrete change', model.system)
        self.assertIn('role: request/start/result/receiver_response/error/repeat/no_op', model.system)
        self.assertEqual(REVIEW_PROFILE['public_language'], 'ru')

    def test_multiple_implementations_do_not_establish_an_invocation_route(self):
        code = ('interface Port { void store(); }\n'
                'class One implements Port { public void store() { logger.info("one"); } }\n'
                'class Two implements Port { public void store() { logger.info("two"); } }\n'
                'class Entry { Port port; void run() { port.store(); } }\n')
        (self.root/'flow.java').write_text(code)
        nav = LoggingNavigation(snapshot_repository(self.root, max_files=None, max_chars=None))
        call = {'path': 'flow.java', 'start_line': 4, 'end_line': 4}
        site = {'path': 'flow.java', 'start_line': 2, 'end_line': 2}
        with self.assertRaises(LoggingReviewError): nav.invocation_route(call, site, [call], [call])

    def test_route_requires_true_or_explicit_confirmed_status(self):
        code = ('import logging\nlogger = logging.getLogger(__name__)\n'
                'def record():\n    logger.info("done")\ndef apply():\n    record()\n')
        (self.root/'app.py').write_text(code)
        op = operation(code, first=5, last=6, outcome='unknown')
        view = MapView.from_dict({'schema_version': '2.0', 'scenario': {}, 'sources': [{'id': 'source-1'}],
                                 'nodes': [op], 'edges': [], 'run': {'id': 'route', 'status': 'completed'}})
        def callback(p, n):
            site = next(s for s in p['candidates'] if s['event_key'] == 'done')
            call = {'path': 'app.py', 'start_line': 6, 'end_line': 6}
            return reply(p, [{'site_id': site['id'], 'role': 'result', 'moment': 'after_action',
                'meaning': 'Результат локального действия', 'conditions': 'В этой ветке', 'correlation': 'Контекст вызова',
                'establishes': ['occurrence', 'result'],
                'evidence': [{'path': 'app.py', 'start_line': 4, 'end_line': 4}],
                'invocation_ref': call, 'argument_evidence': [call], 'emission_evidence': [], 'covers_outcomes': []}])
        for value in (None, False, {}, {'status': 'unresolved'}):
            with self.subTest(value=value), patch.object(LoggingNavigation, 'invocation_route', return_value=value):
                model = Scripted(callback)
                result = review_logging(view, snapshot_repository(self.root, max_files=None, max_chars=None), model)
                self.assertEqual(result['operations'][0]['state'], 'X')
                self.assertEqual(len(model.calls), 2)

    def test_saved_adapter_declaration_cannot_lose_its_citation(self):
        view, result = self.run_java()
        binding = result['operations'][0]['bindings'][1]
        binding['emission_evidence'] = [r for r in binding['emission_evidence'] if r['path'] != PORT]
        with self.assertRaises(LoggingReviewError): validate_saved_rows(view, result)

    def test_saved_covered_row_cannot_be_relabelled_partial(self):
        view, result = self.run_java()
        result['operations'][0]['state'] = 'P'
        with self.assertRaises(LoggingReviewError): validate_saved_rows(view, result)

    def test_business_command_candidate_is_not_an_available_logger_dependency(self):
        from process_observability.logging_changes import _logger_access
        from process_observability.map_input import RepoContext
        code = 'def apply(item):\n    publisher.publish(item)\n'
        (self.root/'app.py').write_text(code)
        view = MapView.from_dict({'schema_version': '2.0', 'scenario': {}, 'sources': [{'id': 'source-1'}],
                                 'nodes': [operation(code, first=1, last=2)], 'edges': [], 'run': {'id': 'dep', 'status': 'completed'}})
        context = RepoContext(snapshot_repository(self.root, max_files=None, max_chars=None), view)
        site = {'id': 'candidate', 'kind': 'configured_call', 'callee': 'publisher.publish',
                'path': 'app.py', 'start_line': 2, 'end_line': 2}
        row = {'location': {'path': 'app.py', 'start_line': 2, 'end_line': 2}, 'existing_site_ids': ['candidate']}
        self.assertEqual(_logger_access({}, row, context, {'candidate': site}, 'save')['status'], 'requires_dependency')

    def test_selected_source_contract_can_omit_duplicate_manual_emission_refs(self):
        view,result=self.run_java(lambda row: row.pop('emission_evidence'))
        self.assertEqual(result['operations'][0]['state'],'C',result['operations'][0]['review_tasks'])
        binding=result['operations'][0]['bindings'][1]
        self.assertTrue(binding['emission_evidence'])
        self.assertEqual(binding['emission_evidence_origin'],'host_selected_source_contract')
        validate_saved_rows(view,result)

    def test_missing_source_contract_produces_field_addressed_choices(self):
        view,result=self.run_java(lambda row: row.pop('source_contract'))
        self.assertEqual(result['operations'][0]['state'],'X')
        rejection=next(t for t in result['trace'] if t['event']=='validation_rejected')
        details=rejection['repair_details']['issues']
        issue=next(i for i in details if i['code']=='logging_contract_required')
        self.assertEqual(issue['field'],'bindings[0].source_contract')
        self.assertTrue(issue['choices'])
        self.assertTrue(all(c['candidate_id'].startswith('adapter-') for c in issue['choices']))

    def test_host_selected_contract_is_revalidated_on_checkpoint_reuse(self):
        view,first=self.run_java(lambda row: row.pop('emission_evidence'))
        view,second=self.run_java(lambda row: row.pop('emission_evidence'),resume=first)
        self.assertEqual(first['operations'],second['operations'])
        self.assertEqual(second['model_requests'],0)

    def test_host_cannot_extract_an_unread_selected_interface(self):
        from process_observability.logging_review import _Navigator
        original=_Navigator.initial
        def without_interface(navigator,operation):
            packet=original(navigator,operation)
            packet['source']=[ref for ref in packet['source'] if ref['path']!=PORT]
            navigator.context.shown=[ref for ref in navigator.context.shown if ref['path']!=PORT]
            return packet
        with patch.object(_Navigator,'initial',without_interface):
            view,result=self.run_java(lambda row: row.pop('emission_evidence'))
        self.assertEqual(result['operations'][0]['state'],'X')
        issue=next(i for t in result['trace'] if t['event']=='validation_rejected'
                   for i in t['repair_details']['issues'] if i['code']=='unread_logging_contract')
        self.assertTrue(any(r['path']==PORT for r in issue['reads']))
