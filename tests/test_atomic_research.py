"""Ordinary atomic workflow with scripted source choices; OWN fixture text is never executed."""
import ast
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from process_observability import direct_atomic

from process_observability.source_tools import covering_ranges
from process_observability.onboarding_export import ExportError, _atomic_accounting
from process_observability.contracts import ProcessMap
from process_observability.action_refinement import _digest
from test_action_refinement import finding
from test_code_led_map import CodeLedCase, Business, GUARD, handlers, page_reply

PATH = 'app/intake.py'
SOURCE = GUARD + '''

def enter(parcel):
    return choose(parcel)

def choose(parcel):
    return match_policy(parcel)

def match_policy(parcel):
    if parcel.valid:
        return accept(parcel)
    return decline(parcel)

def accept(parcel):
    parcel.accepted = True
    parcel.save()
    return parcel

def decline(parcel):
    parcel.declined = True
    parcel.save()
    return parcel
'''
DECLS = {n.name: (n.lineno, n.end_lineno) for n in ast.parse(SOURCE).body if isinstance(n, ast.FunctionDef)}
FOUND = [finding('entry_point', 'Принять посылку и выбрать обработку', PATH, *DECLS['enter'], 'enter')]
SPEC = {'enter': [('Принять посылку', 'start', 'Приём', 'continues', None)],
        'match_policy': [('Проверить условия приёма', 'decision', 'Приём', 'continues', None)],
        'accept': [('Подтвердить приём', 'operation', 'Приём', 'terminal', None)],
        'decline': [('Отклонить приём', 'operation', 'Приём', 'terminal', None)]}


def request(tool, **values):
    command = {'tool': tool, 'unit_id': None, 'path': None, 'start_line': None, 'end_line': None,
               'query': None, 'offset': 0, 'limit': 24, 'section': None, **values}
    return {'operations': [], 'new_groups': [], 'unit_accounting': [], 'event_mappings': [],
            'operation_transitions': [], 'unknowns': [], 'source_request': command}


