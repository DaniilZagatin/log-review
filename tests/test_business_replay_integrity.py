"""Own cache-boundary negative controls; no actual run/target/model data."""
import hashlib, json, tempfile, unittest
from pathlib import Path
from process_observability.action_refinement import _digest
from process_observability.business_replay import prior_atomic_reply, _one


class ReplayExactBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name).resolve()
        self.system='TASK: map-atomic\nOwn synthetic request'
        self.user={'units':[{'number':1,'symbol':'own_synthetic_unit'}]}
        self.request_hash=hashlib.sha256((self.system+'\n'+json.dumps(self.user,ensure_ascii=False,sort_keys=True)).encode()).hexdigest()
        self.proposal={'operations':[],'new_groups':[],'unit_accounting':[],'event_mappings':[],
                       'operation_transitions':[],'unknowns':['Own synthetic reply']}
        self.fields={'task':'map-atomic','mode':'atomic','api_request_sha256':self.request_hash,'shape_sha256':'c'*64,
                     'business_model_sha256':'a'*64,'snapshot_id':'b'*64,'catalog_sha256':'d'*64,'units_sha256':'e'*64,
                     'coverage_sha256':'f'*64,'validator':'direct-atomic-validator/7'}
        answer={'attempt':1,'api_request_sha256':self.request_hash,'response_sha256':_digest(self.proposal)}
        record={'schema':'atomic-map/1','catalog':{'snapshot_id':'b'*64},
                'settings':{'validator':'direct-atomic-validator/7'},
                'requests':[{'id':'adq-own','kind':'atomic','api_request_sha256':self.request_hash,
                             'checkpoint':{'fields':dict(self.fields)},'outcome':'accepted','accepted':dict(answer),
                             'attempts':[{**answer,'outcome':'accepted'}]}]}
        (self.root/'atomic_map.json').write_text(json.dumps(record))
        self.request={'seq':1,'event':'model.request','data':{'role':'business','stage':'map',
                      'task':'map-atomic','attempt':1,'system':self.system,'user':self.user,
                      'api_request_sha256':self.request_hash}}
        self.response={'seq':2,'event':'model.response','data':{'role':'business','stage':'map',
                       'task':'map-atomic','attempt':1,'proposal':self.proposal}}
        self.write(self.request,self.response)
        self.assertIsNotNone(self.hit())

    def write(self,*events):
        (self.root/'trace.jsonl').write_text(''.join(json.dumps(event,ensure_ascii=False)+'\n' for event in events))

    def hit(self):
        return prior_atomic_reply(self.root,api_hash=self.request_hash,
                                  fields={**self.fields,'validator':'direct-atomic-validator/9'})

    def test_changed_request_text_with_unchanged_declared_hash_is_a_miss(self):
        self.request['data']['user']={'units':[{'number':1,'symbol':'changed_synthetic_unit'}]}
        self.write(self.request,self.response)
        self.assertIsNone(self.hit())

    def test_overlapping_unanswered_requests_are_ambiguous_even_if_keys_match(self):
        second=json.loads(json.dumps(self.request));second['seq']=2;self.response['seq']=3
        self.write(self.request,second,self.response)
        self.assertIsNone(self.hit())

    def test_unhashable_model_metadata_is_a_miss(self):
        p=self.root/'atomic_map.json';record=json.loads(p.read_text())
        record['requests'][0]['checkpoint']['fields']['business_model_sha256']={'invalid':'object'}
        p.write_text(json.dumps(record))
        self.assertIsNone(self.hit())

    def test_unhashable_request_hash_is_a_miss(self):
        self.request['data']['api_request_sha256']={'invalid':'object'}
        self.write(self.request,self.response)
        self.assertIsNone(self.hit())

    def test_an_unanswered_later_request_makes_the_trace_incomplete(self):
        later=json.loads(json.dumps(self.request));later['seq']=3
        self.write(self.request,self.response,later)
        self.assertIsNone(self.hit())

    def test_an_unrelated_failure_cannot_clear_a_pending_request(self):
        failure={'seq':2,'event':'model.failed','data':{**self.request['data'],'task':'another-task'}}
        later=json.loads(json.dumps(self.request));later['seq']=3;self.response['seq']=4
        self.write(self.request,failure,later,self.response)
        self.assertIsNone(self.hit())

    def test_a_code_led_record_is_not_an_earlier_record(self):
        p=self.root/'atomic_map.json';record=json.loads(p.read_text())
        record['schema']='atomic-map/2'
        p.write_text(json.dumps(record))
        self.assertIsNone(self.hit())

    def test_real_failure_emitter_without_attempt_allows_the_later_repair_pair(self):
        failure={'seq':2,'event':'model.failed','data':{'role':'business','stage':'map',
                 'task':'map-atomic','type':'LLMError','category':'invalid_json','status':200}}
        repair=json.loads(json.dumps(self.request));repair['seq']=3;repair['data']['attempt']=2
        repair['data']['system']+='\nRepair the invalid JSON.'
        repair_hash=hashlib.sha256((repair['data']['system']+'\n'+json.dumps(
            repair['data']['user'],ensure_ascii=False,sort_keys=True)).encode()).hexdigest()
        repair['data']['api_request_sha256']=repair_hash
        self.response['seq']=4;self.response['data']['attempt']=2
        self.write(self.request,failure,repair,self.response)
        found=_one(self.root,repair_hash,'map-atomic',2)
        self.assertIsNotNone(found)
        self.assertEqual(found[1]['request_seq'],3)
