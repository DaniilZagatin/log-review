"""Rules distilled from two real runs: task closure, loop exits, checks, calls and public language.

The cases use synthetic names and layouts; they reproduce the structure of the
observed defects, not the projects themselves.
"""
import copy
import json
import shutil
import subprocess
import unittest

from pko.render.scenario_map import _GRAPH_LAYOUT_JS, render_scenario_map
from pko.scenario_map.compaction import SUMMARY_MAX_CHARS, validate_summary
from pko.scenario_map.schema import MapState
from quality_helpers import explain_args
from test_scenario_schema import workspace


class _State(unittest.TestCase):
    def setUp(self):
        w = workspace(1)
        w.tree.content['source-1/Router.java'] = '\n'.join([
            'public static Agent createAgent() {',
            '  builder.addChild(Handler.createAgent());',
            '  return builder.build();', '}'])
        w.tree.content['source-1/Handler.java'] = 'public static Agent createAgent() {\n return factory();\n}'
        w.tree.files = list(w.tree.content)
        self.state = MapState(w, 'Синтетический вариант процесса')
        self.state.observe_read('source-1/channel.py', 1, 2)
        self.ref = {'path': 'source-1/channel.py', 'line_start': 1, 'line_end': 2}

    def step(self, **extra):
        payload = {'name': 'Получение результата', 'description': 'Самостоятельное действие', 'kind': 'operation',
                   'evidence_status': 'observed', 'evidence': [self.ref], **extra}
        return self.state.dispatch('upsert_step', explain_args(payload))

    def discovered(self):
        self.state.observe_read('source-1/Router.java', 1, 4)
        task = copy.deepcopy(self.state.model['research_tasks'][0])
        self.assertEqual(task['target'], 'Handler.createAgent')
        return {key: task[key] for key in ('id', 'target', 'kind', 'evidence')}


