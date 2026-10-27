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

    def node(self, kind='operation', evidence=None, **extra):
        result = self.state.dispatch('upsert_step', explain_args({'name':'Получение результата', 'description':'Самостоятельное действие',
            'kind':'operation', 'evidence_status':'observed', 'evidence':evidence or [self.ref], **extra}))
        self.assertTrue(result['ok'], result)
        if kind == 'end':self.state.model['scenario']['terminal_operation_ids'].append(result['id'])
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
        task['target']='Absent.createAgent'
        gap = self.state.dispatch('add_gap',{'description':'Implementation unavailable','category':'missing_source','task_ids':[task['id']], 'evidence':task['evidence']})['id']
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
        self.state.model['nodes']=[{'id':f'step-{i}','kind':'operation','basis':'Known','code_explanations':[{}],
            'evidence_status':'observed'} for i in range(1,4)]
        self.state.model['edges']=[{'id':f'edge-{i}','source':'step-1','target':f'step-{i+1}',
            'kind':'sequence','condition':'','basis':'Known','code_explanations':[{}],'evidence_status':'observed'} for i in (1,2)]
        self.assertTrue(any('категорию' in e for e in self.state.quality_errors()))
        for i,edge in enumerate(self.state.model['edges']):edge['condition']=f'Выбрана категория номер {i}'
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
        self.state.model['scenario']['terminal_operation_ids']=['step-1']
        self.state.model['nodes']=[{'id':'step-1','kind':'operation','evidence':[self.ref]}]
        payload=review_payload(self.state)
        payload['checks']['outcomes']['node_ids']=[]
        response=self.state.dispatch('review_semantics',payload)
        self.assertFalse(response['ok']);self.assertEqual(response['details']['missing_node_ids'],['step-1'])
        payload=review_payload(self.state);payload['checks']['atomicity']['status']='uncertain'
        self.assertFalse(self.state.dispatch('review_semantics',payload)['ok'])

    def test_code_basis_and_effect_level_are_finish_requirements(self):
        self.state.model['scenario']['terminal_operation_ids']=['step-1']
        self.state.model['nodes']=[{'id':'step-1','kind':'operation','basis':'','evidence_status':'observed'}]
        errors=';'.join(self.state.quality_errors())
        for field in ('basis','code_explanations','effect_kind','review_semantics'):self.assertIn(field,errors)

    def test_brief_is_stale_only_after_new_read_ranges(self):
        self.state.model['research_brief']={'revision':1,'read_signature':self.state.read_signature()}
        self.state.observe_read('source-1/channel.py',1,2)
        self.assertFalse(any('research_brief устарел' in e for e in self.state.quality_errors()))
        self.state.observe_read('source-1/Worker.java',1,3)
        self.assertTrue(any('research_brief устарел' in e for e in self.state.quality_errors()))


