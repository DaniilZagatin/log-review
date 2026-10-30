"""Delivered neighboring evidence must be assessed, without being forced into coverage."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from process_observability.logging_contracts import LoggingReviewError
from process_observability.logging_review import _Navigator, ReviewOptions, _validate_reply, _repair_details
from process_observability.log_discovery import discover_logging
from process_observability.map_input import MapView, RepoContext
from process_observability.source_tools import snapshot_repository
from test_map_first_context_reuse import PYTHON, JAVA


class ContextAccountingControls(unittest.TestCase):
    def fixture(self, java=False, code=None):
        code=code or (JAVA if java else PYTHON.replace('journal','logger'))
        path='Library.java' if java else 'Clinic.py'
        first,last=(9,13) if java else (7,10)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/path).write_text(code)
            op={'id':'receive','kind':'operation','name':'Получить объект','outcome':'unknown',
                'evidence':[{'path':'source-1/'+path,'line_start':first,'line_end':last,
                             'quote':'\n'.join(code.splitlines()[first-1:last])}]}
            view=MapView.from_dict({'schema_version':'2.0','sources':[{'id':'source-1'}],'scenario':{},
                'nodes':[op],'edges':[],'run':{'id':'synthetic','status':'completed'}})
            snap=snapshot_repository(root,max_files=None,max_chars=None,logging_calls=['journal.info'])
            ctx=RepoContext(snap,view);ctx.logging_contracts=[]
            inv=discover_logging(snap,call_names=snap.logging_calls,max_sites=None,max_per_file=None,
                                 max_hints=None,include_columns=True,exclude_declarations=True)
            packet=_Navigator(ctx,inv,ReviewOptions()).initial(view.operations[0])
            ids={i for e in packet['downstream_context']['delivered'] for i in e['candidate_site_ids']}
            reply={'action':'review','operation_id':'receive','bindings':[],'missing':['Нет записи результата'],
                   'review_tasks':[],'limitations':[],'changes':[],
                   'candidate_reviews':[{'site_id':s['id'],'classification':'technical_log',
                                         'reason':'Иной факт по прочитанному коду'}
                                        for s in packet['candidates'] if s['id'] not in ids]}
            return packet,ctx,reply,ids

    def test_python_and_java_omission_cannot_publish_absence(self):
        for java in (False,True):
            with self.subTest(java=java):
                packet,ctx,reply,ids=self.fixture(java)
                self.assertTrue(ids)
                with self.assertRaisesRegex(LoggingReviewError,'контекст'):
                    _validate_reply(reply,packet,ctx)

    def test_context_error_precedes_optional_proposal_error(self):
        packet,ctx,reply,ids=self.fixture()
        reply['changes']=[{'id':'deliberately-incomplete-proposal'}]
        with self.assertRaisesRegex(LoggingReviewError,'контекст'):
            _validate_reply(reply,packet,ctx)
        details=_repair_details(reply,packet,ctx)
        issue=next(x for x in details['issues'] if x['code']=='context_not_assessed')
        self.assertEqual({i for c in issue['calls'] for i in c['candidate_site_ids']},ids)

    def test_explicit_irrelevance_neither_forces_binding_nor_creates_C(self):
        packet,ctx,reply,ids=self.fixture()
        reply['context_reviews'] = [{'context_call_id':e['context_call_id'],'use':'not_relevant',
            'reason':'Эта запись описывает другой факт; результат текущей операции не подтверждён'} for e in packet['downstream_context']['delivered']]
        row=_validate_reply(reply,packet,ctx)
        self.assertEqual(row['state'],'U')
        self.assertEqual(row['bindings'],[])
        self.assertTrue(row['missing'])

    def test_unresolved_neighbor_cannot_become_established_absence(self):
        packet,ctx,reply,ids=self.fixture()
        reply['context_reviews'] = [{'context_call_id':e['context_call_id'],'use':'unresolved',
            'reason':'Связь записи с полученным объектом ещё не установлена'} for e in packet['downstream_context']['delivered']]
        row=_validate_reply(reply,packet,ctx)
        self.assertEqual(row['state'],'X')
        self.assertEqual(row['missing_status'],'hypothesis')

    def test_no_neighbor_does_not_add_an_obligation(self):
        code=PYTHON.replace('journal','logger').replace('return appointment_details(item.key)','return item')
        packet,ctx,reply,ids=self.fixture(code=code)
        self.assertFalse(ids)
        row=_validate_reply(reply,packet,ctx)
        self.assertEqual(row['state'],'U')

    def test_resolved_frontier_keeps_candidate_for_explicit_assessment(self):
        from process_observability.logging_context import refresh_frontier
        from types import SimpleNamespace
        entry={'reason':'context_call_limit','invocation':{'path':'x.py','start_line':3,'end_line':3},'target':{'path':'x.py','start_line':7,'end_line':9},
               'candidate_site_ids':['s']}
        packet={'downstream_context':{'delivered':[],'frontier':[entry]},'candidates':[{'id':'s'}],
                'search':{'pending':['Не завершена проверка соседнего контекста с кандидатом журнала: x.py:7 (context_call_limit)']}}
        refresh_frontier(packet,SimpleNamespace(shown=[entry['target']]))
        self.assertEqual(packet['downstream_context']['frontier'],[])
        self.assertEqual(packet['downstream_context']['delivered'][0]['candidate_site_ids'],['s'])

    def test_duplicate_unknown_and_fabricated_bound_reviews_are_rejected(self):
        packet,ctx,reply,ids=self.fixture()
        key=packet['downstream_context']['delivered'][0]['context_call_id']
        good={'context_call_id':key,'use':'not_relevant','reason':'Другой факт'}
        for reviews in ([good,good],[{**good,'context_call_id':'invented'}],[{**good,'use':'bound'}]):
            with self.subTest(reviews=reviews):
                reply['context_reviews']=reviews
                with self.assertRaises(LoggingReviewError):_validate_reply(reply,packet,ctx)

    def test_saved_context_cannot_lose_an_assessment_or_invent_a_binding(self):
        from process_observability.context_assessment import validate_saved
        packet,ctx,reply,ids=self.fixture()
        reply['context_reviews']=[{'context_call_id':e['context_call_id'],'use':'not_relevant','reason':'Другой факт'}
                                 for e in packet['downstream_context']['delivered']]
        row=_validate_reply(reply,packet,ctx)
        data={'inventory':{'sites':packet['candidates']},'source_fingerprints':ctx.snapshot.fingerprints}
        validate_saved(row,data)
        for alteration in ('omitted','bound','wrong_source','unresolved'):
            with self.subTest(alteration=alteration):
                bad=deepcopy(row)
                if alteration=='omitted':bad['context_assessment']['entries']=[]
                elif alteration=='wrong_source':bad['context_assessment']['entries'][0]['target']['file_sha256']='wrong'
                else:bad['context_assessment']['entries'][0]['use']=alteration
                with self.assertRaises(LoggingReviewError):validate_saved(bad,data)

    def test_one_call_review_can_cover_multiple_existing_sites(self):
        from process_observability.context_assessment import assess
        code=JAVA.replace('journal.info("book key {}",key);','journal.info("book key {}",key); journal.info("details requested");')
        packet,ctx,reply,ids=self.fixture(java=True,code=code)
        entry=packet['downstream_context']['delivered'][0]
        self.assertEqual(len(ids),2)
        reply['context_reviews']=[{'context_call_id':entry['context_call_id'],'use':'prior_fact_not_bound',
                                  'reason':'Найден существующий предшествующий факт; это не достаточное покрытие операции'}]
        result=assess(reply,packet,[],ctx)
        self.assertEqual(len(result['entries']),1)
        self.assertEqual(len(result['entries'][0]['candidate_site_ids']),2)

    def test_adjacency_links_before_call_and_after_previous_statement_only(self):
        from process_observability.context_assessment import adjacent_sites
        packet,ctx,reply,ids=self.fixture()
        for position,first,last,expected in [('before',11,11,ids),('after',8,10,ids),('before',7,7,set())]:
            with self.subTest(position=position,first=first):
                location=ctx.verify_ref({'path':'Clinic.py','start_line':first,'end_line':last})
                self.assertEqual(adjacent_sites({'kind':'add_event','placement':position},location,ctx,packet),expected)

    def test_bad_optional_context_proposal_still_uses_proposal_isolation(self):
        from process_observability.logging_contracts import ChangeValidationError
        packet,ctx,reply,ids=self.fixture()
        reply['context_reviews']=[{'context_call_id':e['context_call_id'],'use':'not_relevant','reason':'Другой факт'}
                                 for e in packet['downstream_context']['delivered']]
        reply['changes']=[{'id':'extra','kind':'add_event','placement':'before','existing_site_ids':[],
                          'location':{'path':'Clinic.py','start_line':11,'end_line':11}}]
        with self.assertRaises(ChangeValidationError):_validate_reply(reply,packet,ctx)
        row=_validate_reply(reply,packet,ctx,isolate_changes=True)
        self.assertEqual(row['state'],'U')
        self.assertEqual(row['changes'],[])
        self.assertEqual(len(row['change_diagnostics']),1)

    def test_newly_read_frontier_is_assessed_before_absence_validation(self):
        packet,ctx,reply,ids=self.fixture()
        entry=packet['downstream_context']['delivered'].pop()
        packet['downstream_context']['frontier']=[{**entry,'reason':'context_call_limit'}]
        with self.assertRaisesRegex(LoggingReviewError,'контекст'):_validate_reply(reply,packet,ctx)

    def test_inspect_publishes_promoted_context_ID_before_next_reply(self):
        from process_observability.logging_review import _inspect
        packet,ctx,reply,ids=self.fixture()
        entry=packet['downstream_context']['delivered'].pop()
        packet['downstream_context']['frontier']=[{**entry,'reason':'context_line_budget'}]
        inv=discover_logging(ctx.snapshot,call_names=ctx.snapshot.logging_calls,max_sites=None,max_per_file=None,
                             max_hints=None,include_columns=True,exclude_declarations=True)
        ctx.shown=[]
        _inspect({'action':'inspect','reads':[{k:entry['target'][k] for k in ('path','start_line','end_line')}]},
                 packet,ctx,ReviewOptions(),inv)
        self.assertTrue(packet['downstream_context']['delivered'])
        self.assertFalse(packet['downstream_context']['frontier'])

    def test_recognized_prior_fact_without_binding_is_not_no_record(self):
        packet,ctx,reply,ids=self.fixture()
        reply['context_reviews']=[{'context_call_id':e['context_call_id'],'use':'prior_fact_not_bound',
                                  'reason':'Запись подтверждает факт этой операции, привязка ещё не указана'}
                                 for e in packet['downstream_context']['delivered']]
        row=_validate_reply(reply,packet,ctx)
        self.assertEqual(row['state'],'X')
        self.assertFalse(row['search']['checks']['direct_context'])
        self.assertIn(packet['downstream_context']['delivered'][0]['context_call_id'],' '.join(row['search']['pending']))

    def test_valid_manual_invocation_is_also_an_assessed_context(self):
        packet,ctx,reply,ids=self.fixture()
        entry=packet['downstream_context']['delivered'][0]
        reply['bindings']=[{'site_id':next(iter(ids)),'role':'result','moment':'after_action','meaning':'Получен ID объекта',
                           'conditions':'После проверки','correlation':'Переданный ID','establishes':['occurrence','result'],
                           'invocation_ref':entry['invocation'],'argument_evidence':[entry['invocation']]}]
        row=_validate_reply(reply,packet,ctx)
        self.assertEqual(row['context_assessment']['entries'][0]['use'],'bound')

    def test_saved_required_flag_and_new_profile_cannot_be_downgraded(self):
        from process_observability.context_assessment import validate_saved
        packet,ctx,reply,ids=self.fixture()
        reply['context_reviews']=[{'context_call_id':e['context_call_id'],'use':'not_relevant','reason':'Другой факт'}
                                 for e in packet['downstream_context']['delivered']]
        row=_validate_reply(reply,packet,ctx)
        data={'inventory':{'sites':packet['candidates']},'source_fingerprints':ctx.snapshot.fingerprints,
              'review_profile':{'context_assessment':'delivered-call/1'}}
        for delete in (False,True):
            bad=deepcopy(row)
            if delete:bad.pop('context_assessment')
            else:bad['context_assessment'].update(required=False,entries=[])
            with self.assertRaises(LoggingReviewError):validate_saved(bad,data)

    def test_selector_only_binding_is_not_reported_missing_by_repair(self):
        packet,ctx,reply,ids=self.fixture()
        entry=packet['downstream_context']['delivered'][0]
        reply['bindings']=[{'site_id':next(iter(ids)),'context_call_id':entry['context_call_id'],
                           'role':'result','moment':'after_action','meaning':'Получен ID объекта','conditions':'После проверки',
                           'correlation':'Переданный ID','establishes':['occurrence','result']}]
        _validate_reply(reply,packet,ctx)
        self.assertNotIn('context_not_assessed',{d['code'] for d in _repair_details(reply,packet,ctx)['issues']})

    def test_manual_same_line_ambiguity_does_not_assign_a_call(self):
        from process_observability.context_assessment import bound_context_ids
        entries=[{'context_call_id':key,'candidate_site_ids':['s'],
                  'invocation':{'path':'x.py','start_line':8,'end_line':8,'column':col}}
                 for key,col in [('first',3),('second',20)]]
        binding={'site_id':'s','invocation_ref':{'path':'x.py','start_line':8,'end_line':8}}
        self.assertEqual(bound_context_ids([binding],entries),set())
        binding['invocation_ref']['column']=20
        self.assertEqual(bound_context_ids([binding],entries),{'second'})


if __name__=='__main__':unittest.main()
