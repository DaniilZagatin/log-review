"""Regressions distilled from the two P3283 runs; no bank sources required."""
import copy
import unittest

from pko.scenario_map.schema import MapState
from pko.scenario_map.quality import QualityError, SEMANTIC_CHECKS
from pko.scenario_map.code_references import symbols, symbol_valid, dependencies
from pko.scenario_map.context import compact_context, _json
from pko.scenario_map.agent import _brief
from test_scenario_schema import workspace
from quality_helpers import review_payload, explain_args


class ResearchQualityTests(unittest.TestCase):
    def setUp(self):
        w = workspace(1)
        w.tree.content['source-1/Manager.java'] = '\n'.join([
            'public static Agent createAgent() {',
            '  builder.addChild(Worker.createAgent());',
            '  config.systemPrompt(Prompts.answer);',
            '  return builder.build();', '}',
            '// builder.addChild(Fake.createAgent());'])
        w.tree.content['source-1/Worker.java'] = 'public static Agent createAgent() {\n return factory();\n}'
        w.tree.content['source-1/Prompts.java'] = 'public static String answer = "Answer from source data";'
        w.tree.files = list(w.tree.content)
        self.state = MapState(w, 'A synthetic scenario')
        self.state.observe_read('source-1/channel.py', 1, 2)
        self.ref = {'path':'source-1/channel.py','line_start':1,'line_end':2}

    def node(self, kind='action', evidence=None, **extra):
        existing = self.state.model['systems']
        system = self.state.dispatch('upsert_system', {**({'id':existing[0]['id']} if existing else {}), 'name':'Test system','source_ids':['source-1'],
            'evidence_status':'observed','evidence':[self.ref]})
        self.assertTrue(system['ok'], system)
        result = self.state.dispatch('upsert_step', explain_args({'name':'Test action', 'description':'Synthetic action',
            'kind':kind, 'system_id':system['id'], 'evidence_status':'observed', 'evidence':evidence or [self.ref], **extra}))
        self.assertTrue(result['ok'], result)

        return result['id']

    def test_review_error_reports_actual_missing_range_not_other_file(self):
        refs = [{'path':'Manager.java','line_start':50,'line_end':62},
                {'path':'TaskTypeHelper.java','line_start':13,'line_end':19}]
        scope = [{'path':'Manager.java','line_start':43,'line_end':127},
                 {'path':'TaskTypeHelper.java','line_start':21,'line_end':56}]
        with self.assertRaises(QualityError) as caught:
            self.state.require_coverage('step-3','node.evidence',refs,scope)
        self.assertEqual(caught.exception.details['missing_evidence'], [refs[1]])
        self.assertIn('TaskTypeHelper.java:13-19', str(caught.exception))
        self.state.require_coverage('step-3','node.evidence',refs,scope+[refs[1]])

    def test_disjoint_review_ranges_report_only_uncovered_lines(self):
        with self.assertRaises(QualityError) as caught:
            self.state.require_coverage('step-1','node.evidence', [{'path':'x','line_start':1,'line_end':10}],
                                        [{'path':'x','line_start':1,'line_end':3},{'path':'x','line_start':7,'line_end':10}])
        self.assertEqual(caught.exception.details['missing_evidence'],[{'path':'x','line_start':4,'line_end':6}])

    def test_function_names_are_verified_not_accepted_from_comment_or_call(self):
        args = [{'kind':'function','symbol':'submit','explanation':'Передаёт заявку в банк.','evidence':[self.ref]}]
        result = self.state.code_explanations(args, [self.ref])
        self.assertEqual(result[0]['symbol'], 'submit')
        args[0]['symbol']='post'
        with self.assertRaises(QualityError):self.state.code_explanations(args,[self.ref])
        self.assertNotIn('imaginary', symbols('/* public static void imaginary() {} */'))
        self.assertNotIn('imaginary', symbols('String s = "public static void imaginary() {}";'))
        self.assertFalse(symbol_valid('', 'lambda', 'String value = "a -> b"; // lambda'))
        self.assertTrue(symbol_valid('', 'lambda', 'values.map(x -> x.id);'))
        self.assertFalse(symbol_valid('', 'field', 'value = 1'))

    def test_dependency_discovery_keeps_read_windows_separate(self):
        refs = dependencies('x', {1:'String text = """', 20:'builder.addChild(Worker.createAgent());', 21:'""";'})
        self.assertEqual([r['target'] for r in refs], ['Worker.createAgent'])
        self.assertEqual(dependencies('x', {1:'/*', 2:'builder.addChild(Fake.createAgent());', 3:'*/'}), [])

    def test_dispatcher_registers_all_nine_nested_scenarios(self):
        lines = {i+1:f'case "variant-{i}": return Scenario{i}.createAgent();' for i in range(9)}
        refs = dependencies('Dispatcher.java', lines)
        self.assertEqual(len(refs), 9)
        self.assertEqual({row['target'] for row in refs}, {f'Scenario{i}.createAgent' for i in range(9)})

    def test_unread_declaration_is_not_accepted_as_function_explanation(self):
        with self.assertRaises(ValueError):
            self.state.code_explanations([{'kind':'function','symbol':'createAgent','explanation':'Worker',
                'evidence':[{'path':'source-1/Worker.java','line_start':1,'line_end':3}]}],[self.ref])

    def test_prompt_and_executor_read_creates_stable_open_obligations(self):
        self.state.observe_read('source-1/Manager.java',1,6)
        tasks = self.state.model['research_tasks']
        self.assertEqual([t['target'] for t in tasks],['Worker.createAgent','Prompts.answer'])
        self.state.observe_read('source-1/Manager.java',1,6)
        self.assertEqual(len(tasks),2)
        self.assertTrue(any('Worker.createAgent' in e for e in self.state.quality_errors()))
        args = {k:copy.deepcopy(tasks[0][k]) for k in ('id','target','kind','evidence')}
        response = self.state.dispatch('save_research_task',{**args,'status':'mapped','resolution':'Read'})
        self.assertFalse(response['ok'])
        self.assertEqual(tasks[0]['status'],'open')

    def test_gap_resolution_survives_compaction_with_recovery_path(self):
        self.state.observe_read('source-1/Manager.java',1,6)
        task = self.state.model['research_tasks'][0]
        gap = self.state.dispatch('add_gap',{'description':'Implementation unavailable'})['id']
        args = {k:task[k] for k in ('id','target','kind','evidence')}
        result=self.state.dispatch('save_research_task',{**args,'status':'gap','resolution':'Implementation unavailable','gap_ids':[gap]})
        self.assertTrue(result['ok'],result)
        messages=[{'role':'system','content':'rules'},{'role':'user','content':'scenario'},{'role':'assistant','content':'x'*40000}]
        for _ in range(2):
            messages=compact_context(messages,self.state,12000,brief=_brief)
            self.assertIn('research_tasks',_json(messages))
            messages.append({'role':'assistant','content':'x'*40000})
        self.assertEqual(self.state.model['research_tasks'][0]['gap_ids'],[gap])
        self.assertEqual(self.state.model['research_tasks'][1]['status'],'open')

    def test_common_segment_cannot_lose_category_on_unconditional_exits(self):
        self.state.model['nodes']=[{'id':f'step-{i}','kind':'action','basis':'Known','code_explanations':[{}],
            'evidence_status':'observed'} for i in range(1,4)]
        self.state.model['edges']=[{'id':f'edge-{i}','source':'step-1','target':f'step-{i+1}',
            'kind':'sequence','condition':'','basis':'Known','code_explanations':[{}],'evidence_status':'observed'} for i in (1,2)]
        self.assertTrue(any('категорию' in e for e in self.state.quality_errors()))
        for i,edge in enumerate(self.state.model['edges']):edge['condition']=f'category={i}'
        self.assertFalse(any('категорию' in e for e in self.state.quality_errors()))

    def test_semantic_review_is_model_assessment_and_becomes_stale_on_effect_change(self):
        self.node('end', effect_kind='recommendation')
        payload=review_payload(self.state)
        result=self.state.dispatch('review_semantics',payload)
        self.assertTrue(result['ok'],result)
        self.assertFalse(result['semantic_verification'])
        self.assertFalse(any('review_semantics' in e for e in self.state.quality_errors()))
        self.state.model['nodes'][0]['effect_kind']='result'
        self.assertTrue(any('review_semantics' in e for e in self.state.quality_errors()))

    def test_dependency_cannot_be_mapped_from_registration_only(self):
        self.state.observe_read('source-1/Manager.java',1,6)
        task = copy.deepcopy(self.state.model['research_tasks'][0])
        node_id = self.node(evidence=task['evidence'])
        args = {k:task[k] for k in ('id','target','kind','evidence')}
        result = self.state.dispatch('save_research_task', {**args, 'status':'mapped',
            'node_ids':[node_id], 'resolution':'Read the registration'})
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['details']['target'], 'Worker.createAgent')
        self.state.observe_read('source-1/Worker.java',1,3)
        implementation = {'path':'source-1/Worker.java','line_start':1,'line_end':3}
        worker_id = self.node(evidence=[implementation])
        result = self.state.dispatch('save_research_task', {**args, 'evidence':[implementation], 'status':'mapped',
            'node_ids':[worker_id], 'resolution':'The worker action is represented in the map'})
        self.assertTrue(result['ok'], result)
        worker = next(n for n in self.state.model['nodes'] if n['id']==worker_id)
        worker['evidence'] = task['evidence']
        self.assertTrue(any('больше не покрывают' in error for error in self.state.quality_errors()))

    def test_uncertain_review_requires_gap_and_each_end_must_be_reviewed(self):
        self.state.model['nodes']=[{'id':'step-1','kind':'end','evidence':[self.ref]}]
        payload=review_payload(self.state)
        payload['checks']['outcomes']['node_ids']=[]
        response=self.state.dispatch('review_semantics',payload)
        self.assertFalse(response['ok']);self.assertEqual(response['details']['missing_node_ids'],['step-1'])
        payload=review_payload(self.state);payload['checks']['atomicity']['status']='uncertain'
        self.assertFalse(self.state.dispatch('review_semantics',payload)['ok'])

    def test_code_basis_and_effect_level_are_finish_requirements(self):
        self.state.model['nodes']=[{'id':'step-1','kind':'end','basis':'','evidence_status':'observed'}]
        errors=';'.join(self.state.quality_errors())
        for field in ('basis','code_explanations','effect_kind','review_semantics'):self.assertIn(field,errors)

    def test_brief_is_stale_only_after_new_read_ranges(self):
        self.state.model['research_brief']={'revision':1,'read_signature':self.state.read_signature()}
        self.state.observe_read('source-1/channel.py',1,2)
        self.assertFalse(any('research_brief устарел' in e for e in self.state.quality_errors()))
        self.state.observe_read('source-1/Worker.java',1,3)
        self.assertTrue(any('research_brief устарел' in e for e in self.state.quality_errors()))


if __name__=='__main__':unittest.main()
