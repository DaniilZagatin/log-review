"""Known source gaps become proposals; unresolved inputs are never silently promoted to coverage."""
from copy import deepcopy
from pathlib import Path
import tempfile, unittest

from process_observability.logging_review import review_logging, ReviewOptions, SYSTEM
from process_observability.map_input import MapView
from process_observability.source_tools import snapshot_repository
from process_observability.operation_coverage import validate_saved_rows
from test_map_first_logging import operation, model_reply, Scripted

CODE = """import logging
logger = logging.getLogger(__name__)
def check(item):
    logger.info('checking')
    if item is None:
        logger.info('absent')
        return False
    return True
"""

class KnownGapControls(unittest.TestCase):
    def run_review(self, callback, **kwargs):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root/'app.py').write_text(CODE)
            view = MapView.from_dict({'schema_version':'2.0', 'scenario':{}, 'sources':[{'id':'source-1'}],
                'nodes':[operation(CODE, first=3, last=8, outcome='unknown')], 'edges':[],
                'run':{'id':'synthetic', 'status':'completed'}})
            model = Scripted(callback)
            result = review_logging(view, snapshot_repository(root, max_files=None, max_chars=None), model, **kwargs)
            validate_saved_rows(view, result)
            return model, result

    def partial(self, packet):
        q = model_reply(packet, key='checking', role='start', establishes=['occurrence'])
        q['bindings'][0]['moment'] = 'before_action'
        q['bindings'][0]['covers_outcomes'] = []  # Intent does not claim the operation's unknown outcome.
        q['missing'] = ['Положительный результат проверки не записывается']
        q['changes'] = [{'id':'result', 'kind':'add_event', 'operation_ids':[packet['operation']['id']],
            'problem':'Не записывается положительный результат', 'what':'Добавить запись перед return True',
            'why':'Отличать успешную проверку от её начала', 'fields':[],
            'location':{'path':'app.py','start_line':8,'end_line':8}, 'timing':'После проверки, перед return True',
            'placement':'before','guards':[{'condition':{'path':'app.py','start_line':5,'end_line':5},'branch':'false'}],
            'verification':'При непустом входе появляется запись результата, при пустом — отсутствия',
            'existing_site_ids':[q['bindings'][0]['site_id']], 'dependencies':[]}]
        return q

    def test_unaddressed_source_question_gets_one_clear_repair_with_its_old_json(self):
        def answer(p, n):
            q = self.partial(p)
            if n == 1:
                q['review_tasks'] = [{'question':'Подтверждает ли отрицательная ветка положительный результат?', 'scope':'source'}]
            return q
        model, result = self.run_review(answer, options=ReviewOptions(stop_on_failed_repair=True))
        self.assertEqual(len(model.calls), 2)
        self.assertIn('source_basis', model.calls[1]['repair_errors'][0])
        self.assertTrue(model.calls[1]['rejected_response']['reply']['review_tasks'])
        self.assertEqual(result['operations'][0]['state'], 'P')
        self.assertEqual(result['operations'][0]['review_tasks'], [])
        self.assertTrue(result['operations'][0]['changes'])

    def test_unaddressed_question_is_not_silently_discarded_after_failed_repair(self):
        def answer(p, n):
            q = self.partial(p); q['review_tasks'] = [{'question':'Требуется ли запись результата?', 'scope':'source'}]
            return q
        model, result = self.run_review(answer, options=ReviewOptions(stop_on_failed_repair=True))
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result['stop_reason']['kind'], 'repair_failed')
        self.assertEqual(result['operations'][0]['state'], 'X')

    def test_known_absence_remains_partial_with_proposal_and_no_source_question(self):
        model, result = self.run_review(lambda p,n: self.partial(p))
        row = result['operations'][0]
        self.assertEqual(len(model.calls), 1)
        self.assertEqual((row['state'], row['missing_status']), ('P', 'established'))
        self.assertTrue(row['search']['complete'])
        self.assertEqual(result['summary']['counts']['C'], 0)

    def test_real_missing_input_still_blocks_even_when_static_search_is_complete(self):
        def answer(p,n):
            q = self.partial(p)
            q['review_tasks'] = [{'question':'Как реализован недоступный адаптер?', 'scope':'source',
                'source_basis':{'kind':'code_unavailable','detail':'Реализация внешнего адаптера отсутствует в допущенных исходниках'}}]
            return q
        model,result = self.run_review(answer)
        row = result['operations'][0]
        self.assertEqual(len(model.calls), 1)
        self.assertEqual((row['state'],row['missing_status']), ('X','hypothesis'))
        self.assertEqual(row['review_tasks'][0]['source_basis']['kind'], 'code_unavailable')
        self.assertTrue(row['review_tasks'][0]['blocking'])

    def test_invented_or_empty_basis_gets_no_automatic_downgrade(self):
        for basis in ({'kind':'known_missing','detail':'Лог отсутствует'}, {'kind':'code_unavailable','detail':''}):
            with self.subTest(basis=basis):
                def answer(p,n):
                    q=self.partial(p); q['review_tasks']=[{'question':'Вопрос','scope':'source','source_basis':basis}]; return q
                model,result=self.run_review(answer,options=ReviewOptions(stop_on_failed_repair=True))
                self.assertEqual(len(model.calls),2); self.assertEqual(result['stop_reason']['kind'],'repair_failed')
                self.assertEqual(result['operations'][0]['state'],'X')

    def test_map_runtime_and_legacy_questions_keep_their_scope(self):
        for task, state in (({'question':'Граница операции противоречит коду','scope':'map'},'X'),
                            ({'question':'Включён ли уровень в проде','scope':'runtime'},'P'),
                            ('Неизвестный старый вопрос','X')):
            with self.subTest(task=task):
                def answer(p,n):q=self.partial(p); q['review_tasks']=[task]; return q
                model,result=self.run_review(answer)
                self.assertEqual(result['operations'][0]['state'],state)

    def test_primary_example_defaults_to_no_question_and_explains_known_absence(self):
        self.assertIn('"review_tasks":[]', SYSTEM)
        self.assertIn('source_basis', SYSTEM)
        self.assertIn('Отрицательная ветка', SYSTEM)

    def test_fresh_string_and_unaddressed_map_cannot_bypass_single_repair(self):
        for task in ('Нужна ли запись результата?', {'question':'Отказ подтверждает успех?', 'scope':'map'}):
            with self.subTest(task=task):
                def answer(p,n):q=self.partial(p); q['review_tasks']=[task]; return q
                model,result=self.run_review(answer,options=ReviewOptions(stop_on_failed_repair=True))
                self.assertEqual(len(model.calls),2); self.assertEqual(result['stop_reason']['kind'],'repair_failed')
                self.assertEqual(result['operations'][0]['state'],'X')

    def test_real_map_conflict_stays_blocking_and_null_unused_basis_is_harmless(self):
        def answer(p,n):
            q=self.partial(p); q['review_tasks']=[{'question':'Граница ветвей не определена', 'scope':'map',
                'map_basis':'В карте одна операция имеет противоречащие друг другу границы двух ветвей', 'source_basis':None}]; return q
        model,result=self.run_review(answer)
        self.assertEqual(len(model.calls),1); self.assertEqual(result['operations'][0]['state'],'X')
        self.assertTrue(result['operations'][0]['review_tasks'][0]['map_basis'])
        def runtime(p,n):q=self.partial(p); q['review_tasks']=[{'question':'Включён ли уровень?', 'scope':'runtime','source_basis':None,'map_basis':None}]; return q
        model,result=self.run_review(runtime); self.assertEqual(len(model.calls),1); self.assertEqual(result['operations'][0]['state'],'P')

    def test_all_question_errors_are_in_repair_even_if_binding_failed_first(self):
        def answer(p,n):
            q=self.partial(p)
            if n==1:
                q['bindings'][0]['site_id']='unknown'
                q['review_tasks']=[{'question':'Вопрос без источника','scope':'source'}, 'Другой вопрос',
                                   {'question':'Вопрос без конфликта','scope':'map'}]
            return q
        model,result=self.run_review(answer,options=ReviewOptions(stop_on_failed_repair=True))
        issues=[x for x in model.calls[1]['repair_details']['issues'] if x['code']=='review_question_basis_required']
        self.assertEqual([x['field'] for x in issues],['review_tasks[0]','review_tasks[1]','review_tasks[2]'])
        self.assertTrue(all(x['message'].startswith(x['field']) for x in issues))
        self.assertEqual(result['operations'][0]['state'],'P'); self.assertEqual(len(model.calls),2)

if __name__ == '__main__': unittest.main()
