"""Product boundary tests: real source snapshot, consumer and exporter; no network."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from process_observability.pipeline import run_product, export_product, RunBudget, BelkaClient, RunHalt
from process_observability.llm import ModelConfig, OpenAICompatibleModel, TransportResponse
from test_map_first_logging import CODE, operation, Scripted, model_reply

class ProductPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root/'repo'; self.repo.mkdir()
        (self.repo/'app.py').write_text(CODE, encoding='utf-8', newline='\n')
        self.map = self.root/'scenario_map.json'
        self.data = {'schema_version':'2.0','scenario':{'title':'Обращения','entry_operation_ids':['save'],
            'terminal_operation_ids':['save'],'instance_unit':'Обращение'}, 'sources':[{'id':'source-1'}],
            'nodes':[operation()], 'edges':[], 'run':{'status':'completed','id':'fixture','stop_kind':'finish'}}
        self.map.write_text(json.dumps(self.data, ensure_ascii=False), encoding='utf-8')
    def args(self, **kw):
        args = dict(source=[str(self.repo)], map=str(self.map), out=str(self.root/'out'), inventory_only=True,
                    resume_from=None, scenario_text=None, process_file=None, config=None, max_requests=10,
                    max_steps=10,max_seconds=60,model_timeout=10,request_chars=180000,max_navigation_turns=4,
                    max_context_lines=1600,log_call=[])
        args.update(kw); return SimpleNamespace(**args)
    def test_inventory_export_and_source_identity(self):
        r = run_product(self.args())
        self.assertEqual(r['status'], 'partial')
        folder = Path(r['report']).parent
        manifest = json.loads((folder/'product_run.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['requests'], [])
        self.assertEqual(json.loads((folder/'scenario_map.json').read_text(encoding='utf-8')), self.data)
        for label in ('Карта','Логирование','Доработки','Метрики'):
            self.assertIn(label, (folder/'report.html').read_text(encoding='utf-8'))
        exported = export_product(folder,self.root/'export')
        self.assertTrue(Path(exported['report']).is_file())
        (folder/'metric_catalog.json').write_text('{}',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'checksum'):
            export_product(folder,self.root/'tampered')
    def test_resume_reuses_map_and_detects_source_change(self):
        run_product(self.args())
        with patch('pko.scenario_map.runner.run_scenario', side_effect=AssertionError('No map rerun')):
            result = run_product(self.args(map=None,resume_from=str(self.root/'out'),out=str(self.root/'resumed')))
        self.assertEqual(result['status'],'partial')
        manifest = json.loads(Path(result['manifest']).read_text(encoding='utf-8'))
        self.assertTrue(manifest['stages']['map']['reused'])
        self.assertEqual(manifest['attempt'],2)
        (self.repo/'app.py').write_text(CODE+'\n# changed',encoding='utf-8')
        changed = run_product(self.args(map=None,resume_from=str(self.root/'out'),out=str(self.root/'changed')))
        self.assertEqual(changed['status'],'failed')
    def test_semantic_logging_uses_original_ids(self):
        class FullModel(Scripted):
            def complete_json(self, system, user):
                packet = json.loads(user)
                if 'candidates' in packet:
                    return {'metrics':packet['candidates']}
                return super().complete_json(system,user)
        fake = FullModel(lambda p,n:model_reply(p))
        result = run_product(self.args(inventory_only=False), logging_model=fake)
        self.assertEqual(result['status'],'completed')
        log = json.loads((self.root/'out/logging_review.json').read_text(encoding='utf-8'))
        self.assertEqual(log['map'],self.data)
        self.assertEqual(log['operations'][0]['operation_id'],'save')
        self.assertEqual(len(fake.calls),1)
    def test_budget_reserved_before_wire_and_failed_save_blocks_call(self):
        from process_observability.llm import LLMError
        m={'requests':[]}; saved=[]
        budget=RunBudget(1,60,m,lambda:saved.append(len(m['requests'])))
        budget.reserve('map')
        with self.assertRaises(RunHalt): budget.reserve('logging')
        self.assertEqual(saved,[1])
        with self.assertRaises(RunHalt):
            RunBudget(1,60,{'requests':[]},lambda: (_ for _ in ()).throw(OSError('disk'))).reserve('map')
    def test_belka_bridge_preserves_tools_discards_reasoning(self):
        envelope={'choices':[{'message':{'content':None,'reasoning_content':'private',
            'tool_calls':[{'id':'t1','type':'function','function':{'name':'inspect_map','arguments':'{}'}}]},
            'finish_reason':'tool_calls'}],'usage':{'total_tokens':5}}
        model=OpenAICompatibleModel(ModelConfig('http://fixture.invalid','fixture'),transport=lambda *a:TransportResponse(200,json.dumps(envelope).encode()))
        answer=BelkaClient(model).chat([{'role':'user','content':'x'}],tools=[{'type':'function'}])
        self.assertEqual(answer.tool_calls[0]['id'],'t1')
        self.assertEqual(answer.reasoning_content,'')
    def test_resume_rejects_changed_map_before_model_or_output(self):
        run_product(self.args())
        target=self.root/'out/scenario_map.json'
        changed=json.loads(target.read_text(encoding='utf-8')); changed['nodes'][0]['name']='Changed'
        target.write_text(json.dumps(changed),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'checksum'):
            run_product(self.args(map=None,resume_from=str(self.root/'out'),out=str(self.root/'resume')))
        self.assertFalse((self.root/'resume').exists())

    def test_contracts_and_log_call_survive_default_resume(self):
        declaration={'schema_version':'logging-contracts/1','contracts':[{'callee':'Vendor.record','basis':'Declared test API'}]}
        (self.root/'logging_contracts.json').write_text(json.dumps(declaration),encoding='utf-8')
        run_product(self.args(log_call=['Vendor.record']))
        result=run_product(self.args(map=None,resume_from=str(self.root/'out'),out=str(self.root/'resume')))
        manifest=json.loads(Path(result['manifest']).read_text(encoding='utf-8'))
        self.assertEqual(manifest['log_calls'],['Vendor.record'])
        saved=json.loads((self.root/'resume/logging_contracts.json').read_text(encoding='utf-8'))
        self.assertEqual(saved,declaration)
        review=json.loads((self.root/'resume/logging_review.json').read_text(encoding='utf-8'))
        self.assertEqual(review['logging_contracts'],declaration['contracts'])

    def test_historical_grouping_cancel_does_not_cancel_new_attempt(self):
        self.data['run']['grouping_status']='cancelled'
        self.map.write_text(json.dumps(self.data),encoding='utf-8')
        first=run_product(self.args())
        result=run_product(self.args(map=None,resume_from=str(self.root/'out'),out=str(self.root/'resume')))
        self.assertFalse(result['interrupted'])
        self.assertTrue((self.root/'resume/logging_review.json').is_file())

    def test_output_inside_source_rejected(self):
        with self.assertRaises(ValueError): run_product(self.args(out=str(self.repo/'out')))

if __name__ == '__main__': unittest.main()
