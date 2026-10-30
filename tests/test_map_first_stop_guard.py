"""Stop at a failed repair or an observer decision, preserving the whole immutable map denominator."""
from pathlib import Path
import tempfile,unittest,json,os
from unittest.mock import patch
from process_observability.logging_review import review_logging,ReviewOptions
from process_observability.map_input import MapView
from process_observability.source_tools import snapshot_repository
from process_observability.logging_report import validate_report
from process_observability.logging_contracts import LoggingReviewError
from test_map_first_logging import Scripted,operation,model_reply,CODE
from test_map_first_protocol import change

class StopGuardControls(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve();(self.root/'app.py').write_text(CODE)
  self.view=MapView.from_dict({'schema_version':'2.0','scenario':{},'sources':[{'id':'source-1'}],
   'nodes':[operation(),operation(identifier='refusal',first=6,last=7,outcome='refusal',effect='decision')],'edges':[],'run':{'id':'synthetic','status':'completed'}})
  self.snap=snapshot_repository(self.root,max_files=None,max_chars=None)
  self.options=ReviewOptions(stop_on_failed_repair=True)
 def test_failed_repair_stops_before_any_next_operation_call(self):
  def cb(p,n):q=model_reply(p);q['operation_id']='foreign';return q
  m=Scripted(cb);saved=[];r=review_logging(self.view,self.snap,m,options=self.options,on_checkpoint=saved.append)
  self.assertEqual(len(m.calls),2);self.assertEqual(r['stop_reason']['kind'],'repair_failed');self.assertFalse(r['interrupted'])
  self.assertEqual(r['summary']['counts'],{'C':0,'P':0,'U':0,'X':2});self.assertEqual(r['map'],self.view.original)
  self.assertTrue(any(b.get('stop_reason',{}).get('kind')=='repair_failed' for b in saved if b.get('stop_reason')))
  self.assertTrue(validate_report(r))
 def test_successful_repair_allows_the_next_operation(self):
  def cb(p,n):
   q=model_reply(p,key='denied',role='error') if p['operation']['id']=='refusal' else model_reply(p)
   if n==1:q['operation_id']='foreign'
   return q
  m=Scripted(cb);r=review_logging(self.view,self.snap,m,options=self.options)
  self.assertEqual(len(m.calls),3);self.assertNotIn('stop_reason',r);self.assertEqual(r['summary']['counts']['C'],2)
 def test_strict_guard_stops_even_when_optional_repair_could_be_isolated(self):
  m=Scripted(lambda p,n:model_reply(p,changes=[change('bad','enrich',existing=['foreign'])]))
  r=review_logging(self.view,self.snap,m,options=self.options)
  self.assertEqual(len(m.calls),2);self.assertEqual(r['stop_reason']['kind'],'repair_failed');self.assertEqual(r['operations'][0]['state'],'X')
  self.assertEqual(r['checkpoints'],[])
 def test_observer_can_stop_after_a_valid_review_before_the_next_call(self):
  m=Scripted(lambda p,n:model_reply(p));r=review_logging(self.view,self.snap,m,options=self.options,on_checkpoint=lambda book:False)
  self.assertEqual(len(m.calls),1);self.assertEqual(r['stop_reason']['kind'],'observer_stop')
  self.assertEqual([x['state'] for x in r['operations']],['C','X']);self.assertFalse(r['interrupted']);self.assertTrue(validate_report(r))
 def test_observer_stop_on_primary_does_not_send_repair(self):
  def cb(p,n):q=model_reply(p);q['operation_id']='foreign';return q
  m=Scripted(cb);r=review_logging(self.view,self.snap,m,options=self.options,on_checkpoint=lambda book:False)
  self.assertEqual(len(m.calls),1);self.assertEqual(r['stop_reason']['kind'],'observer_stop')
 def test_observer_stop_on_inspect_prevents_following_answer(self):
  m=Scripted(lambda p,n:{'action':'inspect','reads':[{'path':'app.py','start_line':1,'end_line':2}]})
  r=review_logging(self.view,self.snap,m,options=self.options,on_checkpoint=lambda book:False)
  self.assertEqual(len(m.calls),1);self.assertEqual(r['stop_reason']['kind'],'observer_stop');self.assertEqual(len(r['operations']),2)
 def test_observer_stop_after_revalidated_cache_does_not_call_model(self):
  first=review_logging(self.view,self.snap,Scripted(lambda p,n:model_reply(p)),options=self.options,on_checkpoint=lambda book:False)
  m=Scripted(lambda p,n:self.fail('must stop at the saved prefix'))
  r=review_logging(self.view,self.snap,m,options=self.options,resume=first,on_checkpoint=lambda book:False)
  self.assertEqual(m.calls,[]);self.assertEqual(r['summary']['denominator'],2);self.assertTrue(validate_report(r))
 def test_guard_option_is_boolean(self):
  for value in (1,'true',None):
   with self.assertRaises(LoggingReviewError):ReviewOptions(stop_on_failed_repair=value)
 def test_cli_flag_stops_at_two_wire_calls_and_exports_partial(self):
  from process_observability import cli,llm
  repo=self.root/'repo';repo.mkdir();(repo/'app.py').write_text(CODE)
  model_map=self.root/'map.json';model_map.write_text(json.dumps(self.view.original))
  profile=self.root/'profile.json';profile.write_text(json.dumps({'base_url':'https://fixture.invalid/v1','model':'synthetic',
   'api_key_env':'SYNTHETIC_KEY','max_output_tokens':128}))
  calls=[]
  def transport(url,payload,headers,tls):
   calls.append(payload)
   q={'action':'review','operation_id':'foreign','bindings':[],'missing':[],'review_tasks':[],'limitations':[],'changes':[]}
   return llm.TransportResponse(200,json.dumps({'choices':[{'message':{'content':json.dumps(q)}}]}).encode())
  out=self.root/'out'
  with patch.dict(os.environ,{'SYNTHETIC_KEY':'synthetic-placeholder'}),patch.object(llm,'urllib_transport',transport):
   code=cli.main(['review-logging','--map',str(model_map),'--repo',str(repo),'--config',str(profile),'--out',str(out),'--stop-on-failed-repair'])
  self.assertEqual(code,1);self.assertEqual(len(calls),2)
  r=json.loads((out/'logging_review.json').read_text());self.assertEqual(r['stop_reason']['kind'],'repair_failed')
  self.assertTrue((out/'report.html').is_file());self.assertEqual(r['summary']['denominator'],2)

if __name__=='__main__':unittest.main()
