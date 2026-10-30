"""Regression controls from live protocol failures, with synthetic independent source only."""
from copy import deepcopy
from pathlib import Path
import tempfile,unittest
from process_observability.logging_review import review_logging,ReviewOptions
from process_observability.map_input import MapView,RepoContext,MapInputError
from process_observability.source_tools import snapshot_repository
from test_map_first_logging import CODE,operation,model_reply,Scripted

class LiveProtocolTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);(self.root/'app.py').write_text(CODE)
 def review(self,callback,ops=None,**kwargs):
  view=MapView.from_dict({'schema_version':'2.0','scenario':{},'sources':[{'id':'source-1'}],
    'nodes':ops or [operation()],'edges':[],'run':{'id':'synthetic','status':'completed'}})
  model=Scripted(callback);return model,review_logging(view,snapshot_repository(self.root,max_files=None,max_chars=None),model,**kwargs)
 def test_outer_quote_margin_does_not_reject_real_host_text(self):
  def callback(p,n):
   q=model_reply(p);q['bindings'][0]['evidence'][0]['quote']=CODE.splitlines()[4].strip();return q
  model,result=self.review(callback)
  self.assertEqual(result['operations'][0]['state'],'C');self.assertEqual(len(model.calls),1)
  self.assertEqual(result['operations'][0]['bindings'][0]['evidence'][0]['quote'],CODE.splitlines()[4])
 def test_operation_catalog_exposes_foreign_ids_without_changing_the_map(self):
  ops=[operation(),operation(identifier='other',first=8,last=9)]
  model,result=self.review(lambda p,n:model_reply(p),ops)
  catalog=model.calls[0]['packet']['operation_catalog']
  self.assertEqual({x['id'] for x in catalog['operations']},{'save','other'})
  self.assertEqual(catalog['omitted'],0);self.assertEqual(result['map']['nodes'],ops)
 def test_related_id_repair_is_actionable_and_can_remove_optional_pointer(self):
  def callback(p,n):
   q=model_reply(p)
   if n==1:
    q['candidate_reviews']=[{'site_id':q['bindings'][0]['site_id'],'classification':'technical_log',
      'reason':'Диагностическая запись','related_operation_id':'Название вместо ID','evidence':deepcopy(q['bindings'][0]['evidence'])}]
   return q
  model,result=self.review(callback)
  self.assertEqual(result['operations'][0]['state'],'C');self.assertEqual(len(model.calls),2)
  issues=model.calls[1]['repair_details']['issues'];issue=next(i for i in issues if i['code']=='invalid_related_operation')
  self.assertEqual(issue['field'],'candidate_reviews[0].related_operation_id');self.assertIn('убери',issue['instruction'])
 def test_changed_quote_still_rejects_and_identifies_exact_reference(self):
  def callback(p,n):
   q=model_reply(p);q['bindings'][0]['evidence'][0]['quote']='logger.info("different event")';return q
  model,result=self.review(callback);self.assertEqual(result['operations'][0]['state'],'X');self.assertEqual(len(model.calls),2)
  self.assertTrue(any(i['code']=='invalid_source_reference' and i['field']=='bindings[0].evidence[0]' for i in model.calls[1]['repair_details']['issues']))
 def test_unread_and_wrong_range_citations_stay_invalid(self):
  view=MapView.from_dict({'schema_version':'2.0','scenario':{},'sources':[{'id':'source-1'}],'nodes':[operation()],'edges':[],'run':{'id':'s','status':'completed'}})
  ctx=RepoContext(snapshot_repository(self.root,max_files=None,max_chars=None),view)
  with self.assertRaises(MapInputError):ctx.verify_ref({'path':'app.py','start_line':5,'end_line':5})
  ctx.read('app.py',1,5)
  with self.assertRaises(MapInputError):ctx.verify_ref({'path':'app.py','start_line':5,'end_line':500})
  with self.assertRaises(MapInputError):ctx.verify_ref({'path':'../app.py','start_line':5,'end_line':5})

 def test_one_repair_sees_independent_reference_and_id_errors_together(self):
  def callback(p,n):
   q=model_reply(p)
   if n==1:
    q['bindings'][0]['evidence'][0]['quote']='wrong text'
    q['candidate_reviews']=[{'site_id':q['bindings'][0]['site_id'],'classification':'technical_log',
      'reason':'Диагностическая запись','related_operation_id':'Название вместо ID','evidence':[{'path':'app.py','start_line':5,'end_line':5}]}]
   return q
  model,result=self.review(callback)
  self.assertEqual(len(model.calls),2);self.assertEqual(result['operations'][0]['state'],'C')
  self.assertEqual({x['code'] for x in model.calls[1]['repair_details']['issues']}, {'invalid_source_reference','invalid_related_operation'})
 def test_malformed_site_id_stays_a_rejected_row_without_crashing_feedback(self):
  def callback(p,n):
   q=model_reply(p);q['bindings'][0]['site_id']=[];return q
  model,result=self.review(callback)
  self.assertEqual(result['operations'][0]['state'],'X');self.assertEqual(len(model.calls),2)
 def test_bounded_catalog_never_changes_the_accounting_denominator(self):
  ops=[operation(identifier='op-'+str(i)) for i in range(101)]
  model,result=self.review(lambda p,n:model_reply(p),ops,options=ReviewOptions(max_requests=1))
  catalog=model.calls[0]['packet']['operation_catalog']
  self.assertEqual(len(catalog['operations']),100);self.assertEqual(catalog['total'],101);self.assertEqual(catalog['omitted'],1)
  self.assertEqual(result['summary']['denominator'],101);self.assertEqual(len(result['operations']),101)

if __name__=='__main__':unittest.main()
