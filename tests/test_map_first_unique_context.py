"""References may be assembled only for a model-selected, unambiguous delivered route."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from process_observability.logging_review import _validate_reply, _repair_details
from process_observability.logging_contracts import LoggingReviewError
import test_map_first_context_accounting as accounting
from test_map_first_context_reuse import PYTHON, JAVA

class UniqueContextControls(unittest.TestCase):
    def fixture(self, java=False, code=None):
        packet, ctx, reply, ids = accounting.ContextAccountingControls().fixture(java, code)
        entry=packet['downstream_context']['delivered'][0]
        reply['bindings']=[{'site_id':entry['candidate_site_ids'][0], 'role':'result', 'moment':'after_action',
                           'meaning':'Получен объект с идентификатором', 'conditions':'После проверки объекта',
                           'correlation':'Ключ объекта в записи; повторные обращения не различает',
                           'establishes':['occurrence','result'], 'covers_outcomes':[],
                           'invocation_ref':None, 'argument_evidence':[]}]
        reply['missing']=[]
        return packet,ctx,reply,entry

    def test_python_and_java_omitted_selector_matches_explicit_route_without_raw_rewrite(self):
        for java in (False,True):
            with self.subTest(java=java):
                packet,ctx,reply,entry=self.fixture(java);before=deepcopy(reply)
                explicit=deepcopy(reply);explicit['bindings'][0]['context_call_id']=entry['context_call_id']
                expected=_validate_reply(explicit,packet,ctx)
                actual=_validate_reply(reply,packet,ctx)
                self.assertEqual(actual['bindings'],expected['bindings'])
                self.assertEqual(actual['state'],expected['state'])
                self.assertTrue(any('единственному' in w for w in actual['warnings']))
                self.assertEqual(reply,before)
                issues=_repair_details(reply,packet,ctx)['issues']
                self.assertFalse([i for i in issues if i['code'] in ('context_selector_available','context_not_assessed')])

    def test_two_calls_are_not_reduced_to_a_unique_shortlist(self):
        code=PYTHON.replace('journal','logger').replace('    return appointment_details(item.key)',
                '    appointment_details(item.key)\n    return appointment_details(item.key)')
        for hide in (False,True):
            packet,ctx,reply,entry=self.fixture(code=code)
            self.assertEqual(len(packet['downstream_context']['delivered']),2)
            if hide:packet['downstream_context']['delivered']=packet['downstream_context']['delivered'][:1]
            with self.assertRaises(LoggingReviewError):_validate_reply(reply,packet,ctx)

    def test_ownerless_operation_anchor_prevents_assuming_unique_route(self):
        from process_observability.logging_binding_sources import assemble_context_routes
        code=PYTHON.replace('journal','logger')+'appointment_details("second")\n'
        packet,ctx,reply,_=self.fixture(code=code)
        span={'path':'Clinic.py','start_line':12,'end_line':12}
        self.assertIsNone(ctx.navigation.owner(span))
        packet['operation_ranges'].append(span);ctx.read('Clinic.py',12,12)
        normalized,notes=assemble_context_routes(reply,packet,ctx)
        self.assertEqual(normalized,reply);self.assertFalse(notes)

    def test_nested_python_call_is_counted_even_when_shortlist_hides_it(self):
        from process_observability.logging_binding_sources import assemble_context_routes
        code=PYTHON.replace('journal','logger')+'    def again():\n        return appointment_details("second")\n'
        packet,ctx,reply,entry=self.fixture(code=code)
        packet['operation_ranges'].append({'path':'Clinic.py','start_line':7,'end_line':13})
        packet['downstream_context']['delivered']=[entry]
        ctx.read('Clinic.py',7,13)
        normalized,notes=assemble_context_routes(reply,packet,ctx)
        self.assertEqual(normalized,reply);self.assertFalse(notes)

    def test_java_lambda_and_anonymous_calls_prevent_unique_assembly(self):
        from process_observability.logging_binding_sources import assemble_context_routes
        for nested in ('Runnable again = () -> describe("second");',
                       'Runnable again = new Runnable() { public void run() { describe("second"); } };'):
            with self.subTest(nested=nested):
                code=JAVA.replace('        describe(book.key);','        describe(book.key);\n        '+nested)
                packet,ctx,reply,entry=self.fixture(java=True,code=code)
                packet['operation_ranges'].append({'path':'Library.java','start_line':14,'end_line':15})
                packet['downstream_context']['delivered']=[entry]
                ctx.read('Library.java',14,15)
                owner=ctx.navigation.owner(packet['operation_ranges'][-1])
                calls=ctx.navigation.java.member_calls(owner['id'])
                self.assertGreaterEqual(len([c for c in calls if c['name']=='describe']),2)
                normalized,notes=assemble_context_routes(reply,packet,ctx)
                self.assertEqual(normalized,reply);self.assertFalse(notes)

    def test_confirmed_route_with_unsupported_prior_fact_stays_explicit(self):
        from process_observability.logging_binding_sources import assemble_context_routes
        code=PYTHON.replace('journal','logger').replace('    return appointment_details(item.key)',
                    '    marker = 1\n    return appointment_details(item.key)')
        packet,ctx,reply,entry=self.fixture(code=code)
        from process_observability.logging_source_context import contextual_basis
        site=next(s for s in packet['candidates'] if s['id']==reply['bindings'][0]['site_id'])
        self.assertNotEqual(contextual_basis(ctx,packet,site,entry['invocation'],True)['status'],'supported')
        normalized,notes=assemble_context_routes(reply,packet,ctx)
        self.assertEqual(normalized,reply);self.assertFalse(notes)

    def test_frontier_or_unread_source_prevents_assembly(self):
        for java in (False,True):
            for kind in ('frontier','unread'):
                with self.subTest(java=java,kind=kind):
                    packet,ctx,reply,entry=self.fixture(java)
                    if kind=='frontier':packet['downstream_context']['frontier'].append({**entry,'reason':'ambiguous_call_target'})
                    else:ctx.shown=[r for r in ctx.shown if not (r['path']==entry['target']['path'] and r['start_line']<=entry['target']['start_line']<=r['end_line'])]
                    with self.assertRaises(LoggingReviewError):_validate_reply(reply,packet,ctx)

    def test_explicit_bad_selector_or_witness_is_not_replaced(self):
        for change in ({'context_call_id':'not-delivered'},{'invocation_ref':{'path':'missing.py','start_line':1,'end_line':1}},
                       {'argument_evidence':[{'path':'missing.py','start_line':1,'end_line':1}]}):
            packet,ctx,reply,_=self.fixture();reply['bindings'][0].update(change);before=deepcopy(reply)
            with self.assertRaises(LoggingReviewError):_validate_reply(reply,packet,ctx)
            self.assertEqual(reply,before)

    def test_explicit_context_disagreement_is_not_overridden(self):
        packet,ctx,reply,entry=self.fixture()
        reply['context_reviews']=[{'context_call_id':entry['context_call_id'],'use':'not_relevant','reason':'Иной факт'}]
        with self.assertRaises(LoggingReviewError):_validate_reply(reply,packet,ctx)

    def test_unconfirmed_route_is_not_assembled(self):
        for java in (False,True):
            packet,ctx,reply,_=self.fixture(java)
            with patch.object(ctx.navigation,'invocation_route',return_value={'status':'unresolved'}):
                with self.assertRaises(LoggingReviewError):_validate_reply(reply,packet,ctx)

    def test_no_interpretation_of_context_id_written_inside_prose(self):
        packet,ctx,reply,entry=self.fixture()
        reply['bindings'][0]['correlation']='context_call_id=forged-other-call'
        row=_validate_reply(reply,packet,ctx)
        self.assertEqual(row['bindings'][0]['context_call_id'],entry['context_call_id'])
        self.assertEqual(row['bindings'][0]['correlation'],reply['bindings'][0]['correlation'])
        # The source assembler does not judge or correct this intentionally bad semantic prose.

    def test_actual_ambiguous_java_receiver_stays_unselected(self):
        from pathlib import Path
        import tempfile
        from process_observability.map_input import MapView,RepoContext
        from process_observability.source_tools import snapshot_repository
        from process_observability.log_discovery import discover_logging
        from process_observability.logging_review import _Navigator,ReviewOptions
        from process_observability.logging_binding_sources import assemble_context_routes
        code=('import org.slf4j.Logger;\ninterface Notice { void send(String key); }\n'
              'class Email implements Notice { Logger log; public void send(String key) { log.info("email {}",key); } }\n'
              'class Sms implements Notice { Logger log; public void send(String key) { log.info("sms {}",key); } }\n'
              'class Ship { Notice notice; void finish(Item item) {\n'
              '    item.finish();\n    notice.send(item.key);\n} }\n')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'Shipment.java').write_text(code)
            view=MapView.from_dict({'schema_version':'2.0','sources':[{'id':'s'}],'scenario':{},'edges':[],
                'nodes':[{'id':'finish','kind':'operation','name':'Завершить отправку','evidence':[
                    {'path':'s/Shipment.java','line_start':6,'line_end':6,'quote':code.splitlines()[5]}]}],
                'run':{'id':'synthetic','status':'completed'}})
            snap=snapshot_repository(root,max_files=None,max_chars=None)
            ctx=RepoContext(snap,view);ctx.logging_contracts=[]
            inventory=discover_logging(snap,max_sites=None,max_per_file=None,max_hints=None,include_columns=True,exclude_declarations=True)
            packet=_Navigator(ctx,inventory,ReviewOptions()).initial(view.operations[0])
            self.assertTrue(packet['downstream_context']['frontier'])
            self.assertFalse(packet['downstream_context']['delivered'])
            for site in inventory['sites']:
                # Make every known registry site visible; ambiguity must still prevent route assembly.
                if site['id'] not in {s['id'] for s in packet['candidates']}:packet['candidates'].append(site)
                reply={'bindings':[{'site_id':site['id']}]}
                normalized,notes=assemble_context_routes(reply,packet,ctx)
                self.assertEqual(normalized,reply);self.assertFalse(notes)

    def test_disconnected_same_name_java_method_does_not_get_other_route(self):
        from process_observability.logging_binding_sources import assemble_context_routes
        from process_observability.log_discovery import discover_logging
        code=JAVA+'class Legacy { org.slf4j.Logger journal; void describe(String key) { journal.info("legacy {}",key); } }\n'
        packet,ctx,reply,entry=self.fixture(java=True,code=code)
        inventory=discover_logging(ctx.snapshot,call_names=ctx.snapshot.logging_calls,max_sites=None,max_per_file=None,max_hints=None,include_columns=True,exclude_declarations=True)
        legacy=next(s for s in inventory['sites'] if 'legacy' in str(s.get('event_key')))
        packet['candidates'].append(legacy)
        ctx.read('Library.java',legacy['start_line'],legacy['end_line'])
        reply['bindings'][0]['site_id']=legacy['id']
        normalized,notes=assemble_context_routes(reply,packet,ctx)
        self.assertEqual(normalized,reply);self.assertFalse(notes)
        with self.assertRaises(LoggingReviewError):_validate_reply(reply,packet,ctx)

    def test_saved_reply_keeps_original_selector_and_revalidates_route_on_reuse(self):
        from process_observability.logging_review import review_logging
        from process_observability.operation_coverage import validate_saved_rows
        from test_map_first_logging import Scripted
        packet,ctx,reply,entry=self.fixture()
        before=deepcopy(reply)
        first=review_logging(ctx.view,ctx.snapshot,Scripted(lambda p,n:deepcopy(reply)))
        self.assertEqual(first['operations'][0]['state'],'C')
        self.assertEqual(first['checkpoints'][0]['reply'],before)
        self.assertNotIn('context_call_id',first['checkpoints'][0]['reply']['bindings'][0])
        validate_saved_rows(ctx.view,first)
        second=review_logging(ctx.view,ctx.snapshot,Scripted(lambda p,n:deepcopy(reply)),resume=first)
        self.assertEqual(second['model_requests'],0)
        self.assertEqual(first['operations'],second['operations'])

    def test_route_assembly_does_not_promote_an_intent_to_result(self):
        packet,ctx,reply,entry=self.fixture()
        reply['bindings'][0].update(role='request',moment='before_action',establishes=['occurrence'])
        reply['missing']=['Результат действия не подтверждён']
        row=_validate_reply(reply,packet,ctx)
        self.assertEqual(row['state'],'P')
        self.assertEqual(row['bindings'][0]['establishes'],['occurrence'])
        self.assertTrue(row['missing'])

    def test_own_code_has_no_added_route_and_foreign_error_is_addressed(self):
        packet,ctx,reply,entry=self.fixture()
        own=next(s for s in packet['candidates'] if s['own_code'])
        reply['bindings'][0].update(site_id=own['id'])
        reply['candidate_reviews']=[d for d in reply['candidate_reviews'] if d['site_id']!=own['id']]
        row=_validate_reply(reply,packet,ctx)
        self.assertIsNone(row['bindings'][0]['invocation_ref'])
        self.assertFalse(any('единственному' in w for w in row['warnings']))
        packet,ctx,reply,entry=self.fixture();packet['downstream_context']['delivered']=[]
        with self.assertRaisesRegex(LoggingReviewError,r'bindings\[0\].*site_id='):_validate_reply(reply,packet,ctx)

if __name__=='__main__':unittest.main()