class TaskClosureTests(_State):
    def test_out_of_scope_needs_a_visible_scope_exclusion(self):
        task = self.discovered()
        closing = {**task, 'status': 'out_of_scope', 'resolution': 'Другой вариант'}
        result = self.state.dispatch('save_research_task', closing)
        self.assertFalse(result['ok'])
        self.assertIn('set_scope(exclusions)', result['error'])
        self.state.model['scenario']['exclusions'] = ['Перевыпуск карты']
        result = self.state.dispatch('save_research_task', {**closing, 'exclusion': 'Что-то другое'})
        self.assertFalse(result['ok'])
        self.assertIn('Перевыпуск карты', result['error'])
        result = self.state.dispatch('save_research_task', {**closing, 'exclusion': 'Перевыпуск карты'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.state.model['research_tasks'][0]['exclusion'], 'Перевыпуск карты')
        # Removing the exclusion later makes the closure visible as wrong at finish.
        self.state.model['scenario']['exclusions'] = []
        self.assertTrue(any('исключение задачи не соответствует' in error for error in self.state.quality_errors()))

    def test_out_of_scope_cannot_close_work_already_shown_on_the_map(self):
        task = self.discovered()
        self.state.model['scenario']['exclusions'] = ['Перевыпуск карты']
        self.state.observe_read('source-1/Handler.java', 1, 3)
        implementation = {'path': 'source-1/Handler.java', 'line_start': 1, 'line_end': 3}
        node = self.step(evidence=[implementation])
        self.assertTrue(node['ok'], node)
        closing = {**task, 'status': 'out_of_scope', 'resolution': 'Раскрыт операциями', 'exclusion': 'Перевыпуск карты'}
        result = self.state.dispatch('save_research_task', {**closing, 'evidence': [implementation]})
        self.assertFalse(result['ok'])
        self.assertIn('mapped', result['error'])
        result = self.state.dispatch('save_research_task', {**closing, 'node_ids': [node['id']]})
        self.assertFalse(result['ok'])
        self.assertIn('mapped', result['error'])

    def test_unexplored_local_dependency_is_an_honest_gap_without_reading_it(self):
        task = self.discovered()
        unresolved = self.state.dispatch('add_gap', {'description': 'Поведение обработчика не установлено',
                                                     'category': 'unresolved_behavior', 'task_ids': [task['id']],
                                                     'evidence': task['evidence']})
        self.assertTrue(unresolved['ok'], unresolved)
        result = self.state.dispatch('save_research_task', {**task, 'status': 'gap', 'resolution': 'Не исследован',
                                                            'gap_ids': [unresolved['id']]})
        self.assertFalse(result['ok'])
        self.assertIn('не прочитана', result['error'])
        unexplored = self.state.dispatch('add_gap', {'description': 'Обработчик варианта не исследован в пределах бюджета',
                                                     'category': 'unexplored', 'task_ids': [task['id']],
                                                     'evidence': task['evidence']})
        self.assertTrue(unexplored['ok'], unexplored)
        result = self.state.dispatch('save_research_task', {**task, 'status': 'gap', 'resolution': 'Не исследован',
                                                            'gap_ids': [unexplored['id']]})
        self.assertTrue(result['ok'], result)
        missing = self.state.dispatch('add_gap', {'description': 'Кода нет', 'category': 'missing_source',
                                                  'task_ids': [task['id']], 'evidence': task['evidence']})
        result = self.state.dispatch('save_research_task', {**task, 'status': 'gap', 'resolution': 'Кода нет',
                                                            'gap_ids': [missing['id']]})
        self.assertFalse(result['ok'], 'local code cannot be declared missing')


class LoopExitTests(unittest.TestCase):
    def errors(self, edges, completion=()):
        nodes = {name: {'id': name, 'completion_condition': 'Остановка' if name in completion else ''}
                 for name in {value for edge in edges for value in edge}}
        outgoing = {name: set() for name in nodes}
        for source, target in edges:
            outgoing[source].add(target)
        return MapState._loop_exit_errors(nodes, outgoing)

    def test_loop_whose_exit_was_drawn_as_a_repeat_is_reported(self):
        # Fetch → check → enqueue → fetch: the "nothing left" exit became another repeat.
        self.assertEqual(len(self.errors([('fetch', 'check'), ('check', 'enqueue'), ('enqueue', 'fetch'), ('check', 'fetch')])), 1)

    def test_loop_with_an_exit_or_conditional_completion_is_fine(self):
        self.assertEqual(self.errors([('fetch', 'check'), ('check', 'fetch'), ('check', 'publish')]), [])
        self.assertEqual(self.errors([('take', 'take'), ('take', 'process')]), [])
        self.assertEqual(self.errors([('fetch', 'check'), ('check', 'fetch')], completion={'check'}), [])

    def test_self_loop_without_exit_is_reported(self):
        self.assertEqual(len(self.errors([('poll', 'poll')])), 1)


class ReviewedKindTests(_State):
    def graph(self, edges, kind, **node):
        self.state.model['nodes'] = [{'id': name, 'completion_condition': '', 'external_interactions': [], **node.get(name, {})}
                                     for name in ('step-1', 'step-2', 'step-3')]
        self.state.model['edges'] = [{'id': f'edge-{i}', 'source': s, 'target': t, 'kind': 'sequence'}
                                     for i, (s, t) in enumerate(edges, 1)]
        self.state.model['execution_reviews'] = [{'id': 'review-1', 'operations': [{'node_id': 'step-1', 'kind': kind}]}]
        return self.state._reviewed_kind_errors()

    def test_check_with_one_continuation_lost_its_other_outcome(self):
        self.assertEqual(len(self.graph([('step-1', 'step-2')], 'validation')), 1)
        self.assertEqual(self.graph([('step-1', 'step-2'), ('step-1', 'step-3')], 'validation'), [])
        self.assertEqual(self.graph([('step-1', 'step-2')], 'validation', **{'step-1': {'completion_condition': 'Нет решения'}}), [])
        self.state.model['gaps'] = [{'id': 'gap-1', 'node_ids': ['step-1']}]
        self.assertEqual(self.graph([('step-1', 'step-2')], 'validation'), [])

    def test_operation_reviewed_as_a_call_names_the_called_system(self):
        self.assertEqual(len(self.graph([('step-1', 'step-2')], 'integration')), 1)
        call = [{'name': 'АС заявок', 'purpose': 'Передать заявку', 'direction': 'send', 'endpoint': '/applications',
                 'contract': '', 'evidence': [self.ref]}]
        self.assertEqual(self.graph([('step-1', 'step-2')], 'integration', **{'step-1': {'external_interactions': call}}), [])


class OperationContentTests(_State):
    def call(self, name):
        return {'name': name, 'purpose': 'Передать заявку на рассмотрение', 'direction': 'send',
                'endpoint': 'https://applications.example/debit', 'evidence': [self.ref]}

    def test_calls_to_two_systems_are_two_operations(self):
        result = self.step(external_interactions=[self.call('АС заявок'), self.call('АС выпуска')])
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'several_systems')
        self.assertTrue(self.step(external_interactions=[self.call('АС заявок')])['ok'])

    def test_variable_and_method_names_stay_out_of_public_fields(self):
        for field, value in (('description', 'Проверяет pprbAttrError и продолжает'),
                             ('input', 'record_date и сумма операции'), ('output', 'Результат handleResponse()')):
            result = self.step(**{field: value})
            self.assertFalse(result['ok'], field)
            self.assertTrue(result['error'].startswith(field + ':'), result['error'])
        accepted = self.step(description='Разбор назначения платежа через GigaChat', input='Назначение платежа и счёт',
                             output='Найденный кредитный договор')
        self.assertTrue(accepted['ok'], accepted)



