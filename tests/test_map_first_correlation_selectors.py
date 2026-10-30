"""Analyzer navigation IDs are not emitted record fields; field discovery stays open-ended."""
from copy import deepcopy
import unittest
import process_observability.logging_review as review
from process_observability.logging_contracts import LoggingReviewError
from process_observability.operation_coverage import validate_saved_rows
import test_map_first_prior_fact_accounting as prior_fact
from test_map_first_logging import Scripted


class CorrelationSelectorControls(unittest.TestCase):
    def fixture(self, java=False):
        packet,ctx,reply,entry=prior_fact.PriorFactAccountingControls().fixture(java)
        reply['context_reviews']=[]
        reply['bindings'].append({'site_id':entry['candidate_site_ids'][0],
            'context_call_id':entry['context_call_id'],'role':'result','moment':'after_action',
            'meaning':'Получен идентификатор объекта','conditions':'После положительной проверки объекта',
            'correlation':'Идентификатор объекта; отдельные попытки не различает',
            'establishes':['occurrence','result'],'covers_outcomes':['available']})
        self.assertEqual(review._validate_reply(reply,packet,ctx)['state'],'C')
        return packet,ctx,reply,entry

    def test_exact_delivered_selector_is_rejected_without_rewriting(self):
        for java in (False,True):
            packet,ctx,reply,entry=self.fixture(java)
            reply['bindings'][-1]['correlation']='context_call_id: '+entry['context_call_id']
            original=deepcopy(reply)
            with self.assertRaisesRegex(LoggingReviewError,'correlation_host_selector'):
                review._validate_reply(reply,packet,ctx)
            issue=next(x for x in review._repair_details(reply,packet,ctx)['issues'] if x['code']=='correlation_host_selector')
            self.assertEqual(issue['field'],'bindings[1].correlation')
            self.assertEqual(issue['selector_values'],[entry['context_call_id']]);self.assertEqual(reply,original)

    def test_adapter_selector_from_another_candidate_is_also_reserved(self):
        packet,ctx,reply,_=self.fixture(True)
        packet['candidates'][0]['logging_chain']={'adapter_candidates':[
            {'source_contract':{'candidate_id':'adapter-issued-library'}}]}
        reply['bindings'][-1]['correlation']='adapter-issued-library'
        with self.assertRaisesRegex(LoggingReviewError,'correlation_host_selector'):
            review._validate_reply(reply,packet,ctx)

    def test_frontier_selector_is_not_a_record_field(self):
        packet,ctx,reply,_=self.fixture()
        packet['downstream_context']['frontier'].append({'context_call_id':'context-issued-frontier'})
        reply['bindings'][-1]['correlation']='context-issued-frontier'
        issues=review._repair_details(reply,packet,ctx)['issues']
        self.assertIn('correlation_host_selector',{x['code'] for x in issues})

    def test_unknown_similar_values_and_real_field_names_are_not_banned(self):
        packet,ctx,reply,entry=self.fixture()
        for value in ('Поле context_call_id приложения', 'context-not-issued',
                      'prefix'+entry['context_call_id'],entry['context_call_id']+'suffix',
                      entry['context_call_id']+'-different'):
            reply['bindings'][-1]['correlation']=value
            self.assertEqual(review._validate_reply(reply,packet,ctx)['state'],'C')

    def test_exact_token_inside_quotes_is_rejected(self):
        packet,ctx,reply,entry=self.fixture()
        reply['bindings'][-1]['correlation']='Ключ: «'+entry['context_call_id']+'».'
        with self.assertRaisesRegex(LoggingReviewError,'correlation_host_selector'):
            review._validate_reply(reply,packet,ctx)

    def test_fields_are_hints_not_a_closed_selection_or_absence_proof(self):
        packet,ctx,reply,entry=self.fixture()
        site=next(s for s in packet['candidates'] if s['id']==reply['bindings'][-1]['site_id'])
        site.update(fields=['object_key'],fields_status='partial')
        reply['bindings'][-1]['correlation']=entry['context_call_id']
        issue=next(x for x in review._repair_details(reply,packet,ctx)['issues'] if x['code']=='correlation_host_selector')
        self.assertEqual(issue['field_hints'],{'fields':['object_key'],'status':'partial','complete':False})
        for value in ('В обёртке виден другой идентификатор; сам по себе не различает попытки',
                      'В показанной записи поля связи нет; контекст SDK не установлен'):
            site['fields']=[];reply['bindings'][-1]['correlation']=value
            self.assertEqual(review._validate_reply(reply,packet,ctx)['state'],'C')

    def test_other_fields_keep_typed_selector_and_unrelated_prose(self):
        packet,ctx,reply,entry=self.fixture()
        reply['bindings'][-1]['conditions']+='; навигационная ссылка '+entry['context_call_id']
        row=review._validate_reply(reply,packet,ctx)
        self.assertEqual(row['bindings'][-1]['context_call_id'],entry['context_call_id'])

    def test_feedback_collects_selector_issue_beside_an_earlier_reference_error(self):
        packet,ctx,reply,entry=self.fixture()
        reply['bindings'][0]['evidence']=[{'path':'absent.py','start_line':1,'end_line':1}]
        reply['bindings'][-1]['correlation']=entry['context_call_id']
        codes={x['code'] for x in review._repair_details(reply,packet,ctx)['issues']}
        self.assertIn('invalid_source_reference',codes);self.assertIn('correlation_host_selector',codes)

    def test_saved_new_profile_rechecks_but_old_profile_exports_remain_readable(self):
        _,ctx,reply,entry=self.fixture()
        book=review.review_logging(ctx.view,ctx.snapshot,Scripted(lambda p,n:deepcopy(reply)))
        validate_saved_rows(ctx.view,book)
        book['operations'][0]['bindings'][-1]['correlation']=entry['context_call_id']
        with self.assertRaisesRegex(LoggingReviewError,'correlation_host_selector'):
            validate_saved_rows(ctx.view,book)
        book['review_profile'].pop('correlation_namespace',None)
        validate_saved_rows(ctx.view,book)

    def test_one_repair_preserves_original_and_failure_stops(self):
        _,ctx,reply,entry=self.fixture();reply['bindings'][-1]['correlation']=entry['context_call_id']
        original=deepcopy(reply)
        for fixed in (False,True):
            def answer(packet,n):
                value=deepcopy(reply)
                if fixed and n==2:value['bindings'][-1]['correlation']='Поле идентификатора объекта; ключ попытки не установлен'
                return value
            model=Scripted(answer)
            book=review.review_logging(ctx.view,ctx.snapshot,model,options=review.ReviewOptions(stop_on_failed_repair=True))
            self.assertEqual(len(model.calls),2)
            self.assertEqual(model.calls[1]['rejected_response']['reply'],original)
            self.assertEqual(book['operations'][0]['state'],'C' if fixed else 'X')
            if not fixed:self.assertEqual(book['stop_reason']['kind'],'repair_failed')
        self.assertEqual(reply,original)

    def test_nontext_correlation_still_fails_normal_validation(self):
        packet,ctx,reply,_=self.fixture();reply['bindings'][-1]['correlation']=None
        with self.assertRaises(LoggingReviewError):review._validate_reply(reply,packet,ctx)
        review._repair_details(reply,packet,ctx)

    def test_saved_namespace_includes_issued_but_unselected_values(self):
        _,ctx,reply,_=self.fixture()
        book=review.review_logging(ctx.view,ctx.snapshot,Scripted(lambda p,n:deepcopy(reply)))
        # A host value issued during inspection need not occur in a selected binding or initial packet.
        book['checkpoints'][0]['issued_correlation_selectors'].append('adapter-inspected-unselected')
        book['operations'][0]['bindings'][-1]['correlation']='adapter-inspected-unselected'
        with self.assertRaisesRegex(LoggingReviewError,'correlation_host_selector'):validate_saved_rows(ctx.view,book)

    def test_missing_saved_namespace_is_not_silently_accepted(self):
        _,ctx,reply,_=self.fixture()
        book=review.review_logging(ctx.view,ctx.snapshot,Scripted(lambda p,n:deepcopy(reply)))
        del book['checkpoints'][0]['issued_correlation_selectors']
        with self.assertRaisesRegex(LoggingReviewError,'issued_correlation_selectors'):validate_saved_rows(ctx.view,book)

    def test_cache_revalidation_rebuilds_host_namespace_without_new_calls(self):
        _,ctx,reply,_=self.fixture()
        book=review.review_logging(ctx.view,ctx.snapshot,Scripted(lambda p,n:deepcopy(reply)))
        expected=deepcopy(book['checkpoints'][0]['issued_correlation_selectors'])
        book['checkpoints'][0]['issued_correlation_selectors']=['forged-not-issued']
        old=deepcopy(book)
        model=Scripted(lambda p,n:self.fail('Valid cache must not call model'))
        actual=review.review_logging(ctx.view,ctx.snapshot,model,resume=book)
        self.assertEqual(actual['checkpoints'][0]['issued_correlation_selectors'],expected)
        self.assertEqual(len(model.calls),0);self.assertEqual(book,old)
        validate_saved_rows(ctx.view,actual)
