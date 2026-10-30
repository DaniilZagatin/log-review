"""Policy-2 protocol controls for the map consumer: repair per logical response, optional-proposal isolation,
blocking vs runtime questions, request vs result, reference shapes and navigation reads. Synthetic inputs only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from process_observability.logging_contracts import (LEGACY_POLICY, POLICY, LoggingReviewError,
                                                     validate_logging_contracts)
from process_observability.logging_review import review_logging
from process_observability.map_input import MapInputError, MapView, RepoContext
from process_observability.operation_coverage import binding_is_sufficient, validate_saved_rows
from process_observability.source_tools import snapshot_repository
from test_map_first_logging import CODE, Scripted, model_reply, operation

PLAIN = "def archive(item):\n    return item\n"


def site_at(packet, line):
    return next(s for s in packet['candidates'] if s['start_line'] == line)


def change(identifier, kind='add_event', *, existing=(), location=(5, 5), path='app.py', fields=(), placement='after'):
    value = {'id': identifier, 'kind': kind, 'operation_ids': ['save'], 'problem': 'Не видно результата',
             'what': 'Дополнить запись результата', 'why': 'Видеть исход операции', 'fields': list(fields),
             'location': None if location is None else {'path': path, 'start_line': location[0], 'end_line': location[1]},
             'timing': 'После сохранения', 'verification': 'Одна запись на успешное сохранение'}
    if existing: value['existing_site_ids'] = list(existing)
    if kind in ('add_event','move_emit') and location is not None:value.update(placement=placement,guards=[])
    return value


class ProtocolControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def inputs(self, operations=None, code=CODE, extra=None):
        (self.root/'app.py').write_text(code)
        for name, text in (extra or {}).items(): (self.root/name).write_text(text)
        view = MapView.from_dict({'schema_version': '2.0', 'scenario': {}, 'sources': [{'id': 'source-1'}],
                                  'nodes': operations if operations is not None else [operation(code)], 'edges': [],
                                  'run': {'id': 'protocol-fixture', 'status': 'completed'}})
        return view, snapshot_repository(self.root, max_files=None, max_chars=None)

    def review(self, callback, operations=None, **kwargs):
        view, snapshot = self.inputs(operations, extra=kwargs.pop('extra', None))
        model = Scripted(callback)
        return model, review_logging(view, snapshot, model, **kwargs)

    # Navigation reads and reference shapes

    def test_range_feedback_names_the_file_length_and_citations_are_never_clamped(self):
        view, snapshot = self.inputs()
        context = RepoContext(snapshot, view)
        with self.assertRaises(MapInputError) as caught: context.read('app.py', 1, 200)
        self.assertIn('the file has 11 lines', str(caught.exception))
        span = context.read('app.py', 8, 200, clamp=True)
        self.assertEqual((span['start_line'], span['end_line'], span['line_count']), (8, 11, 11))
        self.assertEqual((span['requested']['end_line'], span['clamped']), (200, True))
        with self.assertRaises(MapInputError) as caught: context.read('app.py', 40, 60, clamp=True)
        self.assertIn('11 lines', str(caught.exception))
        # The navigation read delivered 8–11, yet an evidence citation past the end is still invalid.
        with self.assertRaises(MapInputError) as caught: context.verify_ref({'path': 'app.py', 'start_line': 8, 'end_line': 200})
        self.assertIn('requested 8–200; the file has 11 lines', str(caught.exception))

    def test_failed_inspect_items_do_not_discard_successful_reads_and_searches(self):
        def callback(packet, number):
            if number == 1:
                return {'action': 'inspect', 'reads': [{'path': 'app.py', 'start_line': 40, 'end_line': 50},
                                                       {'path': 'app.py', 'start_line': 8, 'end_line': 30}],
                        'searches': ['<thinking>PRIVATE_SEARCH_MARKER</thinking>', 'logger.error']}
            return model_reply(packet)
        model, result = self.review(callback)
        tool = model.calls[1]['tool_results'][0]['result']
        self.assertEqual([(r['start_line'], r['end_line']) for r in tool['reads']], [(8, 11)])
        self.assertEqual(tool['reads'][0]['requested']['end_line'], 30)
        self.assertEqual(sorted(item['tool'] for item in tool['failed_items']), ['reads', 'searches'])
        self.assertIn('11 lines', next(i for i in tool['failed_items'] if i['tool'] == 'reads')['error'])
        self.assertEqual([s['term'] for s in tool['searches']], ['logger.error'])
        self.assertTrue(any(s['start_line'] == 9 for s in model.calls[1]['packet']['candidates']))
        self.assertEqual(result['operations'][0]['state'], 'C')
        self.assertNotIn('PRIVATE_SEARCH_MARKER', json.dumps(result))

    def test_string_invocation_ref_gets_exact_shape_feedback_and_is_not_guessed(self):
        def callback(packet, number):
            value = model_reply(packet); value['bindings'][0]['invocation_ref'] = 'app.py:4'; return value
        model, result = self.review(callback)
        self.assertEqual(len(model.calls), 2)
        self.assertIn('invocation_ref must be null or an object', model.calls[1]['repair_errors'][0])
        self.assertEqual(result['operations'][0]['state'], 'X')

    def test_exact_known_reference_aliases_are_normalized_before_strict_validation(self):
        def callback(packet, number):
            value = model_reply(packet)
            value['bindings'][0]['evidence'] = [{'path': 'app.py', 'line_start': 5, 'line_end': 5}]
            return value
        model, result = self.review(callback)
        self.assertEqual((len(model.calls), result['operations'][0]['state']), (1, 'C'))
        self.assertEqual(result['operations'][0]['bindings'][0]['evidence'][0]['start_line'], 5)
        def conflicting(packet, number):
            value = model_reply(packet)
            value['bindings'][0]['evidence'] = [{'path': 'app.py', 'start_line': 5, 'end_line': 5, 'line_start': 4}]
            return value
        model, result = self.review(conflicting)
        self.assertIn('conflicting line_start and start_line', model.calls[1]['repair_errors'][0])
        self.assertEqual(result['operations'][0]['state'], 'X')

    # One repair per logical response

    def test_successful_inspect_starts_a_new_response_with_its_own_single_repair(self):
        def callback(packet, number):
            if number in (1, 3):
                value = model_reply(packet); value['operation_id'] = 'foreign'; return value
            if number == 2: return {'action': 'inspect', 'reads': [{'path': 'app.py', 'start_line': 8, 'end_line': 9}]}
            return model_reply(packet)
        model, result = self.review(callback)
        requests = [t for t in result['trace'] if t['event'] == 'model_request']
        self.assertEqual([t['attempt'] for t in requests], ['primary', 'repair', 'primary', 'repair'])
        self.assertEqual([t['response'] for t in requests], [0, 0, 1, 1])
        self.assertEqual(result['operations'][0]['state'], 'C')

    def test_no_hidden_third_attempt_for_the_same_invalid_response(self):
        def callback(packet, number):
            if number == 2: return {'action': 'inspect', 'reads': [{'path': 'app.py', 'start_line': 8, 'end_line': 9}]}
            value = model_reply(packet); value['operation_id'] = 'foreign'; return value
        model, result = self.review(callback)
        self.assertEqual(len(model.calls), 4)
        self.assertEqual(result['model_requests'], 4)
        self.assertEqual(result['operations'][0]['state'], 'X')

    def test_empty_inspect_does_not_renew_the_spent_repair(self):
        def callback(packet, number):
            if number == 2: return {'action': 'inspect', 'reads': [{'path': 'app.py', 'start_line': 90, 'end_line': 95}]}
            value = model_reply(packet); value['operation_id'] = 'foreign'; return value
        model, result = self.review(callback)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual([t['attempt'] for t in result['trace'] if t['event'] == 'model_request'], ['primary', 'repair'])
        self.assertEqual(result['operations'][0]['state'], 'X')

    def test_transport_failure_on_the_repair_keeps_a_core_valid_primary(self):
        from process_observability.llm import LLMError
        def callback(packet, number):
            if number == 1: return model_reply(packet, changes=[change('bad', 'enrich', existing=['missing-site-id'])])
            raise LLMError('Fixture unavailable', category='http', status=500)
        model, result = self.review(callback)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result['operations'][0]['state'], 'C')
        self.assertEqual(result['checkpoints'][0]['accepted_attempt'], 'primary')

    # Optional proposals

    def test_private_text_in_a_rejected_proposal_never_becomes_valid_on_reuse(self):
        proposal = change('tagged', location=None); proposal['what'] = '<thinking>PRIVATE_PROPOSAL_MARKER</thinking>'
        model, result = self.review(lambda p, n: model_reply(p, changes=[proposal]))
        self.assertEqual(result['operations'][0]['state'], 'C')
        self.assertEqual(result['operations'][0]['change_diagnostics'][0]['local_id'], 'tagged')
        self.assertNotIn('PRIVATE_PROPOSAL_MARKER', json.dumps(result))
        view, snapshot = self.inputs()
        again = review_logging(view, snapshot, Scripted(lambda p, n: self.fail('must reuse')), resume=result)
        self.assertEqual(again['operations'], result['operations'])
        self.assertFalse(again['changes'])

    def test_invalid_optional_proposal_is_isolated_after_repair_and_keeps_the_sound_binding(self):
        bad = change('bad', 'enrich', existing=['missing-site-id'])
        model, result = self.review(lambda p, n: model_reply(p, changes=[bad]))
        row = result['operations'][0]
        self.assertEqual(len(model.calls), 2)
        self.assertIn('unknown existing site', model.calls[1]['repair_errors'][0])
        self.assertEqual(row['state'], 'C')
        self.assertEqual(row['change_diagnostics'][0]['local_id'], 'bad')
        self.assertFalse(result['changes'])
        self.assertTrue(any('необязательные доработки' in w for w in row['warnings']))
        checkpoint = result['checkpoints'][0]
        self.assertEqual((checkpoint['accepted_attempt'], checkpoint['changes_isolated']), ('repair', True))
        self.assertEqual(checkpoint['reply']['changes'][0]['id'], 'bad')  # the raw attempt is preserved
        rejected = [t for t in result['trace'] if t['event'] == 'validation_rejected']
        self.assertTrue(all(t['optional_proposal_only'] for t in rejected) and len(rejected) == 2)
        self.assertTrue(any(t['event'] == 'optional_proposals_isolated' for t in result['trace']))
        view, snapshot = self.inputs()
        again = review_logging(view, snapshot, Scripted(lambda p, n: self.fail('isolated decision must be reusable')), resume=result)
        self.assertEqual(again['operations'], result['operations'])
        validate_saved_rows(MapView.from_dict(result['map']), result)

    def test_invalid_mandatory_citation_still_blocks_even_with_a_valid_proposal(self):
        def callback(packet, number):
            value = model_reply(packet, changes=[change('valid', location=None)])
            value['bindings'][0]['evidence'] = [{'path': 'app.py', 'start_line': 9, 'end_line': 9}]
            return value
        model, result = self.review(callback)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result['operations'][0]['state'], 'X')
        self.assertFalse(result['changes'])

    def test_only_the_invalid_proposal_is_dropped_and_dependents_follow_it(self):
        def callback(packet, number):
            site = site_at(packet, 5)
            good = change('good', existing=[site['id']])
            broken = change('broken', 'enrich', existing=['missing-site-id'])
            dependent = change('dependent', existing=[site['id']]); dependent['dependencies'] = ['broken']
            return model_reply(packet, role='start', establishes=['occurrence'], changes=[good, broken, dependent])
        model, result = self.review(callback)
        row = result['operations'][0]
        self.assertEqual(row['state'], 'P')
        self.assertEqual([c['local_id'] for c in row['changes']], ['good'])
        self.assertEqual(sorted(d['local_id'] for d in row['change_diagnostics']), ['broken', 'dependent'])
        self.assertEqual(row['changes'][0]['logger_access']['basis'], 'existing_record')

    def test_core_valid_earlier_attempt_is_used_when_the_repair_breaks_mandatory_fields(self):
        def callback(packet, number):
            if number == 1: return model_reply(packet, changes=[change('bad', 'enrich', existing=['missing-site-id'])])
            value = model_reply(packet); value['operation_id'] = 'foreign'; return value
        model, result = self.review(callback)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result['operations'][0]['state'], 'C')
        self.assertEqual(result['checkpoints'][0]['accepted_attempt'], 'primary')
        self.assertTrue(result['checkpoints'][0]['changes_isolated'])

    def test_new_record_next_to_a_bound_record_must_name_it(self):
        model, result = self.review(lambda p, n: model_reply(p, role='start', establishes=['occurrence'], changes=[change('blind')]))
        row = result['operations'][0]
        self.assertEqual(row['state'], 'P')
        self.assertIn('existing_site_ids', row['change_diagnostics'][0]['error'])
        self.assertFalse(row['changes'])

    def test_logger_dependency_is_explicit_when_no_logger_exists_at_the_location(self):
        node = operation(PLAIN, identifier='archive', first=1, last=2, effect='unknown', outcome='unknown')
        node['evidence'][0]['path'] = 'source-1/plain.py'
        def callback(packet, number):
            proposal = change('record', location=(2, 2), path='plain.py',placement='before'); proposal['operation_ids'] = ['archive']
            return model_reply(packet, key='absent', changes=[proposal])
        model, result = self.review(callback, [node], extra={'plain.py': PLAIN})
        proposal = result['operations'][0]['changes'][0]
        self.assertEqual(result['operations'][0]['state'], 'U')
        self.assertEqual(proposal['logger_access']['status'], 'requires_dependency')
        self.assertTrue(proposal['requirements'])
        def inspect_module(packet,number):
            if number == 1:
                return {'action':'inspect','reads':[{'path':'app.py','start_line':1,'end_line':2}]}
            return model_reply(packet,key='absent',changes=[dict(change('module',location=(11,11),placement='before'),operation_ids=['unlogged'])])
        module = self.review(inspect_module,[operation(identifier='unlogged',first=10,last=11,effect='unknown',outcome='unknown')])[1]
        self.assertEqual(module['operations'][0]['changes'][0]['logger_access']['basis'], 'module_logger')
        self.assertTrue(module['operations'][0]['changes'][0]['logger_access']['evidence'])

    # Blocking source questions vs runtime limitations

    def test_runtime_question_does_not_reopen_a_completed_static_review(self):
        def callback(packet, number):
            value = model_reply(packet)
            value['review_tasks'] = [{'question': 'Включён ли уровень INFO в рабочей среде', 'scope': 'runtime'}]
            return value
        model, result = self.review(callback)
        row = result['operations'][0]
        self.assertEqual(row['state'], 'C')
        self.assertEqual([(t['kind'], t['blocking'], t['scope']) for t in row['review_tasks']], [('runtime_question', False, 'runtime')])
        validate_saved_rows(MapView.from_dict(result['map']), result)

    def test_source_question_and_untyped_legacy_question_block(self):
        for task in ({'question': 'Проверить ветку отказа', 'scope': 'source'}, 'Проверить ветку отказа',
                     {'question': 'Слишком широкая операция', 'scope': 'map'}):
            with self.subTest(task=task):
                def callback(packet, number, task=task):
                    value = model_reply(packet); value['review_tasks'] = [task]; return value
                model, result = self.review(callback)
                self.assertEqual(result['operations'][0]['state'], 'X')
                self.assertTrue(result['operations'][0]['review_tasks'][0]['blocking'])

    def test_unknown_task_scope_is_a_schema_error_not_a_downgrade(self):
        def callback(packet, number):
            value = model_reply(packet); value['review_tasks'] = [{'question': 'Позже', 'scope': 'later'}]; return value
        model, result = self.review(callback)
        self.assertIn('scope', model.calls[1]['repair_errors'][0])
        self.assertEqual(result['operations'][0]['state'], 'X')

    def test_unassessed_hypotheses_are_never_published_as_established_absence(self):
        def callback(packet, number):
            value = model_reply(packet, key='absent'); value['missing'] = ['Нет записи результата']
            value['review_tasks'] = ['Найти обёртку журнала']; return value
        model, result = self.review(callback)
        row = result['operations'][0]
        self.assertEqual((row['state'], row['missing_status']), ('X', 'hypothesis'))
        view = MapView.from_dict(result['map'])
        validate_saved_rows(view, result)
        edited = deepcopy(result); edited['operations'][0]['missing_status'] = 'established'
        with self.assertRaises(LoggingReviewError): validate_saved_rows(view, edited)

    # Request intent vs sent command vs result

    def test_pre_action_request_never_proves_a_command_or_result(self):
        command = [operation(effect='command', outcome='unknown')]
        cases = [(command, 'request', 'before_action', 'P'), (command, 'request', 'after_action', 'C'),
                 (None, 'request', 'after_action', 'P'), (None, 'result', 'before_action', 'X'),
                 (None, 'result', 'after_confirmation', 'C')]
        for operations, role, moment, expected in cases:
            with self.subTest(role=role, moment=moment, command=operations is not None):
                def callback(packet, number, role=role, moment=moment):
                    value = model_reply(packet, role=role); value['bindings'][0]['moment'] = moment
                    if moment == 'before_action' and role == 'request':
                        value['bindings'][0].update(establishes=['occurrence'], covers_outcomes=[])
                    if role == 'request' and moment == 'after_action' and operations is not None:
                        value['missing'] = []  # The command itself was sent; no claim about receiver application.
                    return value
                self.assertEqual(self.review(callback, operations)[1]['operations'][0]['state'], expected)

    def test_saved_policy_keeps_its_own_request_rule(self):
        command = {'id': 'send', 'effect_kind': 'command', 'outcome': 'unknown'}
        legacy = {'role': 'request', 'establishes': ['occurrence', 'result']}
        self.assertTrue(binding_is_sufficient(command, legacy, {}, policy=LEGACY_POLICY))
        self.assertFalse(binding_is_sufficient(command, legacy, {}, policy=POLICY))
        self.assertTrue(binding_is_sufficient(command, {**legacy, 'moment': 'after_action'}, {}, policy=POLICY))
        model, result = self.review(lambda p, n: dict(model_reply(p, role='request'), missing=[], bindings=[
            dict(model_reply(p, role='request')['bindings'][0], moment='after_action')]), [operation(effect='command', outcome='unknown')])
        self.assertEqual(result['operations'][0]['state'], 'C')
        view = MapView.from_dict(result['map'])
        edited = deepcopy(result); del edited['operations'][0]['bindings'][0]['moment']
        with self.assertRaises(LoggingReviewError): validate_saved_rows(view, edited)
        edited['policy'] = LEGACY_POLICY
        validate_saved_rows(view, edited)

    # Dispositions and registry

    def test_real_record_of_another_operation_is_a_disposition_not_a_not_log_claim(self):
        operations = [operation(), operation(identifier='other', first=8, last=9)]
        def callback(packet, number):
            if packet['operation']['id'] == 'other': return model_reply(packet, key='absent')
            if number == 1: return {'action': 'inspect', 'reads': [{'path': 'app.py', 'start_line': 8, 'end_line': 9}]}
            value = model_reply(packet)
            value['candidate_reviews'] = [{'site_id': site_at(packet, 9)['id'], 'classification': 'other_operation_log',
                                           'related_operation_id': 'other', 'reason': 'Запись относится к другой операции',
                                           'evidence': [{'path': 'app.py', 'start_line': 9, 'end_line': 9}]}]
            return value
        model, result = self.review(callback, operations)
        self.assertEqual(result['operations'][0]['state'], 'C')
        status = {s['start_line']: s['review_status'] for s in result['inventory']['sites']}
        self.assertEqual((status[5], status[9]), ('mapped', 'other_operation'))

    def test_other_operation_disposition_is_strict(self):
        operations = [operation(), operation(identifier='other', first=8, last=9)]
        for related in ('save', 'not-in-map'):
            with self.subTest(related=related):
                def callback(packet, number, related=related):
                    if packet['operation']['id'] == 'other': return model_reply(packet, key='absent')
                    value = model_reply(packet)
                    value['candidate_reviews'] = [{'site_id': site_at(packet, 5)['id'], 'classification': 'other_operation_log',
                                                   'related_operation_id': related, 'reason': 'Чужая запись',
                                                   'evidence': [{'path': 'app.py', 'start_line': 5, 'end_line': 5}]}]
                    return value
                self.assertEqual(self.review(callback, operations)[1]['operations'][0]['state'], 'X')
        def both(packet, number):
            if packet['operation']['id'] == 'other': return model_reply(packet, key='absent')
            value = model_reply(packet)
            value['candidate_reviews'] = [{'site_id': site_at(packet, 5)['id'], 'classification': 'other_operation_log',
                                           'reason': 'Чужая запись', 'evidence': [{'path': 'app.py', 'start_line': 5, 'end_line': 5}]}]
            return value
        model, result = self.review(both, operations)
        self.assertIn('another operation', model.calls[1]['repair_errors'][0])
        self.assertEqual(result['operations'][0]['state'], 'X')

    # Contracts and cache identity

    def test_declared_logging_contracts_are_validated_and_part_of_the_cache_identity(self):
        contracts = [{'callee': 'Vendor.Journal.info', 'basis': 'Синтетическое заявление оператора'}]
        with self.assertRaises(LoggingReviewError): validate_logging_contracts([{'callee': 'info', 'basis': 'x'}])
        with self.assertRaises(LoggingReviewError): validate_logging_contracts(contracts * 2)
        with self.assertRaises(LoggingReviewError): validate_logging_contracts({'schema_version': 'other', 'contracts': contracts})
        self.assertEqual(validate_logging_contracts({'schema_version': 'logging-contracts/1', 'contracts': contracts}), contracts)
        view, snapshot = self.inputs()
        first = review_logging(view, snapshot, Scripted(lambda p, n: model_reply(p)), logging_contracts=contracts)
        self.assertEqual(first['logging_contracts'], contracts)
        self.assertEqual(first['policy'], POLICY)
        same = review_logging(view, snapshot, Scripted(lambda p, n: self.fail('same contract must reuse')), resume=first,
                              logging_contracts=contracts)
        changed = review_logging(view, snapshot, Scripted(lambda p, n: model_reply(p)), resume=first,
                                 logging_contracts=[dict(contracts[0], callee='Vendor.Journal.warn')])
        self.assertEqual((same['model_requests'], changed['model_requests']), (0, 1))

    def test_contract_file_is_optional_explicit_input_with_a_checksum(self):
        from process_observability.logging_cli import load_logging_contracts
        folder = self.root/'artifacts'; folder.mkdir()
        self.assertEqual(load_logging_contracts(folder), ([], None))
        (folder/'logging_contracts.json').write_text(json.dumps({'schema_version': 'logging-contracts/1', 'contracts': [
            {'callee': 'Vendor.Journal.info', 'basis': 'Синтетическое заявление оператора'}]}))
        contracts, sha = load_logging_contracts(folder)
        self.assertEqual((contracts[0]['callee'], len(sha)), ('Vendor.Journal.info', 64))
        (folder/'logging_contracts.json').write_text(json.dumps({'contracts': []}))
        with self.assertRaises(LoggingReviewError): load_logging_contracts(folder)


if __name__ == '__main__': unittest.main()
