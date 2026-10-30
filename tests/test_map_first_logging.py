"""Independent source/meaning controls for the new map consumer (no network)."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from process_observability.map_input import MapView
from process_observability.source_tools import snapshot_repository
from process_observability.logging_review import ReviewOptions, review_logging

CODE = """import logging
logger = logging.getLogger(__name__)
def save_request(item):
    persist(item)
    logger.info('done', extra={'id': item.id})
def refuse(item):
    logger.error('denied')
def other(item):
    logger.info('done')
def no_logging(item):
    return item
"""


def operation(code=CODE, *, identifier='save', first=3, last=5, effect='result', outcome='success'):
    return {'id': identifier, 'kind': 'operation', 'name': identifier,
            'description': 'Самостоятельное действие процесса', 'input': 'Обращение',
            'output': 'Результат самостоятельного действия', 'effect_kind': effect, 'outcome': outcome,
            'evidence': [{'path': 'source-1/app.py', 'line_start': first, 'line_end': last,
                          'quote': '\n'.join(code.splitlines()[first-1:last])}]}


def model_reply(packet, *, key='done', role='result', establishes=None, changes=None):
    anchor = packet['anchors'][0]
    site = next((s for s in packet['candidates'] if s['event_key'] == key
                 and anchor['start_line'] <= s['start_line'] <= anchor['end_line']), None)
    bindings = [] if site is None else [
        {'site_id': site['id'], 'role': role, 'meaning': 'Подтверждает действие этой операции',
         'conditions': 'В этой ветке обработчика', 'correlation': 'По месту вызова и контексту операции',
         'establishes': establishes or ['occurrence', 'result'],
         'evidence': [{'path': site['path'], 'start_line': site['start_line'], 'end_line': site['end_line']}],
         'covers_outcomes': [packet['operation']['outcome']], 'invocation_ref': None}]
    # Synthetic answers must name their intended gap; the host must not invent one for them.
    missing = []
    if not bindings:
        missing = ['Подходящая запись в проверенном коде не найдена']
    elif role in ('start', 'request'):
        missing = ['Начало или запрос не подтверждают результат действия']
    elif role in ('error', 'repeat', 'no_op') and packet['operation'].get('outcome') == 'success':
        missing = ['Запись иного исхода не подтверждает новый успешный результат']
    elif establishes is not None and 'occurrence' not in establishes:
        missing = ['Результат не связан с отдельным выполнением']
    elif establishes is not None and 'result' not in establishes:
        missing = ['Результат действия не подтверждён']
    return {'action': 'review', 'operation_id': packet['operation']['id'], 'bindings': bindings,
            'missing': missing, 'review_tasks': [], 'limitations': [], 'changes': changes or []}


class Scripted:
    def __init__(self, callback): self.callback, self.calls = callback, []
    def complete_json(self, system, user):
        self.calls.append(json.loads(user))
        return self.callback(json.loads(user)['packet'], len(self.calls))


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'app.py').write_text(CODE)

    def run_review(self, model, operations=None, code=CODE, **kwargs):
        (self.root/'app.py').write_text(code)
        view = MapView.from_dict({'schema_version': '2.0', 'scenario': {'title': 'Тест'}, 'sources': [{'id': 'source-1'}],
                                 'nodes': operations if operations is not None else [operation(code)], 'edges': [],
                                 'run': {'id': 'fixture-run', 'status': 'completed'}})
        snapshot = snapshot_repository(self.root, max_files=None, max_chars=None)
        return review_logging(view, snapshot, model, **kwargs)

    def test_usable_result_is_covered_without_requiring_new_identity_fields(self):
        result = self.run_review(Scripted(lambda p, n: model_reply(p)))
        self.assertEqual(result['summary']['counts'], {'C': 1, 'P': 0, 'U': 0, 'X': 0})
        self.assertEqual(result['model_requests'], 1)
        self.assertEqual(result['business_metrics'], 'frozen')
        self.assertEqual(len(result['inventory']['sites']), 3)
        self.assertEqual(sum(s['review_status'] == 'unmapped' for s in result['inventory']['sites']), 2)

    def test_start_or_exception_never_establishes_a_fresh_success(self):
        for role in ('start', 'request', 'error', 'repeat', 'no_op'):
            with self.subTest(role=role):
                result = self.run_review(Scripted(lambda p, n: model_reply(p, role=role)))
                self.assertEqual(result['operations'][0]['state'], 'P')

    def test_error_log_can_cover_its_own_refusal_operation(self):
        result = self.run_review(Scripted(lambda p, n: model_reply(p, key='denied', role='error')),
                                 [operation(identifier='refusal', first=6, last=7, effect='decision', outcome='refusal')])
        self.assertEqual(result['operations'][0]['state'], 'C')

    def test_repeat_can_cover_its_own_operation_without_proving_a_new_result(self):
        result = self.run_review(Scripted(lambda p, n: model_reply(p, role='repeat')),
                                 [operation(effect='decision', outcome='no_decision')])
        self.assertEqual(result['operations'][0]['state'], 'C')

    def test_same_key_at_a_foreign_site_is_not_automatic_coverage(self):
        def callback(packet, n):
            if n == 1: return {'action': 'inspect', 'reads': [{'path': 'app.py', 'start_line': 8, 'end_line': 9}]}
            value = model_reply(packet)
            foreign = next(s for s in packet['candidates'] if s['start_line'] == 9)
            value['bindings'][0].update(site_id=foreign['id'], evidence=[
                {'path': foreign['path'], 'start_line': 9, 'end_line': 9}])
            return value
        model = Scripted(callback)
        result = self.run_review(model)
        self.assertEqual(result['operations'][0]['state'], 'X')
        self.assertEqual(len(model.calls), 3)
        self.assertIn('invocation', model.calls[2]['repair_errors'][0])

    def test_foreign_operation_id_remains_invalid_after_one_repair(self):
        def callback(packet, n):
            value = model_reply(packet); value['operation_id'] = 'close-but-foreign'; return value
        model = Scripted(callback); result = self.run_review(model)
        self.assertEqual(result['operations'][0]['state'], 'X')
        self.assertEqual(result['summary']['denominator'], 1)
        self.assertEqual(len(model.calls), 2)

    def test_only_auxiliary_guidance_is_dropped_after_real_repair(self):
        def callback(packet, n):
            value = model_reply(packet); value['guidance_ids'] = ['not-a-rule']; return value
        model = Scripted(callback); result = self.run_review(model)
        self.assertEqual(result['operations'][0]['state'], 'C')
        self.assertEqual(len(model.calls), 2)
        self.assertTrue(result['operations'][0]['warnings'])
        self.assertEqual(result['checkpoints'][0]['accepted_attempt'], 'repair')

    def test_provider_failure_is_not_retried_as_a_schema_error(self):
        from process_observability.llm import LLMError
        def callback(packet, n): raise LLMError('Fixture unavailable', category='http', status=429)
        model = Scripted(callback)
        result = self.run_review(model, [operation(identifier='a'), operation(identifier='b')],
                                 options=ReviewOptions(max_requests=1))
        self.assertEqual(result['summary']['counts']['X'], 2)
        self.assertEqual(len(model.calls), 1)

    def test_no_model_and_input_budget_limits_leave_operations_unknown(self):
        for model, options in ((None, ReviewOptions()), (Scripted(lambda p, n: model_reply(p)), ReviewOptions(request_chars=100))):
            result = self.run_review(model, options=options)
            self.assertEqual(result['operations'][0]['state'], 'X')
            self.assertEqual(result['model_requests'], 0)

    def test_missing_log_is_distinct_from_missing_review(self):
        result = self.run_review(Scripted(lambda p, n: model_reply(p, key='absent')),
                                 [operation(identifier='unlogged', first=10, last=11, effect='unknown', outcome='unknown')])
        self.assertEqual(result['operations'][0]['state'], 'U')
        self.assertTrue(result['operations'][0]['review_tasks'])

    def test_one_bad_map_quote_does_not_erase_another_operations_result(self):
        bad = operation(identifier='bad'); bad['evidence'][0]['quote'] = 'fabricated'
        result = self.run_review(Scripted(lambda p, n: model_reply(p)), [bad, operation(identifier='good')])
        self.assertEqual([r['state'] for r in result['operations']], ['X', 'C'])

    def test_exact_replay_is_revalidated_without_api_and_changed_code_is_not_reused(self):
        model = Scripted(lambda p, n: model_reply(p)); first = self.run_review(model, model_identity={'model': 'fixture'})
        never = Scripted(lambda p, n: self.fail('Compatible decision should not be re-asked'))
        again = self.run_review(never, resume=first, model_identity={'model': 'fixture'})
        self.assertEqual(first['operations'], again['operations'])
        self.assertEqual(again['model_requests'], 0)
        changed = CODE.replace("logger.info('done',", "logger.info('changed',")
        third = self.run_review(Scripted(lambda p, n: model_reply(p, key='changed')), code=changed, resume=first,
                                model_identity={'model': 'fixture'})
        self.assertEqual(third['model_requests'], 1)

    def test_full_inventory_does_not_stop_at_300_sites(self):
        code = "import logging\nlogger = logging.getLogger(__name__)\n" + '\n'.join(f"logger.info('e{i}')" for i in range(350))
        result = self.run_review(None, [], code=code)
        self.assertEqual(len(result['inventory']['sites']), 350)
        self.assertEqual(result['summary']['denominator'], 0)
        self.assertIsNone(result['summary']['uncovered']['lower'])

    def test_detector_keeps_two_same_key_calls_on_one_line_distinct(self):
        code = "import logging\nlogger = logging.getLogger(__name__)\nlogger.info('x'); logger.info('x')\n"
        result = self.run_review(None, [], code=code)
        sites = result['inventory']['sites']
        self.assertEqual(len({s['id'] for s in sites}), 2)
        self.assertNotEqual(sites[0]['column'], sites[1]['column'])


if __name__ == '__main__': unittest.main()
