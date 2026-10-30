import hashlib,json,unittest,warnings
from process_observability.source_tools import RepoSnapshot,SourceFile
from process_observability.redaction import redact_text
from process_observability.declaration_index import build_declaration_index,build_command_units,DeclarationError

def snapshot(text):
 return RepoSnapshot({'app.py':SourceFile('app.py',hashlib.sha256(text.encode()).hexdigest(),redact_text(text),text)}, {})
def candidate(s, name, first,last,symbol):
 return {'id':name,'path':'app.py','start_line':first,'end_line':last,'symbol':symbol,'file_sha256':s.files['app.py'].sha256,'finding_ids':[name+'f'],'findings':[{'id':name+'f','description':'finding'}]}

class IndexAcceptance(unittest.TestCase):
 def test_fragment_members_and_distinct_commands(self):
  s=snapshot('class Basket:\n    def add(self):\n        validate()\n        save()\n    def delete(self):\n        remove()\n')
  cat={'snapshot_id':s.snapshot_id,'candidates':[candidate(s,'a',3,3,'Basket.add'),candidate(s,'b',4,4,'Basket.add'),candidate(s,'c',6,6,'Basket.delete')],'context_anchors':[]}
  record=build_command_units(cat,build_declaration_index(s)); units=record['units']
  self.assertEqual(sorted(sorted(u['candidate_ids']) for u in units),[['a','b'],['c']])
  self.assertEqual(len({u['id'] for u in units}),2)
 def test_wrong_symbol_does_not_merge_and_cross_child_is_tentative(self):
  s=snapshot('class Basket:\n    def add(self):\n        save()\n    def delete(self):\n        remove()\n')
  cat={'snapshot_id':s.snapshot_id,'candidates':[candidate(s,'a',3,3,'Basket.add'),candidate(s,'wrong',3,3,'Basket.delete'),candidate(s,'cross',3,5,'Basket')],'context_anchors':[]}
  record=build_command_units(cat,build_declaration_index(s)); self.assertEqual(len(record['units']),2)
  by_member={cid:u for u in record['units'] for cid in u['candidate_ids']}
  self.assertEqual(by_member['wrong']['declaration']['symbol'],'Basket.add')
  self.assertEqual(by_member['wrong']['id'],by_member['a']['id'])
  self.assertEqual(by_member['wrong']['members'][1]['symbol_check'],'stale_symbol')
  self.assertEqual(by_member['cross']['status'],'tentative')
 def test_original_parse_and_secret_not_serialized(self):
  s=snapshot('class Basket:\n    password = "very-private-value"\n    def add(self):\n        return True\n')
  ix=build_declaration_index(s); self.assertNotIn('very-private-value',json.dumps(ix)); self.assertEqual(len(ix['declarations']),2)
 def test_hash_mismatch_and_foreign_snapshot_rejected(self):
  s=snapshot('def add():\n    save()\n'); ix=build_declaration_index(s); c=candidate(s,'a',2,2,'add'); c['file_sha256']='0'*64
  with self.assertRaises(DeclarationError):build_command_units({'snapshot_id':s.snapshot_id,'candidates':[c],'context_anchors':[]},ix)
  with self.assertRaises(DeclarationError):build_command_units({'snapshot_id':'foreign','candidates':[],'context_anchors':[]},ix)
 def test_same_names_nested_and_repeated_have_distinct_ids(self):
  s=snapshot('def outer():\n    def action():\n        pass\n    action()\ndef action():\n    pass\ndef action():\n    pass\n')
  ix=build_declaration_index(s); funcs=[d for d in ix['declarations'] if d['kind']=='function']; self.assertEqual(len(funcs),4);self.assertEqual(len({d['id'] for d in funcs}),4)

 def test_decorators_belong_to_the_declaration(self):
  s=snapshot('@handler\ndef add():\n    save()\n')
  ix=build_declaration_index(s); d=ix['declarations'][0]
  self.assertEqual((d['start_line'],d['definition_line'],d['end_line']),(1,2,3))
  cat={'snapshot_id':s.snapshot_id,'candidates':[candidate(s,'a',1,3,'add')],'context_anchors':[]}
  self.assertEqual(build_command_units(cat,ix)['units'][0]['status'],'verified')
 def test_checksum_range_and_source_revision_are_strict(self):
  s=snapshot('def add():\n    save()\n'); ix=build_declaration_index(s)
  tampered=json.loads(json.dumps(ix));tampered['declarations'][0]['symbol']='delete'
  with self.assertRaises(DeclarationError):build_command_units({'snapshot_id':s.snapshot_id,'candidates':[]},tampered)
  cat={'snapshot_id':s.snapshot_id,'candidates':[candidate(s,'bad',1,8,'add')]}
  with self.assertRaises(DeclarationError):build_command_units(cat,ix)
  original=s.files['app.py']; s.files['app.py']=SourceFile('app.py','0'*64,original.text,original._raw_text)
  with self.assertRaises(DeclarationError):build_declaration_index(s)
 def test_no_same_owner_call_invention_and_ambiguous_targets_unresolved(self):
  s=snapshot('def helper():\n    pass\ndef helper():\n    pass\nclass Basket:\n    def add(self):\n        helper()\n        self.validate()\n        return 1\n    def validate(self):\n        pass\n    def delete(self):\n        pass\n')
  ix=build_declaration_index(s); calls=ix['calls']
  self.assertEqual(len(calls),2)
  helper=next(c for c in calls if c['symbol']=='helper'); self.assertEqual(helper['status'],'unresolved');self.assertIsNone(helper['callee_id'])
  member=next(c for c in calls if c['symbol']=='Basket.validate');self.assertEqual(member['status'],'resolved')
  delete=next(d for d in ix['declarations'] if d['symbol']=='Basket.delete');self.assertNotIn(delete['id'],[c['callee_id'] for c in calls])
 def test_parse_failure_is_explicit_not_an_empty_success(self):
  s=snapshot('def invalid(:\n    pass\n'); ix=build_declaration_index(s)
  self.assertEqual(ix['files']['app.py']['status'],'parse_failed');self.assertEqual(ix['declarations'],[])
  units=build_command_units({'snapshot_id':s.snapshot_id,'candidates':[candidate(s,'x',1,2,'invalid')]},ix)
  self.assertEqual(units['units'][0]['status'],'tentative');self.assertEqual(units['candidate_count'],1)

 def test_parser_warnings_cannot_expose_original_source(self):
  s=snapshot("def action():\n    value = '\\z'\n")
  with warnings.catch_warnings(record=True) as captured:
   warnings.simplefilter('always')
   build_declaration_index(s)
  self.assertEqual(captured,[])

if __name__=='__main__':unittest.main()
