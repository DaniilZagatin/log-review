"""Codex-owned public-behavior controls. No API, target app, target text or credentials."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest
import tempfile
import threading
import hashlib
import re

from process_observability.code_review import ReviewOptions
from process_observability.llm import LLMError
from process_observability.review_execution import review_repository
from process_observability.tracing import TraceRecorder
from process_observability.source_tools import snapshot_repository
from process_observability.code_graph import build_graph
from process_observability.code_review import plan_review
SYSTEM = "Review the supplied code."

class ExecutionCase(unittest.TestCase):
    def runs(self):
        d=tempfile.TemporaryDirectory(); self.addCleanup(d.cleanup); return Path(d.name).resolve()
    def scope(self,files):
        root=self.runs()
        for name,text in files.items():
            p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        snapshot=snapshot_repository(root,max_files=None,max_chars=None)
        return root,snapshot,build_graph(snapshot)

class Scripted:
    def __init__(self,respond):self.respond=respond;self.calls=[];self.lock=threading.Lock()
    def complete_json(self,system,user):
        with self.lock:self.calls.append((system,user));number=len(self.calls)
        return self.respond(json.loads(user),number)

def reply_for(brief):
    return {"findings":[{"id":f"f{i}","kind":"branch","description":f"Behaviour of {r['symbol']}",
        "refs":[{"path":r['path'],"start_line":r['start_line'],"end_line":r['start_line'],"symbol":r['symbol']}]} for i,r in enumerate(brief['owned_intervals'])],
        "interval_status":[{"interval_id":r['id'],"status":"reviewed","note":""} for r in brief['owned_intervals']],
        "requests":[],"unknowns":[]}

SOURCE = 'raise RuntimeError("fixture: never execute")\n\ndef submit(item):\n    save(item)\n    return item\n'
OPTIONS = ReviewOptions(relations=True, max_followups=0, workers=1)


def good(brief):
    return {**reply_for(brief), 'relations': []}


def span_extra(reply, brief):
    row=brief['owned_intervals'][0]
    reply['requests']=[{'type':'span','path':row['path'],'start_line':row['start_line'],
        'end_line':row['end_line'],'symbol_id':'foreign-request-only-field',
        'reason':'Read the remainder','finding_ids':['f0']}]


def baseless(reply, brief):
    ref=copy.deepcopy(reply['findings'][0]['refs'][0])
    reply['relations']=[{'id':'rel1','kind':'call','source':ref,'refs':[copy.deepcopy(ref)],
        'resolution':'resolved','target':{'symbol_id':None,'ref':None,'label':'persist'},
        'condition':None,'data':None}]


def foreign_source(reply, brief):
    reply['findings'][0]['refs'][0]['path']='not_supplied.py'


class IndependentTechnicalRecovery(ExecutionCase):
    def setUp(self):
        self.root,self.snapshot,self.graph=self.scope({'app/work.py':SOURCE})

    def run_review(self, change=None, *, options=OPTIONS, respond=None, checkpoint=None, resume=None, trace=None):
        if respond is None:
            def respond(brief, number):
                reply=good(brief)
                if change: change(reply, brief)
                return reply
        model=Scripted(respond)
        result=review_repository(self.snapshot,self.graph,model,system=SYSTEM,options=options,
            checkpoint_dir=checkpoint,resume_from=resume,trace=trace)
        return model,result

    def assert_recovered(self, model, result, uncertainty=True):
        self.assertEqual(len(model.calls),2)
        self.assertTrue(result['findings'], 'valid verified findings must survive auxiliary repair failure')
        self.assertFalse(result['errors'])
        if uncertainty:self.assertTrue(result['unknowns'], 'recovery uncertainty/debt must be downstream-visible')
        rows=result['ledger']['tasks']
        self.assertEqual(len(rows),1)
        self.assertTrue(rows[0]['rejections'], 'received-invalid attempts remain recorded')

    def test_repaired_span_extra_field_retains_findings_and_diagnostic(self):
        model,result=self.run_review(span_extra)
        self.assert_recovered(model,result,uncertainty=False)
        def warnings(value):
            if isinstance(value,dict):
                if 'action' in value:yield value
                for k,v in value.items():
                    if k not in ('rejections','rejection_details'):yield from warnings(v)
            elif isinstance(value,list):
                for item in value:yield from warnings(item)
        self.assertTrue(any('symbol_id' in json.dumps(w) and 'remov' in json.dumps(w).lower() for w in warnings(result['ledger']['tasks'])))
        feedback=json.loads(model.calls[1][1]).get('validation_feedback')
        self.assertIsInstance(feedback,dict)
        self.assertIn('requests[0]',feedback.get('path',''))
        self.assertIn('symbol_id',json.dumps(feedback))
        self.assertEqual(len(result['ledger']['requests']),1)
        request=result['ledger']['requests'][0]
        row=json.loads(model.calls[0][1])['owned_intervals'][0]
        self.assertEqual((request['type'],request['path'],request['start_line'],request['end_line']),('span',row['path'],row['start_line'],row['end_line']))
        self.assertTrue(request['finding_ids'])
        self.assertIn(request['status'],('merged','cap_reached'))
        self.assertEqual(result['ledger']['summary']['followup_tasks'],0)

    def test_unknown_kind_is_other_only_after_real_repair(self):
        def change(reply,brief): reply['findings'][0]['kind']='dispatch'
        model,result=self.run_review(change)
        self.assert_recovered(model,result)
        self.assertEqual(result['findings'][0]['kind'],'other')
        self.assertIn('finding',json.dumps(json.loads(model.calls[1][1])['validation_feedback']))

    def test_baseless_relation_is_unresolved_with_raw_label(self):
        model,result=self.run_review(baseless)
        self.assert_recovered(model,result)
        rel=result['relations']['items'][0]
        self.assertEqual(rel['resolution'],'unresolved')
        self.assertEqual(rel['target']['label'],'persist')
        self.assertIsNone(rel['target'].get('symbol_id'))
        self.assertIsNone(rel['target'].get('ref'))

    def test_valid_ref_cannot_be_discarded_to_hide_bad_target_ref(self):
        def change(reply,brief):
            baseless(reply,brief)
            ref=copy.deepcopy(reply['findings'][0]['refs'][0]);ref['path']='not_supplied.py'
            reply['relations'][0]['target']['ref']=ref
            reply['relations'][0]['resolution']='external'
        model,result=self.run_review(change)
        self.assertEqual(len(model.calls),2)
        self.assertFalse(result['findings'])
        self.assertTrue(result['errors'])

    def test_auxiliary_defect_never_hides_foreign_finding_source(self):
        def change(reply,brief): span_extra(reply,brief);foreign_source(reply,brief)
        model,result=self.run_review(change)
        self.assertEqual(len(model.calls),2)
        self.assertFalse(result['findings'])
        self.assertTrue(result['errors'])

    def test_foreign_relation_source_is_strict(self):
        def change(reply,brief):
            baseless(reply,brief);reply['relations'][0]['source']['path']='not_supplied.py'
        model,result=self.run_review(change)
        self.assertEqual(len(model.calls),2)
        self.assertFalse(result['relations']['items'])
        self.assertTrue(result['errors'])

    def test_unknown_core_finding_identity_is_strict(self):
        def change(reply,brief):
            reply['findings'].append(copy.deepcopy(reply['findings'][0]))
        model,result=self.run_review(change)
        self.assertEqual(len(model.calls),2)
        self.assertFalse(result['findings'])
        self.assertTrue(result['errors'])

    def test_no_label_cannot_fabricate_unresolved_target(self):
        def change(reply,brief): baseless(reply,brief);reply['relations'][0]['target']['label']=None
        model,result=self.run_review(change)
        self.assertEqual(len(model.calls),2)
        self.assertTrue(result['errors'])

    def test_repair_attempts_zero_forbids_recovery(self):
        model,result=self.run_review(span_extra,options=replace(OPTIONS,repair_attempts=0))
        self.assertEqual(len(model.calls),1)
        self.assertFalse(result['findings'])
        self.assertTrue(result['errors'])

    def test_missing_owned_interval_never_becomes_reviewed(self):
        def change(reply,brief): reply['interval_status']=[]
        model,result=self.run_review(change)
        self.assertEqual(len(model.calls),2)
        feedback=json.loads(model.calls[1][1]).get('validation_feedback')
        self.assertIsInstance(feedback,dict)
        self.assertIn('interval_status',feedback.get('path',''))
        self.assertFalse(result['coverage_complete'])
        for row in result['ledger']['intervals']:
            self.assertNotEqual(row.get('model_status'),'reviewed')

    def test_feedback_repairs_exact_defect_without_recovery(self):
        def respond(brief,number):
            reply=good(brief)
            if number==1:span_extra(reply,brief)
            return reply
        model,result=self.run_review(respond=respond)
        self.assertEqual(len(model.calls),2)
        repair=json.loads(model.calls[1][1]);self.assertEqual(repair['previous_reply_rejected'],'request_fields')
        self.assertIn('validation_feedback',repair)
        self.assertIn('requests[0]',repair['validation_feedback']['path'])
        self.assertIn('symbol_id',json.dumps(repair['validation_feedback']))
        self.assertTrue(result['findings']);self.assertFalse(result['unknowns']);self.assertTrue(result['complete'])

    def test_non_json_gets_one_repair_and_never_success_from_nothing(self):
        model,result=self.run_review(respond=lambda brief,number:'not json')
        self.assertEqual(len(model.calls),2)
        self.assertFalse(result['findings'])
        self.assertTrue(result['errors'])

    def test_missing_content_gets_single_repair_then_real_json(self):
        def respond(brief,number):
            if number==1: raise LLMError('empty envelope',category='missing_content')
            return good(brief)
        model,result=self.run_review(respond=respond)
        self.assertEqual(len(model.calls),2)
        self.assertIn('previous_reply_rejected',json.loads(model.calls[1][1]))
        self.assertTrue(result['findings'])
        self.assertFalse(result['errors'])

    def test_missing_content_twice_remains_failed(self):
        def respond(brief,number): raise LLMError('empty envelope',category='missing_content')
        model,result=self.run_review(respond=respond)
        self.assertEqual(len(model.calls),2)
        self.assertFalse(result['findings'])
        self.assertTrue(result['errors'])

    def test_http_failure_not_retried_as_schema_repair(self):
        def respond(brief,number): raise LLMError('HTTP failure',category='http',status=403)
        model,result=self.run_review(respond=respond)
        self.assertEqual(len(model.calls),1)
        self.assertTrue(result['errors'])

    def test_valid_checkpoint_remains_compatible_without_model_call(self):
        old=self.runs();new=self.runs()
        model,first=self.run_review(checkpoint=old)
        model,second=self.run_review(checkpoint=new,resume=old,
            respond=lambda brief,number:self.fail('valid checkpoint must not call model'))
        self.assertFalse(model.calls)
        self.assertEqual(first['findings'],second['findings'])
        self.assertEqual(first['relations'],second['relations'])

    def test_derived_checkpoint_resume_rederives_without_model_call(self):
        old=self.runs();new=self.runs()
        _,first=self.run_review(span_extra,checkpoint=old)
        self.assertTrue(first['findings'])
        model,second=self.run_review(checkpoint=new,resume=old,
            respond=lambda brief,number:self.fail('valid recovered checkpoint must rederive offline'))
        self.assertFalse(model.calls)
        self.assertEqual(first['findings'],second['findings'])
        self.assertEqual(first['unknowns'],second['unknowns'])

    def test_checkpoint_response_tamper_is_not_reused(self):
        old=self.runs();new=self.runs()
        _,first=self.run_review(span_extra,checkpoint=old)
        self.assertTrue(first['findings'])
        [file]=list((old/'tasks').glob('*.json'))
        checkpoint=json.loads(file.read_text())
        raw_key='response' if 'response' in checkpoint else 'raw_response'
        checkpoint[raw_key]['findings'][0]['description']='tampered but schema valid'
        file.write_text(json.dumps(checkpoint))
        model,second=self.run_review(checkpoint=new,resume=old)
        self.assertTrue(model.calls,'tampered derived response must not become a cache hit')
        self.assertNotEqual(second['findings'][0]['description'],'tampered but schema valid')

    def test_aux_kind_first_then_foreign_source_is_strict(self):
        def change(reply,brief):reply['findings'][0]['kind']='dispatch';foreign_source(reply,brief)
        model,result=self.run_review(change)
        self.assertEqual(len(model.calls),2);self.assertTrue(result['errors']);self.assertFalse(result['findings'])

    def test_aux_kind_first_then_duplicate_finding_id_is_strict(self):
        def change(reply,brief):
            reply['findings'][0]['kind']='dispatch';reply['findings'].append(copy.deepcopy(reply['findings'][0]))
        model,result=self.run_review(change)
        self.assertEqual(len(model.calls),2);self.assertTrue(result['errors']);self.assertFalse(result['findings'])

    def test_aux_relation_first_then_invalid_unknowns_is_strict(self):
        def change(reply,brief):baseless(reply,brief);reply['unknowns']=[{}]
        model,result=self.run_review(change)
        self.assertEqual(len(model.calls),2);self.assertTrue(result['errors']);self.assertFalse(result['findings'])

    def test_primary_aux_cannot_be_recovered_when_repair_is_not_json(self):
        def respond(brief,number):
            if number==2:return 'not json'
            r=good(brief);span_extra(r,brief);return r
        model,result=self.run_review(respond=respond)
        self.assertEqual(len(model.calls),2);self.assertTrue(result['errors']);self.assertFalse(result['findings'])

    def test_primary_aux_cannot_be_recovered_when_repair_has_no_content(self):
        def respond(brief,number):
            if number==2:raise LLMError('no content',category='missing_content')
            r=good(brief);span_extra(r,brief);return r
        model,result=self.run_review(respond=respond)
        self.assertEqual(len(model.calls),2);self.assertTrue(result['errors']);self.assertFalse(result['findings'])

    def test_primary_aux_cannot_be_recovered_when_repair_has_foreign_source(self):
        def respond(brief,number):
            r=good(brief)
            if number==1:span_extra(r,brief)
            else:foreign_source(r,brief)
            return r
        model,result=self.run_review(respond=respond)
        self.assertEqual(len(model.calls),2);self.assertTrue(result['errors']);self.assertFalse(result['findings'])

    def test_foreign_interval_and_missing_owned_remain_strict(self):
        def change(reply,brief):reply['interval_status']=[{'interval_id':'foreign','status':'reviewed','note':''}]
        model,result=self.run_review(change)
        self.assertEqual(len(model.calls),2);self.assertFalse(result['coverage_complete']);self.assertTrue(result['errors'])

    def test_duplicate_interval_and_missing_other_never_review_missing(self):
        def change(reply,brief):reply['interval_status'].append(copy.deepcopy(reply['interval_status'][0]))
        model,result=self.run_review(change)
        self.assertEqual(len(model.calls),2);self.assertTrue(result['errors'])

    def test_invalid_aux_read_is_quarantined_with_explicit_debt(self):
        def change(reply,brief):
            span_extra(reply,brief);reply['requests'][0]['finding_ids']=['not_a_finding']
        cache=self.runs();model,result=self.run_review(change,checkpoint=cache)
        self.assert_recovered(model,result)
        rows=result['ledger']['requests'];self.assertEqual(len(rows),1)
        self.assertTrue(rows[0].get('quarantined'));self.assertEqual(rows[0]['status'],'invalid')
        [file]=list((cache/'tasks').glob('*.json'));stored=json.loads(file.read_text())
        def contains_original(value):
            if isinstance(value,dict):
                return value.get('finding_ids')==['not_a_finding'] or any(contains_original(v) for v in value.values())
            return any(contains_original(v) for v in value) if isinstance(value,list) else False
        self.assertTrue(contains_original(stored),'original invalid linked identity must remain in raw provenance')
        self.assertTrue(result['ledger']['summary']['request_debt'])

    def test_recovery_never_occurs_if_the_repair_cannot_dispatch(self):
        # Inject a stricter resource ceiling at the dispatcher boundary after the first real
        # scripted response. This is a guard control, not a claim that the planner produced it.
        from unittest.mock import patch
        from process_observability import review_execution as execution
        original=execution._Run.dispatch
        def limited(runtime,task,rejected):
            if rejected is not None:
                runtime.options=replace(runtime.options,request_chars=len(task.system)+len(task.user))
            return original(runtime,task,rejected)
        with patch.object(execution._Run,'dispatch',limited):
            model,result=self.run_review(span_extra)
        self.assertEqual(len(model.calls),1)
        self.assertEqual(result['errors'][0]['category'],'repair_exceeds_request_budget')
        self.assertFalse(result['findings'])

    def test_true_old_baseline_checkpoint_is_byte_preserved(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/synthetic/technical-valid-cache.json').read_text())
        old=self.runs();new=self.runs();(old/'tasks').mkdir()
        (old/'run.json').write_text(json.dumps(fixture['run']))
        for name,text in fixture['tasks'].items():(old/'tasks'/name).write_text(text)
        model,result=self.run_review(checkpoint=new,resume=old)
        self.assertFalse(model.calls);self.assertEqual(result['ledger']['summary']['cache_hits'],1)
        self.assertEqual(result['ledger']['summary']['model_calls'],0)
        self.assertEqual(result['findings'],fixture['findings']);self.assertEqual(result['relations'],fixture['relations'])
        for name,text in fixture['tasks'].items():self.assertEqual((new/'tasks'/name).read_text(),text)

    def test_every_stored_reply_and_binding_hash_rejects_simple_tampering(self):
        old=self.runs();_,result=self.run_review(span_extra,checkpoint=old);self.assertTrue(result['findings'])
        [file]=list((old/'tasks').glob('*.json'));original=json.loads(file.read_text())
        reply_paths=[];hash_paths=[];string_reply_paths=[];provenance_paths=[]
        baseline_keys={'schema','run_fingerprint','task_id','kind','request_sha256','status','attempts','response'}
        def walk(value,path=(),provenance=False):
            if isinstance(value,dict):
                if isinstance(value.get('findings'),list):reply_paths.append(path)
                for key,child in value.items():walk(child,(*path,key),provenance or (not path and key not in baseline_keys))
            elif isinstance(value,list):
                for i,child in enumerate(value):walk(child,(*path,i),provenance)
            else:
                if provenance:provenance_paths.append(path)
                if isinstance(value,str):
                    if re.fullmatch('[0-9a-f]{64}',value):hash_paths.append(path)
                    try:parsed=json.loads(value)
                    except (ValueError,TypeError):parsed=None
                    if isinstance(parsed,dict) and isinstance(parsed.get('findings'),list):string_reply_paths.append(path)
        walk(original);self.assertTrue(reply_paths);self.assertGreaterEqual(len(hash_paths),4)
        variants=[]
        for path in reply_paths:
            x=copy.deepcopy(original);target=x
            for key in path:target=target[key]
            target['findings'][0]['description']='schema valid tamper';variants.append(x)
        for path in hash_paths:
            x=copy.deepcopy(original);target=x
            for key in path[:-1]:target=target[key]
            value=target[path[-1]];target[path[-1]]=('0' if value[0]!='0' else '1')+value[1:];variants.append(x)
        for path in string_reply_paths:
            x=copy.deepcopy(original);target=x
            for key in path[:-1]:target=target[key]
            parsed=json.loads(target[path[-1]]);parsed['findings'][0]['description']='tampered encoded raw'
            target[path[-1]]=json.dumps(parsed);variants.append(x)
        for path in provenance_paths:
            x=copy.deepcopy(original);target=x
            for key in path[:-1]:target=target[key]
            value=target[path[-1]]
            target[path[-1]]=not value if isinstance(value,bool) else value+1 if isinstance(value,(int,float)) else 'tampered'
            variants.append(x)
        for index,x in enumerate(variants):
            with self.subTest(variant=index):
                file.write_text(json.dumps(x));model,new=self.run_review(checkpoint=self.runs(),resume=old)
                self.assertTrue(model.calls,'modified recovery payload/hash must not be reused')

    def test_cross_task_recovered_checkpoint_is_rejected(self):
        body=SOURCE+''.join(f'# filler {i} '+('x'*42)+'\n' for i in range(100))
        _,self.snapshot,self.graph=self.scope({'app/a.py':body,'app/b.py':body})
        options=replace(OPTIONS,request_chars=14000,index_share=0,parent_share=0)
        plan=plan_review(self.snapshot,self.graph,SYSTEM,options)
        self.assertEqual(len(plan.tasks),2)
        old=self.runs();_,first=self.run_review(span_extra,checkpoint=old,options=options)
        self.assertFalse(first['errors'])
        files=sorted((old/'tasks').glob('*.json'));self.assertEqual(len(files),2)
        original_a=json.loads(files[0].read_text());original_b=json.loads(files[1].read_text())
        files[1].write_bytes(files[0].read_bytes())
        model,result=self.run_review(checkpoint=self.runs(),resume=old,options=options)
        self.assertEqual(len(model.calls),1)
        self.assertEqual(result['ledger']['summary']['cache_hits'],1)
        self.assertEqual(result['ledger']['summary']['checkpoints_not_reused']['binding_mismatch'],1)
        relabel=copy.deepcopy(original_a)
        for key in ('task_id','request_sha256'):relabel[key]=original_b[key]
        files[1].write_text(json.dumps(relabel))
        model,result=self.run_review(checkpoint=self.runs(),resume=old,options=options)
        self.assertEqual(len(model.calls),1,'relabelled outer binding must not authorize another task raw reply')

    def test_primary_aux_cannot_be_recovered_after_repair_transport_failure(self):
        def respond(brief,number):
            if number==2:raise LLMError('HTTP failed',category='http',status=500)
            r=good(brief);span_extra(r,brief);return r
        model,result=self.run_review(respond=respond)
        self.assertEqual(len(model.calls),2);self.assertTrue(result['errors']);self.assertFalse(result['findings'])

    def followup_case(self, quarantine=False):
        body=SOURCE+''.join(f'# filler {i} '+('x'*42)+'\n' for i in range(100))
        _,self.snapshot,self.graph=self.scope({'app/a.py':body,'app/b.py':body})
        options=replace(OPTIONS,request_chars=14000,index_share=0,parent_share=0,max_followups=1)
        self.assertEqual(len(plan_review(self.snapshot,self.graph,SYSTEM,options).tasks),2)
        def respond(brief,number):
            r=good(brief)
            if brief['kind']=='followup':
                span_extra(r,brief)
                if quarantine:r['requests'][0]['finding_ids']=['foreign-finding']
            elif any(x['path']=='app/a.py' for x in brief['owned_intervals']):
                r['requests']=[{'type':'span','path':'app/b.py','start_line':1,'end_line':5,
                    'reason':'Need downstream target with parent context','finding_ids':['f0']}]
            return r
        return options,respond

    def test_followup_recovery_keeps_parent_binding_and_resumes(self):
        options,respond=self.followup_case();old=self.runs()
        model,result=self.run_review(respond=respond,options=options,checkpoint=old)
        follow=[r for r in result['ledger']['tasks'] if r['kind']=='followup']
        self.assertEqual(len(follow),1);self.assertEqual(follow[0]['attempts'],2)
        self.assertEqual(follow[0]['recovery']['outcome'],'accepted');self.assertFalse(result['errors'])
        self.assertTrue(follow[0]['followup']['parent_finding_ids'])
        old_requests=result['ledger']['requests']
        model,resumed=self.run_review(options=options,checkpoint=self.runs(),resume=old)
        self.assertFalse(model.calls);self.assertEqual(result['findings'],resumed['findings'])
        self.assertEqual(old_requests,resumed['ledger']['requests'])

    def test_followup_quarantine_is_depth_limited_debt(self):
        options,respond=self.followup_case(quarantine=True)
        model,result=self.run_review(respond=respond,options=options)
        rows=[r for r in result['ledger']['requests'] if r['task_kind']=='followup']
        self.assertEqual(len(rows),1);self.assertTrue(rows[0]['quarantined'])
        self.assertEqual(rows[0]['status'],'invalid');self.assertFalse(result['errors'])
        self.assertTrue(result['ledger']['summary']['request_debt'])

if __name__=='__main__': unittest.main()
