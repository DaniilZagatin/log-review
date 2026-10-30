"""A recognized prior fact must not disappear behind a generic choice coverage limit."""
from copy import deepcopy
import unittest
import process_observability.logging_review as review
import test_map_first_context_accounting as accounting
from test_map_first_logging import Scripted
from process_observability.logging_contracts import LoggingReviewError
from process_observability.map_input import MapView
from process_observability.operation_coverage import validate_saved_rows


class PriorFactAccountingControls(unittest.TestCase):
    def fixture(self, java=False, code=None):
        packet,ctx,reply,_=accounting.ContextAccountingControls().fixture(java,code)
        model=deepcopy(ctx.view.original);model['nodes'][0]['continuation_mode']='choice'
        ctx.view=MapView.from_dict(model);packet['operation']=deepcopy(ctx.view.operations[0])
        entry=packet['downstream_context']['delivered'][0]
        site=next(s for s in packet['candidates'] if s['own_code'])
        reply['bindings']=[{'site_id':site['id'],'role':'result','moment':'after_action',
            'meaning':'Проверка не нашла подходящий объект','conditions':'Отрицательная ветка по коду',
            'correlation':'Идентификатор объекта в этой записи отсутствует',
            'establishes':['occurrence','result'],'covers_outcomes':['unavailable']}]
        reply['candidate_reviews']=[x for x in reply['candidate_reviews'] if x['site_id']!=site['id']]
        reply['context_reviews']=[{'context_call_id':entry['context_call_id'],'use':'prior_fact_not_bound',
            'reason':'Запись подтверждает предшествующий факт, но привязка не оформлена'}]
        reply['missing']=[]
        return packet,ctx,reply,entry

    def test_recognized_fact_and_no_gap_rejected_with_matching_repair_feedback(self):
        for java in (False,True):
            with self.subTest(java=java):
                packet,ctx,reply,entry=self.fixture(java);before=deepcopy(reply)
                with self.assertRaisesRegex(LoggingReviewError,'prior_fact_unaccounted'):
                    review._validate_reply(reply,packet,ctx)
                issue=next(x for x in review._repair_details(reply,packet,ctx)['issues'] if x['code']=='prior_fact_unaccounted')
                self.assertEqual(issue['context_call_ids'],[entry['context_call_id']]);self.assertEqual(reply,before)

    def test_named_attribution_gap_remains_P_and_is_not_auto_bound(self):
        for java in (False,True):
            packet,ctx,reply,entry=self.fixture(java)
            reply['missing']=['Запись не различает вызовы из разных операций']
            row=review._validate_reply(reply,packet,ctx)
            self.assertEqual(row['state'],'P');self.assertEqual(len(row['bindings']),1)
            self.assertEqual(row['missing'],reply['missing'])

    def test_explicit_supported_binding_can_account_for_positive_branch(self):
        for java in (False,True):
            packet,ctx,reply,entry=self.fixture(java)
            reply['context_reviews']=[]
            reply['bindings'].append({'site_id':entry['candidate_site_ids'][0],
                'context_call_id':entry['context_call_id'],'role':'result','moment':'after_action',
                'meaning':'Получен идентификатор объекта','conditions':'После положительной проверки объекта',
                'correlation':'ID объекта, без различения попыток','establishes':['occurrence','result'],
                'covers_outcomes':['available']})
            row=review._validate_reply(reply,packet,ctx)
            self.assertEqual(row['state'],'C')

    def test_irrelevant_context_does_not_force_binding_or_named_absence(self):
        packet,ctx,reply,_=self.fixture();reply['context_reviews'][0]['use']='not_relevant'
        row=review._validate_reply(reply,packet,ctx);self.assertEqual(row['state'],'P')
        self.assertNotIn('prior_fact_unaccounted',str(review._repair_details(reply,packet,ctx)))

    def test_unknown_input_remains_X_and_does_not_invent_a_logging_gap(self):
        packet,ctx,reply,_=self.fixture()
        reply['review_tasks']=[{'question':'Каков внешний контракт?','scope':'source',
            'source_basis':{'kind':'code_unavailable','detail':'Внешняя реализация не предоставлена'}}]
        row=review._validate_reply(reply,packet,ctx);self.assertEqual(row['state'],'X');self.assertFalse(row['missing'])

    def test_only_generic_choice_limit_triggers_the_new_typed_rule(self):
        from process_observability.context_assessment import prior_fact_issue
        base={'state':'P','missing':[],'coverage_limits':['choice_branches_not_established'],
            'context_assessment':{'entries':[{'use':'prior_fact_not_bound','context_call_id':'example'}]}}
        self.assertIsNotNone(prior_fact_issue(base,review.REVIEW_PROFILE))
        for limit in ['shared_record_non_sufficient','contextual_basis_not_certified']:
            for limits in [[limit],base['coverage_limits']+[limit]]:
                self.assertIsNone(prior_fact_issue({**base,'coverage_limits':limits},review.REVIEW_PROFILE))
        self.assertIsNone(prior_fact_issue(base,{'context_assessment':'delivered-call/1'}))

    def test_saved_new_profile_rejects_dropped_gap_but_old_export_is_readable(self):
        _,ctx,reply,_=self.fixture();reply['missing']=['Не различим источник вызова']
        book=review.review_logging(ctx.view,ctx.snapshot,Scripted(lambda p,n:deepcopy(reply)))
        self.assertEqual(book['operations'][0]['state'],'P');validate_saved_rows(ctx.view,book)
        book['operations'][0]['missing']=[]
        with self.assertRaisesRegex(LoggingReviewError,'prior_fact_unaccounted'):validate_saved_rows(ctx.view,book)
        book['review_profile']['context_assessment']='delivered-call/1';validate_saved_rows(ctx.view,book)

    def test_single_repair_receives_original_and_failed_repair_stops(self):
        _,ctx,reply,_=self.fixture();before=deepcopy(reply)
        for fix in (False,True):
            def answer(p,n):
                value=deepcopy(reply)
                if fix and n==2:value['missing']=['Не различим источник вызова']
                return value
            model=Scripted(answer)
            result=review.review_logging(ctx.view,ctx.snapshot,model,options=review.ReviewOptions(stop_on_failed_repair=True))
            self.assertEqual(len(model.calls),2)
            self.assertEqual(model.calls[1]['rejected_response']['reply'],before)
            self.assertEqual(result['operations'][0]['state'],'P' if fix else 'X')
            if not fix:self.assertEqual(result['stop_reason']['kind'],'repair_failed')
        self.assertEqual(reply,before)

    def test_malformed_context_reviews_keep_shape_feedback(self):
        for value in (None, 'prior_fact_not_bound', {'use':'prior_fact_not_bound'}):
            packet,ctx,reply,_=self.fixture();reply['context_reviews']=value
            issues=review._repair_details(reply,packet,ctx)['issues']
            self.assertIn('invalid_context_review',{i['code'] for i in issues})

    def test_prompt_replaces_the_deferred_binding_escape_hatch(self):
        self.assertNotIn('но ещё не оформляет привязку',review.SYSTEM)
        self.assertEqual(review.SYSTEM.count('prior_fact_not_bound — запись подтверждает предыдущий факт'),1)

    def test_earlier_source_error_does_not_hide_conditional_fact_feedback(self):
        for java in (False,True):
            packet,ctx,reply,entry=self.fixture(java)
            reply['bindings'][0]['evidence']=[{'path':'missing.file','start_line':1,'end_line':1}]
            before=deepcopy(reply)
            issues=review._repair_details(reply,packet,ctx)['issues']
            self.assertIn('invalid_source_reference',{x['code'] for x in issues})
            issue=next(x for x in issues if x['code']=='prior_fact_unaccounted')
            self.assertTrue(issue['preliminary']);self.assertTrue(issue['applies_when'])
            self.assertEqual(issue['context_call_ids'],[entry['context_call_id']]);self.assertEqual(reply,before)

    def test_raw_host_witness_cannot_suppress_conditional_feedback(self):
        packet,ctx,reply,_=self.fixture()
        reply['bindings'][0]['context_basis']={'status':'not_established'}
        reply['bindings'][0]['flow_context']=[]
        before=deepcopy(reply)
        with self.assertRaises(LoggingReviewError):review._validate_reply(reply,packet,ctx)
        issue=next(x for x in review._repair_details(reply,packet,ctx)['issues'] if x['code']=='prior_fact_unaccounted')
        self.assertTrue(issue['preliminary']);self.assertEqual(reply,before)

    def test_coherent_C_with_redundant_fact_and_bad_proposal_gets_no_gap_hint(self):
        from test_map_first_context_reuse import PYTHON
        code=PYTHON.replace('journal','logger').replace('    return appointment_details(item.key)',
            '    appointment_details(item.key)\n    return appointment_details(item.key)')
        packet,ctx,reply,entry=self.fixture(code=code)
        other=packet['downstream_context']['delivered'][1]
        reply['context_reviews'][0]['context_call_id']=other['context_call_id']
        reply['bindings'].append({'site_id':entry['candidate_site_ids'][0],
            'context_call_id':entry['context_call_id'],'role':'result','moment':'after_action',
            'meaning':'Получен идентификатор объекта','conditions':'После положительной проверки',
            'correlation':'ID объекта без ID попытки','establishes':['occurrence','result'],'covers_outcomes':['available']})
        self.assertEqual(review._validate_reply(reply,packet,ctx)['state'],'C')
        reply['changes']=[{'id':'incomplete'}]
        with self.assertRaises(LoggingReviewError):review._validate_reply(reply,packet,ctx)
        self.assertNotIn('prior_fact_unaccounted',{i['code'] for i in review._repair_details(reply,packet,ctx)['issues']})

    def test_source_question_with_another_error_gets_no_conditional_gap_hint(self):
        packet,ctx,reply,_=self.fixture()
        reply['review_tasks']=[{'question':'Каков внешний контракт?','scope':'source',
            'source_basis':{'kind':'code_unavailable','detail':'Внешняя реализация не предоставлена'}}]
        self.assertEqual(review._validate_reply(reply,packet,ctx)['state'],'X')
        for error in ('proposal','reference'):
            value=deepcopy(reply)
            if error=='proposal':value['changes']=[{'id':'incomplete'}]
            else:value['bindings'][0]['evidence']=[{'path':'missing.file','start_line':1,'end_line':1}]
            self.assertNotIn('prior_fact_unaccounted',{i['code'] for i in review._repair_details(value,packet,ctx)['issues']})


if __name__=='__main__':unittest.main()