class SummaryContractTests(_State):
    def summary(self, items=3, length=100):
        return {"project_and_scenario": "Проект выдачи карт", "completed_work": [f"Сделано {n}: " + "х" * length for n in range(items)],
                "key_findings": ["Найден диспетчер вариантов"], "open_questions": [], "next_actions": ["Прочитать обработчик"],
                "references": []}

    def test_fenced_or_introduced_summary_is_parsed(self):
        fenced = "```json\n" + json.dumps(self.summary(), ensure_ascii=False) + "\n```"
        self.assertEqual(validate_summary(fenced, self.state)["project_and_scenario"], "Проект выдачи карт")
        introduced = "Сводка исследования:\n" + json.dumps(self.summary(), ensure_ascii=False)
        self.assertEqual(validate_summary(introduced, self.state)["next_actions"], ["Прочитать обработчик"])

    def test_oversized_summary_is_trimmed_not_discarded(self):
        result = validate_summary(json.dumps(self.summary(items=12, length=700), ensure_ascii=False), self.state)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), SUMMARY_MAX_CHARS)
        self.assertLess(len(result["completed_work"]), 12)
        self.assertEqual(result["key_findings"], ["Найден диспетчер вариантов"])


class HeaderAndScopeTests(unittest.TestCase):
    def model(self, description):
        return {"schema_version": "2.0", "scenario": {"title": "Погашение кредита", "description": description,
                                                    "start": "Поступило платёжное поручение", "end": "Платёж обработан или отклонён",
                                                    "exclusions": ["Ручная обработка в другой системе"],
                                                    "entry_operation_ids": [], "terminal_operation_ids": []},
                "nodes": [], "edges": [], "gaps": [], "run": {"status": "partial"},
                "research_tasks": [{"id": "task-1", "status": "out_of_scope", "exclusion": "Ручная обработка в другой системе"}]}

    def test_header_shows_the_variant_not_the_submitted_json(self):
        page = render_scenario_map(self.model('{\n  "process": {"id": "P-1"}}'))
        self.assertIn("briefDescription=scenario.start&&scenario.end", page)
        self.assertIn("'Вне исследования'", page)
        self.assertIn("исключено участков кода", page)



class EdgeGeometryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js нужен для проверки геометрии стрелок")
    def test_backward_loops_self_loops_and_bypasses_stay_on_the_canvas(self):
        # A fetch loop returning to the first rank, a self-loop with an exit, two retries and a long bypass.
        pairs = [("fetch", "check"), ("check", "enqueue"), ("enqueue", "fetch"), ("check", "process"),
                 ("process", "process"), ("process", "send"), ("send", "process"), ("send", "verify"),
                 ("verify", "send"), ("verify", "close"), ("fetch", "close")]
        names = sorted({name for pair in pairs for name in pair})
        edges = [{"id": f"edge-{i}", "source": s, "target": t, "kind": "sequence"} for i, (s, t) in enumerate(pairs)]
        runner = _GRAPH_LAYOUT_JS + """
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const layout=layoutScenarioGraph(input.nodes,input.edges,new Map(input.sizes),input.vertical,input.entry_ids);
const state={};const outside=[];
for(const edge of input.edges){const route=routeScenarioEdge(edge,layout,input.vertical,state);
 for(const [p0,p1,p2,p3] of route.segments)for(let i=0;i<=40;i++){const t=i/40,u=1-t;
  const x=u*u*u*p0.x+3*u*u*t*p1.x+3*u*t*t*p2.x+t*t*t*p3.x,y=u*u*u*p0.y+3*u*u*t*p1.y+3*u*t*t*p2.y+t*t*t*p3.y;
  if(x<0||y<0||x>layout.width||y>layout.height){outside.push([edge.id,+x.toFixed(1),+y.toFixed(1)]);break;}}}
console.log(JSON.stringify({outside,width:layout.width,height:layout.height}));
"""
        for vertical in (False, True):
            with self.subTest(vertical=vertical):
                payload = {"nodes": [{"id": name, "kind": "operation"} for name in names], "edges": edges,
                           "vertical": vertical, "entry_ids": ["fetch"],
                           "sizes": [[name, {"w": 224, "h": 90 + i % 3 * 30}] for i, name in enumerate(names)]}
                result = subprocess.run([shutil.which("node"), "-e", runner], input=json.dumps(payload),
                                        capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["outside"], [])


if __name__ == '__main__':
    unittest.main()