class AtomicResearchTests(CodeLedCase):
    def run_source(self, action, extra_files=None, **settings):
        business = Business(**handlers(atomic=action))
        bundle = self.flow(business, files={PATH: SOURCE, **(extra_files or {})}, found=FOUND, text='Обработка посылок',
                           atomic_page_units=1, refine_max_requests=settings.pop('refine_max_requests', 16), **settings)
        return bundle, business

    def test_model_can_read_a_selector_then_review_it_first_without_dropping_the_original_queue(self):
        normal, state = page_reply(SPEC, path=PATH), {'read': False}
        def action(brief, attempt):
            if not state['read']:
                state['read'] = True
                unit = next(u for u in brief['source_tools']['unit_index']['units'] if u['symbol'] == 'match_policy')
                return request('read_unit', unit_id=unit['id'])
            return normal(brief, attempt)
        bundle, business = self.run_source(action)
        pages = [c for c in business.calls if c['task'] == 'TASK: map-atomic']
        self.assertEqual(pages[0]['brief']['units'][0]['symbol'], 'enter')
        self.assertEqual(pages[1]['brief']['units'][0]['symbol'], 'match_policy')
        self.assertTrue(pages[1]['brief']['source_observations'])
        self.assertEqual(pages[1]['brief']['page']['decided_earlier'], 0, 'read is not reviewed')
        states = bundle['atomic_map']['unit_states']
        self.assertEqual(len(states), 5)
        self.assertEqual({s['state'] for s in states.values()}, {'accounted'})
        self.assertEqual(len(bundle['process_map']['operations']), 4)
        observations = [r for r in bundle['atomic_map']['requests'] if 'source_observation' in r]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]['source_observation']['result']['new_lines'],
                         0, 'the selector was already supplied by bounded control closure')

    def test_ninth_distinct_observation_gets_one_repair_rather_than_halting_the_map(self):
        state = {'count': 0}; normal = page_reply(SPEC, path=PATH)
        def action(brief, attempt):
            if state['count'] < 9 and attempt == 1:
                state['count'] += 1
                return request('search', query='lookup-' + str(state['count']))
            return normal(brief, attempt)
        bundle, business = self.run_source(action, refine_max_requests=20)
        repairs = [c['brief'] for c in business.calls if 'validation_feedback' in c['brief']]
        self.assertTrue(repairs, 'the ninth observation must receive precise repair feedback')
        self.assertEqual(repairs[0]['validation_feedback']['errors'][0]['code'], 'source_observation_limit')
        self.assertEqual({s['state'] for s in bundle['atomic_map']['unit_states'].values()}, {'accounted'})
        self.assertFalse(any('halted' in reason for reason in bundle['atomic_map']['status_reasons']))

    def test_repeated_read_via_alias_or_different_unused_paging_is_not_another_observation(self):
        normal = page_reply(SPEC, path=PATH); state = {'phase': 0}
        def action(brief, attempt):
            if state['phase'] == 0:
                state['phase'] = 1
                unit = next(u for u in brief['source_tools']['unit_index']['units'] if u['symbol'] == 'match_policy')
                return request('read_unit', unit_id=unit['id'])
            if state['phase'] == 1 and attempt == 1:
                state['phase'] = 2
                return request('read', path=PATH, start_line=DECLS['match_policy'][0],
                               end_line=DECLS['match_policy'][1], offset=1, limit=1)
            return normal(brief, attempt)
        bundle, business = self.run_source(action)
        reads = [r for r in bundle['atomic_map']['requests'] if 'source_observation' in r]
        self.assertEqual(len(reads), 1)
        feedback = next(c['brief']['validation_feedback'] for c in business.calls if 'validation_feedback' in c['brief'])
        self.assertEqual(feedback['errors'][0]['code'], 'source_already_supplied')

    def test_backslash_input_remains_refused_by_the_existing_manifest_boundary(self):
        from process_observability.manifest import ManifestError
        with self.assertRaises(ManifestError):
            self.run_source(page_reply(SPEC, path=PATH), extra_files={
                'pkg/odd\\name.py': GUARD + '\n\ndef extra_action():\n    return 1\n'})


    def test_large_neighbourhood_is_removed_before_the_leading_whole_body_is_reduced_to_quotes(self):
        text = GUARD + '\n\ndef enter():\n' + ''.join(f'    x{i} = {i}\n' for i in range(25))
        text += '    return selector()\n\ndef selector():\n    return dispatch()\n\ndef dispatch():\n'
        text += '    return [' + ', '.join(f'part_{i}()' for i in range(25)) + ']\n'
        text += ''.join(f'\ndef part_{i}():\n    # ' + 'context' * 300 + f'\n    return {i}\n' for i in range(25))
        found = [finding('entry_point', 'Принять обращение', PATH, 4, 4, 'enter')]
        normal = page_reply({'enter': SPEC['enter']}, path=PATH)
        def run(**options):
            business = Business(**handlers(atomic=normal))
            bundle = self.flow(business, files={PATH: text}, found=found, text='', atomic_page_units=1, **options)
            return bundle, business
        with patch.object(direct_atomic, 'CONTROL_CONTEXT_DEPTH', 0):
            baseline, _ = run()
        initial = next(q['chars'] for q in baseline['atomic_map']['requests'] if q['kind'] == 'atomic')
        bundle, business = run(business_request_chars=initial + 15000)
        brief = next(c['brief'] for c in business.calls if c['task'] == 'TASK: map-atomic')
        self.assertEqual(brief['units'][0]['symbol'], 'enter')
        self.assertEqual(brief['units'][0]['declaration_supplied'], 'whole')
        self.assertTrue(any(row['reason'] == 'control_context_budget' for row in brief['units'][0]['context_omitted']))
        self.assertIsNotNone(covering_ranges(brief['excerpts'], PATH, 4, 30))

    def test_a_second_invalid_observation_fails_only_that_fragment_and_other_units_continue(self):
        state = {'count': 0, 'extra': False}; normal = page_reply(SPEC, path=PATH)
        def action(brief, attempt):
            if state['count'] < 9 and attempt == 1:
                state['count'] += 1
                return request('search', query='look-' + str(state['count']))
            if attempt == 2 and not state['extra']:
                state['extra'] = True
                return request('search', query='still-an-observation')
            return normal(brief, attempt)
        bundle, business = self.run_source(action, refine_max_requests=20)
        states = {s['state'] for s in bundle['atomic_map']['unit_states'].values()}
        self.assertTrue({'accounted', 'request_failed'} <= states)
        self.assertFalse(any('halted' in reason for reason in bundle['atomic_map']['status_reasons']))

    def test_a_foreign_transition_withdrawal_gets_one_precise_repair_without_removing_any_edge(self):
        normal = page_reply(SPEC, path=PATH); state = {'done': False}
        def action(brief, attempt):
            reply = normal(brief, attempt)
            if not state['done']:
                if attempt == 1:
                    reply['transition_review'] = {'withdrawals': [{'transition_id': 'foreign', 'reason': 'Wrong route',
                        'refs': [{'path': PATH, 'start_line': 4, 'end_line': 5}]}]}
                else: state['done'] = True
            return reply
        bundle, business = self.run_source(action)
        feedback = next(c['brief']['validation_feedback'] for c in business.calls if 'validation_feedback' in c['brief'])
        self.assertEqual(feedback['errors'][0]['code'], 'source_invalid_transition_review')
        self.assertFalse(bundle['atomic_map']['code_led']['transition_support'].get('reviews'))

    def test_export_refuses_reviews_on_observation_only_requests_and_on_an_undeclared_policy(self):
        spec = {**SPEC, 'enter': [SPEC['enter'][0], ('Одобрить без проверки', 'operation', 'Приём', 'continues', None)]}
        edge = ('enter', 0, 'enter', 1, 'source_condition', [(4, 5)], None)
        normal = page_reply(spec, [edge], path=PATH); state = {'phase': 0}
        def action(brief, attempt):
            if state['phase'] == 1:
                state['phase'] = 2
                return request('search', query='parcel.valid')
            reply = normal(brief, attempt)
            if state['phase'] == 2:
                state['phase'] = 3
                reply['transition_review'] = {'withdrawals': [{
                    'transition_id': brief['current_map']['transitions'][0]['id'], 'reason': 'Result is conditional',
                    'refs': [{'path': PATH, 'start_line': DECLS['match_policy'][0],
                              'end_line': DECLS['match_policy'][1]}]}]}
            elif state['phase'] == 0: state['phase'] = 1
            return reply
        bundle, _ = self.run_source(action)
        record = bundle['atomic_map']; observed = next(q for q in record['requests'] if 'source_observation' in q)
        bad = copy.deepcopy(bundle)
        review = bad['atomic_map']['code_led']['transition_support']['reviews'][0]
        review.update(request_id=observed['id'], page=observed['page'])
        with self.assertRaises(ExportError):
            _atomic_accounting(bad, ProcessMap.from_dict(bad['process_map']), record['catalog']['snapshot_id'])
        bad = copy.deepcopy(bundle)
        del bad['atomic_map']['settings']['source_investigation']
        # Remove observations too, so this negative specifically exercises unauthorized source reviews.
        for q in bad['atomic_map']['requests']: q.pop('source_observation', None)
        with self.assertRaises(ExportError):
            _atomic_accounting(bad, ProcessMap.from_dict(bad['process_map']), record['catalog']['snapshot_id'])
        _, exported = self.exported(copy.deepcopy(bundle), 'source-quote-free')
        observations = [q['source_observation'] for q in exported['atomic_accounting']['requests']
                        if 'source_observation' in q]
        self.assertNotIn('text', observations[0]['result']['matches'][0])
        self.assertEqual(exported['instrumentation_accounting'], bundle['instrumentation_accounting'])
        self.assertIn('not this quote-free copy', exported['notes']['atomic_observations'])

    def test_a_run_with_only_an_observation_and_no_semantic_decisions_is_failed(self):
        state = {'first': True}
        def action(brief, attempt):
            if state['first']:
                state['first'] = False
                return request('search', query='parcel.valid')
            return {'invalid': 'not a semantic reply'}
        bundle, _ = self.run_source(action, refine_max_requests=3)
        record = bundle['atomic_map']
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['summary']['decisions'], 0)
        self.assertEqual(record['operations'], [])
        self.assertEqual(len([r for r in record['requests'] if 'source_observation' in r]), 1)

    def test_fact_accounting_in_a_coarse_or_legacy_bundle_is_validated_too(self):
        bundle, _ = self.run_source(page_reply(SPEC, path=PATH))
        legacy = copy.deepcopy(bundle); legacy.pop('atomic_map')
        _atomic_accounting(legacy, ProcessMap.from_dict(legacy['process_map']), 'unused-without-atomic')
        legacy['instrumentation_accounting']['unaddressed_fact_ids'] = []
        with self.assertRaises(ExportError):
            _atomic_accounting(legacy, ProcessMap.from_dict(legacy['process_map']), 'unused-without-atomic')

    def test_foreign_read_gets_one_precise_repair_and_never_opens_the_path(self):
        normal, state = page_reply(SPEC, path=PATH), {'done': False}
        def action(brief, attempt):
            if state['done']:
                return normal(brief, attempt)
            if attempt == 1:
                return request('read', path='/etc/passwd', start_line=1, end_line=1)
            state['done'] = True
            unit = next(u for u in brief['source_tools']['unit_index']['units'] if u['symbol'] == 'match_policy')
            return request('read_unit', unit_id=unit['id'])
        bundle, business = self.run_source(action)
        pages = [c for c in business.calls if c['task'] == 'TASK: map-atomic']
        error = pages[1]['brief']['validation_feedback']['errors'][0]
        self.assertEqual(error['code'], 'source_outside_frozen_snapshot')
        observation = next(r for r in bundle['atomic_map']['requests'] if 'source_observation' in r)
        self.assertEqual(len(observation['attempts']), 2)
        self.assertEqual(observation['outcome'], 'accepted_after_repair')
        self.assertNotIn('/etc/passwd', str(observation['source_observation']['result']))

    def test_search_is_navigation_and_does_not_authorize_unread_definition(self):
        normal, state = page_reply(SPEC, path=PATH), {'done': False}
        def action(brief, attempt):
            if not state['done']:
                state['done'] = True
                return request('search', query='unrelated_marker')
            return normal(brief, attempt)
        unrelated = 'app/unrelated.py'
        bundle, business = self.run_source(action, extra_files={
            unrelated: GUARD + '\n\ndef unused():\n    return "unrelated_marker"\n'})
        pages = [c['brief'] for c in business.calls if c['task'] == 'TASK: map-atomic']
        self.assertEqual(pages[1]['source_observations'][0]['result']['total'], 1)
        self.assertIsNone(covering_ranges(pages[1]['excerpts'], unrelated, 5, 5))
        self.assertEqual(pages[1]['page']['decided_earlier'], 0)

    def test_last_planned_atomic_request_repairs_to_a_decision_instead_of_spending_it_on_unusable_read(self):
        normal = page_reply(SPEC, path=PATH)
        def action(brief, attempt):
            if attempt == 1:
                unit = next(u for u in brief['source_tools']['unit_index']['units'] if u['symbol'] == 'match_policy')
                return request('read_unit', unit_id=unit['id'])
            return normal(brief, attempt)
        bundle, business = self.run_source(action, refine_max_requests=2)
        requests = [r for r in bundle['atomic_map']['requests'] if r['kind'] == 'atomic']
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['outcome'], 'accepted_after_repair')
        self.assertFalse(any('source_observation' in r for r in requests))
        states = bundle['atomic_map']['unit_states']
        self.assertTrue(any(s['state'] == 'accounted' for s in states.values()))
        self.assertTrue({s['state'] for s in states.values()} <= {'accounted', 'deferred'})
        feedback = next(c['brief']['validation_feedback'] for c in business.calls if 'validation_feedback' in c['brief'])
        self.assertEqual(feedback['errors'][0]['code'], 'source_observation_not_deliverable')

    def test_an_observation_must_fit_the_next_whole_model_request_before_its_ranges_are_published(self):
        normal = page_reply(SPEC, path=PATH)
        baseline, _ = self.run_source(normal)
        initial = next(r['chars'] for r in baseline['atomic_map']['requests'] if r['kind'] == 'atomic')
        state = {'done': False}
        def action(brief, attempt):
            if state['done']:
                return normal(brief, attempt)
            if attempt == 1:
                return request('read', path='app/catalog.md', start_line=1, end_line=1)
            state['done'] = True
            return normal(brief, attempt)
        bundle, business = self.run_source(action, business_request_chars=initial + 5000,
                                           extra_files={'app/catalog.md': '# ' + 'x' * 25000 + '\n'})
        repair = next(c['brief'] for c in business.calls if 'validation_feedback' in c['brief'])
        self.assertEqual(repair['validation_feedback']['errors'][0]['code'],
                         'source_observation_exceeds_request_budget')
        self.assertFalse(any('source_observation' in r for r in bundle['atomic_map']['requests']))
        self.assertFalse(any(e['path'] == 'app/catalog.md' for e in repair['excerpts']))
        states = bundle['atomic_map']['unit_states']
        self.assertEqual(next(s['state'] for s in states.values() if s['symbol'] == 'enter'), 'accounted')
        self.assertTrue({s['state'] for s in states.values()} <= {'accounted', 'deferred'})

    def test_current_map_can_be_inspected_after_local_progress_without_recreating_its_actions(self):
        normal, state = page_reply(SPEC, path=PATH), {'phase': 0}
        def action(brief, attempt):
            if state['phase'] == 0:
                state['phase'] = 1
                unit = next(u for u in brief['source_tools']['unit_index']['units'] if u['symbol'] == 'match_policy')
                return request('read_unit', unit_id=unit['id'])
            if state['phase'] == 1:
                state['phase'] = 2
                return normal(brief, attempt)
            if state['phase'] == 2:
                state['phase'] = 3
                return request('inspect_map', section='operations')
            return normal(brief, attempt)
        bundle, business = self.run_source(action)
        reads = [r['source_observation'] for r in bundle['atomic_map']['requests'] if 'source_observation' in r]
        self.assertEqual([r['request']['tool'] for r in reads], ['read_unit', 'inspect_map'])
        registry = reads[1]['result']
        self.assertEqual(registry['total'], 3)
        final = {op['id'] for op in bundle['process_map']['operations']}
        self.assertTrue({op['id'] for op in registry['rows']} <= final)
        self.assertEqual(len(final), 4)

    def test_source_observation_replay_preserves_the_same_context_without_new_atomic_calls(self):
        state = {"done": False}; normal = page_reply(SPEC, path=PATH)
        def action(brief, attempt):
            if not state["done"]:
                state["done"] = True
                return request('search', query='parcel.valid')
            return normal(brief, attempt)
        first, _ = self.run_source(action)
        original = self.out
        again, business = self.run_source(normal, resume_from=original)
        self.assertNotIn('TASK: map-atomic', business.tasks())
        first_observation = next(r['source_observation'] for r in first['atomic_map']['requests']
                                 if 'source_observation' in r)
        again_observation = next(r['source_observation'] for r in again['atomic_map']['requests']
                                 if 'source_observation' in r)
        self.assertEqual(again_observation, first_observation)
        self.assertEqual(again['process_map']['operations'], first['process_map']['operations'])

    def test_source_observation_export_checks_hash_snapshot_navigation_authority_and_saved_units(self):
        state = {"done": False}; normal = page_reply(SPEC, path=PATH)
        def action(brief, attempt):
            if not state["done"]:
                state["done"] = True
                unit = next(u for u in brief['source_tools']['unit_index']['units'] if u['symbol'] == 'match_policy')
                return request('read_unit', unit_id=unit['id'])
            return normal(brief, attempt)
        bundle, _ = self.run_source(action)
        def check(candidate):
            record = candidate['atomic_map']
            return _atomic_accounting(candidate, ProcessMap.from_dict(candidate['process_map']),
                                      record['catalog']['snapshot_id'])
        check(bundle)
        _, exported = self.exported(copy.deepcopy(bundle), 'source-positive')
        saved = next(q['source_observation'] for q in exported['atomic_accounting']['requests'] if 'source_observation' in q)
        self.assertNotIn('text', saved['result'])
        self.assertIn('text_sha256', saved['result'])
        def observation(candidate):
            return next(r['source_observation'] for r in candidate['atomic_map']['requests']
                        if 'source_observation' in r)
        for corruption in ('hash', 'snapshot', 'unit', 'source_only_decision'):
            with self.subTest(corruption=corruption):
                bad = copy.deepcopy(bundle); item = observation(bad)
                if corruption == 'hash': item['result']['text'] += ' FORGED'
                elif corruption == 'snapshot':
                    item['result']['snapshot_id'] = 'foreign'; item['result_sha256'] = _digest(item['result'])
                elif corruption == 'unit': item['unit_id'] = 'foreign'
                else:
                    entry = next(r for r in bad['atomic_map']['requests'] if 'source_observation' in r)
                    next(iter(bad['atomic_map']['unit_states'].values()))['request_id'] = entry['id']
                with self.assertRaises(ExportError): check(bad)

    def test_unaddressed_instrumentation_facts_remain_visible_in_html_and_export_and_cannot_be_hidden(self):
        from process_observability.report import build_html
        bundle, _ = self.run_source(page_reply(SPEC, path=PATH))
        rows = bundle['instrumentation_accounting']
        self.assertEqual(len(rows['unaddressed_fact_ids']), 4)
        html = build_html(bundle)
        self.assertIn('Доработки по этим фактам ещё не определены', html)
        self.assertIn('отсутствие записи не доказывает ошибку операции', html)
        for corruption in ('omit', 'clear'):
            bad = copy.deepcopy(bundle)
            if corruption == 'omit': del bad['instrumentation_accounting']
            else: bad['instrumentation_accounting']['unaddressed_fact_ids'] = []
            with self.assertRaises(ExportError):
                _atomic_accounting(bad, ProcessMap.from_dict(bad['process_map']),
                                   bad['atomic_map']['catalog']['snapshot_id'])


    def test_later_source_can_withdraw_a_structurally_eligible_wrong_condition_without_erasing_history(self):
        spec = {**SPEC, 'enter': [SPEC['enter'][0],
                                 ('Одобрить без проверки', 'operation', 'Приём', 'continues', None)]}
        false_edge = ('enter', 0, 'enter', 1, 'source_condition', [(DECLS['enter'][0], DECLS['enter'][1])], None)
        normal = page_reply(spec, [false_edge], path=PATH)
        state = {'withdrawn': False}
        def action(brief, attempt):
            reply = normal(brief, attempt)
            prior = brief['current_map']['transitions']
            if prior and not state['withdrawn']:
                state['withdrawn'] = True
                reply['transition_review'] = {'withdrawals': [{
                    'transition_id': prior[0]['id'],
                    'reason': 'Приём не означает одобрение: результат зависит от parcel.valid в вызываемом коде.',
                    'refs': [{'path': PATH, 'start_line': DECLS['match_policy'][0],
                              'end_line': DECLS['match_policy'][1]}]}]}
            return reply
        bundle, _ = self.run_source(action)
        support = bundle['atomic_map']['code_led']['transition_support']
        self.assertEqual(len(support['reviews']), 1)
        original = support['proposals'][0]
        self.assertEqual(original['state'], 'active', 'original structural proposal retained, not rewritten')
        self.assertFalse(any(e['id'] == original['id'] for e in bundle['atomic_map']['transitions']))
        self.assertTrue(any(g['kind'] == 'unsupported_transition' for g in bundle['atomic_map']['code_led']['gaps']))
        self.exported(copy.deepcopy(bundle), 'withdrawal-positive')
        bad = copy.deepcopy(bundle)
        bad['atomic_map']['code_led']['transition_support']['reviews'][0]['request_id'] = 'foreign'
        with self.assertRaises(ExportError):
            _atomic_accounting(bad, ProcessMap.from_dict(bad['process_map']),
                               bad['atomic_map']['catalog']['snapshot_id'])


if __name__ == '__main__':
    unittest.main()
