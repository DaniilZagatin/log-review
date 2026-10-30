"""Known site IDs select real delivered source; response repair edits its own public previous JSON."""
from copy import deepcopy
from pathlib import Path
import hashlib,json,tempfile,unittest
from process_observability.logging_review import review_logging,ReviewOptions,_validate_reply
from process_observability.logging_contracts import LoggingReviewError,digest
from process_observability.map_input import MapView,RepoContext
from process_observability.source_tools import snapshot_repository
from test_map_first_logging import CODE,operation,model_reply,Scripted
from test_map_first_reference_feedback import MULTI
from test_map_first_wrapper_review import FILES,GATEWAY,CONTRACT,node,bind,reply
from test_map_first_takeover import BASE,PORT

class HostReferenceControls(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
 def inputs(self,code=MULTI,ops=None,calls=()):
  (self.root/'app.py').write_text(code)
  view=MapView.from_dict({'schema_version':'2.0','scenario':{},'sources':[{'id':'source-1'}],
   'nodes':ops or [operation(code,first=3,last=8 if code==MULTI else 5)],'edges':[],'run':{'id':'synthetic','status':'completed'}})
  return view,snapshot_repository(self.root,max_files=None,max_chars=None,logging_calls=calls)
 def test_multiline_site_reference_is_host_extracted(self):
  view,snap=self.inputs()
  def cb(p,n):q=model_reply(p);del q['bindings'][0]['evidence'];return q
  m=Scripted(cb);r=review_logging(view,snap,m);b=r['operations'][0]['bindings'][0]
  self.assertEqual(len(m.calls),1);self.assertEqual(r['operations'][0]['state'],'C')
  self.assertEqual((b['evidence'][0]['start_line'],b['evidence'][0]['end_line']),(5,8));self.assertEqual(b['evidence'][0]['quote'],'\n'.join(MULTI.splitlines()[4:8]))
  self.assertEqual(b['evidence_origin'],'host_site_id')
 def test_disposition_site_reference_is_host_extracted_without_creating_coverage(self):
  view,snap=self.inputs()
  def cb(p,n):
   q=model_reply(p,key='absent');q['candidate_reviews']=[{'site_id':p['candidates'][0]['id'],'classification':'technical_log','reason':'Запись не отражает результат бизнес-действия'}];return q
  r=review_logging(view,snap,Scripted(cb));row=r['operations'][0]
  self.assertEqual(row['state'],'U');self.assertEqual(row['bindings'],[]);self.assertEqual(row['candidate_reviews'][0]['evidence'][0]['end_line'],8)
 def test_empty_evidence_uses_the_same_verified_host_range(self):
  for value in (None,[]):
   with self.subTest(value=value):
    view,snap=self.inputs()
    def cb(p,n):q=model_reply(p);q['bindings'][0]['evidence']=value;return q
    m=Scripted(cb);r=review_logging(view,snap,m);b=r['operations'][0]['bindings'][0]
    self.assertEqual(len(m.calls),1);self.assertEqual(b['evidence_origin'],'host_site_id');self.assertEqual(b['evidence'][0]['end_line'],8)
    def disposition(p,n):
     q=model_reply(p,key='absent');q['candidate_reviews']=[{'site_id':p['candidates'][0]['id'],'classification':'technical_log','reason':'Нет результата операции','evidence':value}];return q
    r=review_logging(view,snap,Scripted(disposition));self.assertEqual(r['operations'][0]['state'],'U');self.assertEqual(r['operations'][0]['candidate_reviews'][0]['evidence_origin'],'host_site_id')
 def test_missing_read_cannot_be_supplied_by_id_alone(self):
  view,snap=self.inputs();m=Scripted(lambda p,n:model_reply(p));review_logging(view,snap,m)
  ctx=RepoContext(snap,view);ctx.read('app.py',5,5);q=model_reply(m.calls[0]['packet']);del q['bindings'][0]['evidence']
  with self.assertRaises(LoggingReviewError):_validate_reply(q,m.calls[0]['packet'],ctx)
 def test_unknown_site_id_stays_unknown(self):
  view,snap=self.inputs()
  def cb(p,n):q=model_reply(p);q['bindings'][0]['site_id']='invented';q['bindings'][0].pop('evidence');return q
  r=review_logging(view,snap,Scripted(cb),options=ReviewOptions(stop_on_failed_repair=True));self.assertEqual(r['operations'][0]['state'],'X')
 def test_explicit_wrong_quote_or_partial_range_is_not_silently_replaced(self):
  for defect in ('quote','range'):
   with self.subTest(defect=defect):
    view,snap=self.inputs()
    def cb(p,n):
     q=model_reply(p)
     if defect=='quote':q['bindings'][0]['evidence'][0]['quote']='different'
     else:q['bindings'][0]['evidence'][0]['end_line']=5
     return q
    r=review_logging(view,snap,Scripted(cb));self.assertEqual(r['operations'][0]['state'],'X')
 def test_foreign_read_site_still_needs_a_real_invocation(self):
  view,snap=self.inputs(CODE)
  def cb(p,n):
   if n==1:return {'action':'inspect','reads':[{'path':'app.py','start_line':8,'end_line':9}]}
   q=model_reply(p);q['bindings'][0]['site_id']=next(s['id'] for s in p['candidates'] if s['start_line']==9);q['bindings'][0].pop('evidence');return q
  r=review_logging(view,snap,Scripted(cb));self.assertEqual(r['operations'][0]['state'],'X')
 def test_arbitrary_publish_is_not_a_log_when_evidence_is_omitted(self):
  code='def apply(item):\n    publisher.publish(item)\n';op=operation(code,first=1,last=2,outcome='unknown');view,snap=self.inputs(code,[op],['publisher.publish'])
  def cb(p,n):
   q=model_reply(p,key='absent');q['bindings']=[{'site_id':p['candidates'][0]['id'],'role':'result','meaning':'Изменение результата','conditions':'После','correlation':'По элементу','establishes':['occurrence','result']}];return q
  r=review_logging(view,snap,Scripted(cb));self.assertEqual(r['operations'][0]['state'],'X')
 def java(self,source_adapter):
  files=deepcopy(FILES)
  if source_adapter:
   files[PORT]='package com.example.journal;\npublic interface RecordingPort {\n /** Записать текст в журнал. */\n void write(String text, int level);\n}\n'
   files[BASE]=files[BASE].replace('class BaseJournal {','class BaseJournal implements RecordingPort {').replace('protected void write','public void write')
  for path,code in files.items():
   target=self.root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(code)
  view=MapView.from_dict({'schema_version':'2.0','sources':[{'id':'source-1'}],'scenario':{},'nodes':[node(GATEWAY,files[GATEWAY],10,15)],'edges':[],'run':{'id':'synthetic','status':'completed'}})
  def cb(p,n):
   values=[]
   for line,role,moment in ((11,'request','before_action'),(13,'result','after_confirmation')):
    b=bind(p,line,role=role,moment=moment);b.pop('evidence');b.pop('emission_evidence',None)
    if source_adapter:
     site=next(s for s in p['candidates'] if s['id']==b['site_id']);plan=site['logging_chain']['adapter_candidates'][0]
     b['source_contract']={'candidate_id':plan['contract_basis']['candidate_id'],'meaning':'Интерфейс и реализация принимают сообщение и передают его в журнал'}
    values.append(b)
   return reply(p,values)
  return review_logging(view,snapshot_repository(self.root,max_files=None,max_chars=None,logging_calls=['journal.record']),Scripted(cb),logging_contracts=[] if source_adapter else CONTRACT)
 def test_confirmed_wrapper_chain_is_host_extracted(self):
  r=self.java(False);self.assertEqual(r['operations'][0]['state'],'C');self.assertEqual(r['operations'][0]['bindings'][0]['emission_evidence_origin'],'host_logging_chain')
 def test_unread_confirmed_chain_has_an_exact_inspect_target(self):
  from process_observability.logging_review import _Navigator,_chain_plan,_plan_spans,_repair_details
  from process_observability.log_discovery import discover_logging
  r=self.java(False);view=MapView.from_dict(r['map']);snap=snapshot_repository(self.root,max_files=None,max_chars=None,logging_calls=['journal.record'])
  ctx=RepoContext(snap,view);ctx.logging_contracts=CONTRACT
  inv=discover_logging(snap,call_names=snap.logging_calls,max_sites=None,max_per_file=None,max_hints=None,include_columns=True,exclude_declarations=True)
  p=_Navigator(ctx,inv,ReviewOptions()).initial(view.operations[0]);raw=r['checkpoints'][0]['reply']
  site=next(s for s in p['candidates'] if s['id']==raw['bindings'][0]['site_id']);plan=_chain_plan(ctx,site)
  path,first,last=next(span for span in _plan_spans(plan,ctx.snapshot.files) if span[0]!=GATEWAY)
  ctx.shown=[s for s in ctx.shown if s['path']!=path]
  with self.assertRaisesRegex(LoggingReviewError,path.replace('.','\\.')+':'):_validate_reply(raw,p,ctx)
  details=_repair_details(raw,p,ctx);issues=[i for i in details['issues'] if i['code']=='unread_logging_chain']
  self.assertTrue(issues);self.assertIn({'path':path,'start_line':first,'end_line':last},issues[0]['reads'])
 def test_selected_source_adapter_keeps_model_authority_and_full_witnesses(self):
  r=self.java(True);b=r['operations'][0]['bindings'][0];self.assertEqual(r['operations'][0]['state'],'C')
  self.assertEqual(b['chain']['source_contract']['authority'],'model_interpretation_of_source');self.assertIn(PORT,{ref['path'] for ref in b['emission_evidence']})
 def test_repair_contains_the_actual_public_rejected_reply_and_request_hash(self):
  view,snap=self.inputs();bad=[]
  def cb(p,n):
   q=model_reply(p)
   if n==1:q['operation_id']='foreign';q['limitations']=['<thinking>PRIVATE_SYNTHETIC_REASON</thinking>'];bad.append(deepcopy(q))
   return q
  m=Scripted(cb);r=review_logging(view,snap,m);old=m.calls[1]['rejected_response']
  self.assertEqual(old['request_sha256'],r['trace'][0]['request_sha256']);self.assertEqual(old['reply']['operation_id'],'foreign')
  self.assertEqual(old['response_sha256'],digest(old['reply']));self.assertNotIn('PRIVATE_SYNTHETIC_REASON',json.dumps(m.calls))
  self.assertNotIn('rejected_response',m.calls[0]);self.assertEqual(len(m.calls),2)
 def test_repair_cannot_reuse_a_reply_from_a_previous_operation(self):
  ops=[operation(CODE),operation(CODE,identifier='refusal',first=6,last=7,outcome='refusal',effect='decision')];view,snap=self.inputs(CODE,ops);seen={}
  def cb(p,n):
   key=p['operation']['id'];seen[key]=seen.get(key,0)+1
   q=model_reply(p,key='denied',role='error') if key=='refusal' else model_reply(p)
   if seen[key]==1:q['operation_id']='foreign-'+key
   return q
  m=Scripted(cb);review_logging(view,snap,m)
  self.assertEqual(m.calls[1]['rejected_response']['reply']['operation_id'],'foreign-save');self.assertNotIn('rejected_response',m.calls[2]);self.assertEqual(m.calls[3]['rejected_response']['reply']['operation_id'],'foreign-refusal')
 def test_unproductive_json_repair_error_keeps_a_typed_model_category(self):
  from process_observability.llm import LLMError
  view,snap=self.inputs()
  def cb(p,n):
   if n==2:raise LLMError('synthetic',category='missing_content',status=200)
   q=model_reply(p);q['operation_id']='foreign';return q
  r=review_logging(view,snap,Scripted(cb),options=ReviewOptions(stop_on_failed_repair=True))
  self.assertIn('missing_content',r['stop_reason']['errors'][0]);self.assertIn('HTTP 200',r['stop_reason']['errors'][0])
 def test_not_log_still_requires_a_distinct_implementation(self):
  code='def apply(item):\n    publisher.publish(item)\n';op=operation(code,first=1,last=2,outcome='unknown');view,snap=self.inputs(code,[op],['publisher.publish'])
  def cb(p,n):
   q=model_reply(p,key='absent');q['candidate_reviews']=[{'site_id':p['candidates'][0]['id'],'classification':'not_log','reason':'Предположение без отдельной реализации'}];return q
  r=review_logging(view,snap,Scripted(cb));self.assertEqual(r['operations'][0]['state'],'X')
 def test_repair_after_missing_json_does_not_fabricate_a_previous_object(self):
  from process_observability.llm import LLMError
  view,snap=self.inputs()
  def cb(p,n):
   if n==1:raise LLMError('synthetic',category='invalid_json')
   return model_reply(p)
  m=Scripted(cb);r=review_logging(view,snap,m)
  old=m.calls[1]['rejected_response'];self.assertFalse(old['available']);self.assertIsNone(old['reply']);self.assertIsNone(old['response_sha256']);self.assertEqual(r['operations'][0]['state'],'C')
 def test_a_productive_inspect_clears_the_prior_repair_object(self):
  view,snap=self.inputs()
  def cb(p,n):
   if n==1:q=model_reply(p);q['operation_id']='foreign';return q
   if n==2:return {'action':'inspect','reads':[{'path':'app.py','start_line':1,'end_line':2}]}
   return model_reply(p)
  m=Scripted(cb);r=review_logging(view,snap,m)
  self.assertIn('rejected_response',m.calls[1]);self.assertNotIn('rejected_response',m.calls[2]);self.assertEqual(r['operations'][0]['state'],'C')
 def test_oversized_repair_stops_without_truncating_the_old_json(self):
  ops=[operation(CODE),operation(CODE,identifier='other',first=8,last=9)];view,snap=self.inputs(CODE,ops)
  def cb(p,n):q=model_reply(p);q['extra_bad_field']='a'*50000;return q
  m=Scripted(cb);r=review_logging(view,snap,m,options=ReviewOptions(request_chars=20000,stop_on_failed_repair=True))
  self.assertEqual(len(m.calls),1);self.assertEqual(r['stop_reason']['kind'],'repair_request_limit');self.assertEqual(r['summary']['denominator'],2)
  self.assertEqual(len(next(t for t in r['trace'] if t['event']=='validation_rejected')['reply']['extra_bad_field']),50000)
 def test_host_references_allow_independently_anchored_shared_source(self):
  view,snap=self.inputs(CODE,[operation(CODE),operation(CODE,identifier='second')])
  def cb(p,n):q=model_reply(p);q['bindings'][0].pop('evidence');return q
  r=review_logging(view,snap,Scripted(cb));self.assertEqual([x['state'] for x in r['operations']],['C','C']);self.assertEqual(r['summary']['denominator'],2)
  self.assertEqual(r['reconcile_policy'],'independent-source-uses/1')
 def test_host_selected_refs_revalidate_on_cache_reuse(self):
  view,snap=self.inputs()
  def cb(p,n):q=model_reply(p);q['bindings'][0].pop('evidence');return q
  first=review_logging(view,snap,Scripted(cb))
  m=Scripted(lambda p,n:self.fail('same validated ID and sources must reuse'))
  second=review_logging(view,snap,m,resume=first)
  self.assertEqual(m.calls,[]);self.assertEqual(second['operations'],first['operations'])
 def test_unread_selected_interface_is_not_completed_from_the_index(self):
  from process_observability.logging_review import _Navigator
  from process_observability.log_discovery import discover_logging
  r=self.java(True);view=MapView.from_dict(r['map']);snap=snapshot_repository(self.root,max_files=None,max_chars=None,logging_calls=['journal.record'])
  ctx=RepoContext(snap,view);ctx.logging_contracts=[]
  inv=discover_logging(snap,call_names=snap.logging_calls,max_sites=None,max_per_file=None,max_hints=None,include_columns=True,exclude_declarations=True)
  p=_Navigator(ctx,inv,ReviewOptions()).initial(view.operations[0]);ctx.shown=[s for s in ctx.shown if s['path']!=PORT]
  with self.assertRaises(LoggingReviewError):_validate_reply(r['checkpoints'][0]['reply'],p,ctx)

if __name__=='__main__':unittest.main()