class OperationMethodologyQualityTests(unittest.TestCase):
    setUp = ResearchQualityTests.setUp
    node = ResearchQualityTests.node

    def task_args(self, task, **extra):
        return {key:copy.deepcopy(task[key]) for key in ('id','kind','target','evidence')} | extra

    def test_available_executor_cannot_be_closed_as_missing_external_source(self):
        self.state.observe_read('source-1/Manager.java',1,6)
        task=self.state.model['research_tasks'][0]
        gap=self.state.dispatch('add_gap',{'description':'Внешняя система не предоставлена','category':'missing_source',
            'task_ids':[task['id']],'evidence':task['evidence']})
        result=self.state.dispatch('save_research_task',self.task_args(task,status='gap',resolution='Нет внешнего кода',gap_ids=[gap['id']]))
        self.assertFalse(result['ok']);self.assertIn('есть в переданных файлах',result['error'])
        self.assertEqual(self.state.model['research_tasks'][0]['status'],'open')

    def test_unread_local_prompt_cannot_be_closed_as_unresolved_external_behavior(self):
        self.state.observe_read('source-1/Manager.java',1,6)
        task=self.state.model['research_tasks'][1]
        gap=self.state.dispatch('add_gap',{'description':'Внутреннее поведение модели неизвестно','category':'unresolved_behavior',
            'task_ids':[task['id']],'evidence':task['evidence']})
        result=self.state.dispatch('save_research_task',self.task_args(task,status='gap',resolution='Внешняя модель',gap_ids=[gap['id']]))
        self.assertFalse(result['ok']);self.assertIn('ещё не прочитана',result['error'])

    def test_one_external_gap_cannot_close_92_unrelated_available_tasks(self):
        tasks=[]
        for i in range(92):
            target='Absent'+str(i)+'.createAgent'
            result=self.state.dispatch('save_research_task',{'kind':'executor','target':target,'status':'open','evidence':[self.ref]})
            self.assertTrue(result['ok'],result);tasks.append(copy.deepcopy(self.state.model['research_tasks'][-1]))
        gap=self.state.dispatch('add_gap',{'description':'Отсутствует только первый исполнитель','category':'missing_source',
            'task_ids':[tasks[0]['id']],'evidence':[self.ref]})['id']
        for task in tasks[1:]:
            result=self.state.dispatch('save_research_task',self.task_args(task,status='gap',resolution='Общий внешний пробел',gap_ids=[gap]))
            self.assertFalse(result['ok']);self.assertIn('task_ids',result['error'])
        self.assertEqual(sum(t['status']=='open' for t in self.state.model['research_tasks']),92)

    def test_local_alias_and_direct_run_register_repeated_executor_calls(self):
        lines={1:'public static Agent assemble() {',2:'Agent documentAgent = DocumentAgent.createAgent();',
               3:'builder.addChild(documentAgent);',4:'ctx.runAgent(documentAgent, ctx.context());',
               5:'ctx.runAgent(FinalAgent.createAgent(), context);',6:'}'}
        found=dependencies('Flow.java',lines)
        self.assertEqual([(t['target'],t['evidence'][0]['line_start']) for t in found],
                         [('DocumentAgent.createAgent',3),('DocumentAgent.createAgent',4),('FinalAgent.createAgent',5)])
        self.assertEqual(found[0]['evidence'][1]['line_start'],2)

    def test_bare_parameter_in_other_method_is_not_bound_to_unrelated_local(self):
        lines={1:'public static Agent first() {',2:'Agent documentAgent = DocumentAgent.createAgent();',
               3:'builder.addChild(documentAgent);',4:'}',5:'public static void second(Agent documentAgent) {',
               6:'ctx.runAgent(documentAgent, context);',7:'}'}
        found=dependencies('Flow.java',lines)
        self.assertEqual([t['evidence'][0]['line_start'] for t in found],[3])

    def test_task_gap_requires_its_own_related_evidence(self):
        self.state.observe_read('source-1/Manager.java',1,6)
        task=self.state.model['research_tasks'][0]
        gap=self.state.dispatch('add_gap',{'description':'Не хватает сведений','category':'unresolved_behavior',
            'task_ids':[task['id']],'evidence':[self.ref]})['id']
        result=self.state.dispatch('save_research_task',self.task_args(task,status='gap',resolution='Не хватает сведений',gap_ids=[gap]))
        self.assertFalse(result['ok']);self.assertIn('основания',result['error'])

    def test_read_and_analyzed_are_not_finished_tasks(self):
        self.state.observe_read('source-1/Manager.java',1,6)
        self.state.observe_read('source-1/Worker.java',1,3)
        task=copy.deepcopy(self.state.model['research_tasks'][0])
        evidence=[{'path':'source-1/Worker.java','line_start':1,'line_end':3}]
        for status in ['read','analyzed']:
            result=self.state.dispatch('save_research_task',self.task_args(task,status=status,evidence=evidence,resolution='Реализация прочитана'))
            self.assertTrue(result['ok'],result)
            self.assertTrue(any(task['id'] in e for e in self.state.quality_errors()))
            self.assertIn(task['id'],[t['id'] for t in self.state.graph_frontier()['pending_tasks']])

    def parent_tasks(self):
        path='source-1/Root.java'
        self.state.workspace.tree.content[path]='public static Agent createAgent() {\n return Manager.createAgent();\n}'
        self.state.workspace.tree.files.append(path);self.state._files.add(path)
        self.state.observe_read(path,1,3)
        self.state.observe_read('source-1/Manager.java',1,5)
        return {t['target']:copy.deepcopy(t) for t in self.state.model['research_tasks']}

    def test_parent_builder_requires_all_discovered_children_even_when_directly_mapped(self):
        tasks=self.parent_tasks();parent=tasks['Manager.createAgent']
        ref={'path':'source-1/Manager.java','line_start':1,'line_end':5}
        node=self.node(evidence=[ref])
        result=self.state.dispatch('save_research_task',self.task_args(parent,evidence=[ref],status='mapped',node_ids=[node],resolution='Крупный родительский шаг'))
        self.assertFalse(result['ok']);self.assertIn('дочерние',result['error'])

    def test_parent_builder_maps_through_verified_children_without_wrapper_node(self):
        tasks=self.parent_tasks();parent=tasks['Manager.createAgent']
        child_ids=[];node_ids=[]
        for target,path,end in [('Worker.createAgent','source-1/Worker.java',3),('Prompts.answer','source-1/Prompts.java',1)]:
            self.state.observe_read(path,1,end)
            ref={'path':path,'line_start':1,'line_end':end};node=self.node(evidence=[ref]);node_ids.append(node)
            task=tasks[target];child_ids.append(task['id'])
            result=self.state.dispatch('save_research_task',self.task_args(task,evidence=[ref],status='mapped',node_ids=[node],resolution='Самостоятельное действие отображено'))
            self.assertTrue(result['ok'],result)
        result=self.state.dispatch('save_research_task',self.task_args(parent,evidence=[{'path':'source-1/Manager.java','line_start':1,'line_end':5}],
            status='mapped',child_task_ids=child_ids,resolution='Исполнение раскрыто дочерними операциями'))
        self.assertTrue(result['ok'],result)
        mapped=next(t for t in self.state.model['research_tasks'] if t['id']==parent['id'])
        self.assertEqual(set(mapped['node_ids']),set(node_ids));self.assertEqual(len(self.state.model['nodes']),2)
        self.state.model['research_tasks'][1]['status']='analyzed'
        self.assertTrue(any(parent['id'] in e for e in self.state.quality_errors()))

    def test_same_named_class_in_other_package_cannot_close_dependency(self):
        w=workspace(1)
        data={'source-1/a/Manager.java':'package a;\nimport b.Worker;\npublic static Agent createAgent() {\n return Worker.createAgent();\n}',
              'source-1/b/Worker.java':'package b;\npublic static Agent createAgent() { return build(); }',
              'source-1/c/Worker.java':'package c;\npublic static Agent createAgent() { return build(); }'}
        w.tree.content.update(data);w.tree.files=list(w.tree.content);self.state=MapState(w,'Получение результата')
        self.state.observe_read('source-1/a/Manager.java',1,5)
        task=copy.deepcopy(self.state.model['research_tasks'][0])
        self.state.observe_read('source-1/c/Worker.java',1,2)
        wrong={'path':'source-1/c/Worker.java','line_start':2,'line_end':2};node=self.node(evidence=[wrong])
        result=self.state.dispatch('save_research_task',self.task_args(task,status='mapped',node_ids=[node],evidence=[wrong],resolution='Исследовано'))
        self.assertFalse(result['ok'])
        self.state.observe_read('source-1/b/Worker.java',1,2)
        right={'path':'source-1/b/Worker.java','line_start':2,'line_end':2};node=self.node(evidence=[right])
        result=self.state.dispatch('save_research_task',self.task_args(task,status='mapped',node_ids=[node],evidence=[right],resolution='Исследована вызываемая реализация'))
        self.assertTrue(result['ok'],result)

    def branches(self, same_target=False):
        a=self.node();b=self.node();c=b if same_target else self.node()
        self.state.model['nodes'][0]['continuation_mode']='choice'
        for target,condition in [(b,'Прошло более 365 дней'),(c,'Прошло не более 365 дней')]:
            r=self.state.dispatch('upsert_transition',explain_args({'source':a,'target':target,'kind':'branch','condition':condition,
                'description':'Продолжение по проверенному сроку','evidence_status':'observed','evidence':[self.ref]}))
            self.assertTrue(r['ok'],r)
        return a,b,c

    def test_route_review_requires_every_branch_case(self):
        self.branches();payload=review_payload(self.state);payload['route_cases'].pop()
        result=self.state.dispatch('review_semantics',payload)
        self.assertFalse(result['ok']);self.assertIn('route_cases',result['error'])

    def test_route_review_rejects_invented_connection(self):
        self.branches();payload=review_payload(self.state)
        payload['route_cases'][0]['path'].reverse()
        result=self.state.dispatch('review_semantics',payload)
        self.assertFalse(result['ok']);self.assertIn('отсутствующий переход',result['error'])

    def test_same_pair_different_conditions_require_exact_edges(self):
        self.branches(same_target=True);payload=review_payload(self.state)
        payload['route_cases'][0].pop('edge_ids')
        result=self.state.dispatch('review_semantics',payload)
        self.assertFalse(result['ok']);self.assertIn('неоднозначный',result['error'])
        payload=review_payload(self.state);payload['route_cases'].pop()
        self.assertFalse(self.state.dispatch('review_semantics',payload)['ok'])
        self.assertTrue(self.state.dispatch('review_semantics',review_payload(self.state))['ok'])

    def test_route_case_evidence_must_address_traversed_connection(self):
        self.branches();self.state.observe_read('source-1/Worker.java',1,3)
        payload=review_payload(self.state)
        payload['route_cases'][0]['evidence']=[{'path':'source-1/Worker.java','line_start':1,'line_end':3}]
        result=self.state.dispatch('review_semantics',payload)
        self.assertFalse(result['ok']);self.assertIn('пройденным переходам',result['error'])

    def test_routing_check_cannot_omit_edges(self):
        self.branches();payload=review_payload(self.state);payload['checks']['routing']['edge_ids']=[]
        result=self.state.dispatch('review_semantics',payload)
        self.assertFalse(result['ok']);self.assertIn('каждый переход',result['error'])

    def conditional_terminal(self):
        first=self.node(completion_condition='Все продукты портфеля оформлены')
        last=self.node()
        self.state.model['scenario']['entry_operation_ids']=[first]
        self.state.model['scenario']['terminal_operation_ids']=[first,last]
        edge=self.state.dispatch('upsert_transition',explain_args({'source':first,'target':last,'kind':'sequence',
            'condition':'В портфеле остались неоформленные продукты','evidence_status':'observed','evidence':[self.ref]}))
        self.assertTrue(edge['ok'],edge)
        return first,last

    def test_conditional_terminal_keeps_real_continuation_without_artificial_end(self):
        first,last=self.conditional_terminal()
        payload=review_payload(self.state)
        self.assertTrue(self.state.dispatch('review_semantics',payload)['ok'])
        self.assertFalse(any('конечная операция имеет' in e for e in self.state._finish_errors()))
        self.assertNotIn(first,self.state.graph_frontier()['open_exits'])
        self.assertEqual(len(self.state.model['nodes']),2)

    def test_conditional_terminal_requires_both_stop_and_continue_cases(self):
        self.conditional_terminal()
        payload=review_payload(self.state)
        payload['route_cases']=[case for case in payload['route_cases'] if len(case['path'])>1]
        result=self.state.dispatch('review_semantics',payload)
        self.assertFalse(result['ok']);self.assertIn('случая остановки',result['error'])
        payload=review_payload(self.state)
        payload['route_cases']=[case for case in payload['route_cases'] if len(case['path'])==1]
        self.assertFalse(self.state.dispatch('review_semantics',payload)['ok'])

    def test_conditional_terminal_single_outgoing_still_needs_condition(self):
        first,last=self.conditional_terminal()
        self.state.model['edges'][0]['condition']=''
        self.assertTrue(any('условий на всех выходах' in e for e in self.state.quality_errors()))
        self.assertIn(first,self.state.graph_frontier()['open_exits'])

    def test_conditional_terminal_without_continuation_requires_addressed_uncertainty(self):
        first,last=self.conditional_terminal();self.state.model['edges']=[]
        self.assertTrue(any('альтернативного продолжения' in e for e in self.state.quality_errors()))
        self.assertIn(first,self.state.graph_frontier()['open_exits'])
        self.assertFalse(self.state.dispatch('review_semantics',review_payload(self.state))['ok'])
        gap=self.state.dispatch('add_gap',{'description':'Не установлен путь при наличии оставшихся продуктов','category':'unresolved_transition',
            'node_ids':[first],'evidence':[self.ref]})['id']
        payload=review_payload(self.state)
        payload['checks']['routing'].update(status='uncertain',gap_ids=[gap])
        self.assertTrue(self.state.dispatch('review_semantics',payload)['ok'])
        self.assertFalse(any('альтернативного продолжения' in e for e in self.state.quality_errors()))
        self.assertNotIn(first,self.state.graph_frontier()['open_exits'])

    def test_completion_condition_requires_terminal_membership(self):
        first,last=self.conditional_terminal()
        self.state.model['scenario']['terminal_operation_ids']=[last]
        self.assertTrue(any('completion_condition требует включения' in e for e in self.state._finish_errors()))

    def test_business_conditions_keep_numbers_but_reject_code(self):
        a=self.node();b=self.node()
        args={'source':a,'target':b,'kind':'branch','evidence_status':'observed','evidence':[self.ref]}
        for condition in ['days > 365','Срок > 365','category_id == 4','taskTypeName']:
            self.assertFalse(self.state.dispatch('upsert_transition',{**args,'condition':condition})['ok'])
        self.assertTrue(self.state.dispatch('upsert_transition',{**args,'condition':'С момента перевода прошло более 365 дней'})['ok'])

    def test_obvious_code_names_rejected_but_business_acronyms_remain(self):
        for name in ['CheckAgent.createAgent()','ATM_83','Проверка idEcm','DateAgent.java']:
            self.assertFalse(self.state.dispatch('upsert_step',{'name':name,'evidence_status':'observed','evidence':[self.ref]})['ok'])
        self.node(name='Проверка перевода через СБП')


if __name__=='__main__':unittest.main()
