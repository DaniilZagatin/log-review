"""A multiline call is strict evidence; repair must identify its full host-owned span."""
from copy import deepcopy
from pathlib import Path
import tempfile, unittest
from process_observability.logging_review import review_logging, _repair_details
from process_observability.map_input import MapView,RepoContext
from process_observability.source_tools import snapshot_repository
from test_map_first_logging import Scripted,operation,model_reply,CODE
from test_map_first_protocol import change

MULTI='''import logging
logger = logging.getLogger(__name__)
def apply(item):
    persist(item)
    logger.info(
        'done',
        extra={'id': item.id}
    )
'''

class ReferenceFeedbackControls(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
 def inputs(self,code=MULTI):
  (self.root/'app.py').write_text(code)
  view=MapView.from_dict({'schema_version':'2.0','sources':[{'id':'source-1'}],'scenario':{},'nodes':[operation(code,first=3,last=8 if code==MULTI else 5)],'edges':[],'run':{'id':'synthetic','status':'completed'}})
  return view,snapshot_repository(self.root,max_files=None,max_chars=None)
 def test_first_line_is_rejected_then_full_binding_span_repaired(self):
  view,snap=self.inputs()
  def callback(p,n):
   q=model_reply(p)
   if n==1:q['bindings'][0]['evidence'][0]['end_line']=5
   return q
  model=Scripted(callback);result=review_logging(view,snap,model)
  self.assertEqual(result['operations'][0]['state'],'C');self.assertEqual(len(model.calls),2)
  issue=next(i for i in model.calls[1]['repair_details']['issues'] if i['code']=='incomplete_candidate_call')
  self.assertEqual(issue['field'],'bindings[0].evidence');self.assertEqual(issue['expected_reference'],{'path':'app.py','start_line':5,'end_line':8})
  self.assertNotIn('reads',issue)
 def test_candidate_review_gets_exact_span_and_one_primary_repair(self):
  view,snap=self.inputs()
  def callback(p,n):
   q=model_reply(p,key='absent');site=p['candidates'][0]
   q['candidate_reviews']=[{'site_id':site['id'],'classification':'technical_log','reason':'Диагностическая запись другого действия',
    'evidence':[{'path':'app.py','start_line':5,'end_line':5 if n==1 else 8}]}]
   return q
  model=Scripted(callback);result=review_logging(view,snap,model)
  self.assertEqual(len(model.calls),2);self.assertEqual(result['operations'][0]['state'],'U')
  self.assertIn('candidate_reviews[0].evidence',model.calls[1]['repair_errors'][0]);self.assertIn('app.py:5-8',model.calls[1]['repair_errors'][0])
  issue=next(i for i in model.calls[1]['repair_details']['issues'] if i['code']=='incomplete_candidate_call')
  self.assertEqual(issue['expected_reference']['end_line'],8)
 def test_unchanged_bad_span_is_not_silently_extended_after_repair(self):
  view,snap=self.inputs()
  def callback(p,n):
   q=model_reply(p);q['bindings'][0]['evidence'][0]['end_line']=5;return q
  model=Scripted(callback);r=review_logging(view,snap,model)
  self.assertEqual(len(model.calls),2);self.assertEqual(r['operations'][0]['state'],'X');self.assertEqual(r['operations'][0]['bindings'],[])
 def test_unread_full_span_requires_read_instead_of_acceptance(self):
  view,snap=self.inputs();model=Scripted(lambda p,n:model_reply(p));review_logging(view,snap,model)
  p=model.calls[0]['packet'];ctx=RepoContext(snap,view);ctx.read('app.py',5,5)
  q=model_reply(p);q['bindings'][0]['evidence'][0]['end_line']=5
  issue=next(i for i in _repair_details(q,p,ctx)['issues'] if i['code']=='incomplete_candidate_call')
  self.assertEqual(issue['reads'],[{'path':'app.py','start_line':5,'end_line':8}])
  from process_observability.logging_review import _validate_reply
  from process_observability.logging_contracts import LoggingReviewError
  with self.assertRaises(LoggingReviewError):_validate_reply(model_reply(p),p,ctx)
 def test_error_visible_in_checkpoint_before_repair_is_dispatched(self):
  view,snap=self.inputs();saved=[]
  def callback(p,n):
   if n==2:
    self.assertTrue(saved);self.assertEqual(saved[-1]['trace'][-1]['event'],'validation_rejected')
    self.assertEqual(saved[-1]['trace'][-1]['attempt'],'primary');self.assertEqual(saved[-1]['operations'],[])
   q=model_reply(p)
   if n==1:q['bindings'][0]['evidence'][0]['end_line']=5
   return q
  r=review_logging(view,snap,Scripted(callback),on_checkpoint=saved.append);self.assertEqual(r['operations'][0]['state'],'C')
 def test_inspect_result_visible_before_next_call(self):
  view,snap=self.inputs();saved=[]
  def callback(p,n):
   if n==1:return {'action':'inspect','reads':[{'path':'app.py','start_line':1,'end_line':2}]}
   self.assertEqual(saved[-1]['trace'][-1]['event'],'inspection_completed')
   return model_reply(p)
  r=review_logging(view,snap,Scripted(callback),on_checkpoint=saved.append);self.assertEqual(r['operations'][0]['state'],'C')
 def test_proposal_feedback_exposes_bound_site_ids_without_creating_links(self):
  view,snap=self.inputs(CODE)
  def callback(p,n):
   q=model_reply(p,role='start',establishes=['occurrence'],changes=[change('new')])
   if n==2:q['changes'][0]['existing_site_ids']=[q['bindings'][0]['site_id']]
   return q
  model=Scripted(callback);r=review_logging(view,snap,model)
  self.assertEqual(len(model.calls),2);self.assertEqual(r['operations'][0]['state'],'P');self.assertEqual(len(r['operations'][0]['changes']),1)
  issue=next(i for i in model.calls[1]['repair_details']['issues'] if i['code']=='missing_existing_site_links')
  self.assertEqual(issue['field'],'changes[0].existing_site_ids');self.assertEqual(issue['bound_site_ids'],[model.calls[0]['packet']['candidates'][0]['id']])

if __name__=='__main__':unittest.main()
