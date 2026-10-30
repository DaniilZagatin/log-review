"""Explicit business topology, source-only presentation, untrusted graph labels and layout invariants."""

import copy
import dataclasses
import html
import re
import unittest

from process_observability import report as r
from process_observability.report import build_html, build_markdown
from process_observability.scenario_graph import ROUTES, build_layout
from process_observability.scenario_layout import LABEL, LABEL_LIMIT, NAME, Box, fits, normalize, verify, wrap
from test_workflow import FakeSnapshot, analyse, map_response, instrumentation_response, metrics_response, ref


def graph_bundle(stages, operations, transitions, scenarios):
    """A rendered-only map bundle; layout tests do not depend on the analysis workflow."""
    return {'process_map': {
        'stages': [{'id': key, 'name': name} for key, name in stages],
        'operations': [{'id': key, 'name': name, 'stage_id': stage, 'kind': kind, 'description': None, 'actor': None,
                        'event_keys': [], 'evidence_ids': []} for key, stage, kind, name in operations],
        'operation_transitions': [{'id': key, 'from_operation': a, 'to_operation': b, 'condition': condition}
                                  for key, a, b, condition in transitions],
        'scenarios': [{'id': key, 'name': name, 'description': '', 'operation_ids': ops, 'transition_ids': edges}
                      for key, name, ops, edges in scenarios]}}


# Synthetic transfer process shaped like the public run05 map: same topology, similar long Russian conditions.
RUN05_STAGES = [('init', 'Приём распоряжения на перевод'), ('debit', 'Списание суммы'), ('credit', 'Зачисление суммы'),
                ('compensate', 'Возврат суммы отправителю'), ('finish', 'Завершение перевода')]
RUN05_OPERATIONS = [('op-start', 'init', 'start', 'Принять распоряжение'),
                    ('op-debit', 'debit', 'operation', 'Списать сумму со счёта отправителя'),
                    ('op-credit', 'credit', 'operation', 'Зачислить сумму на счёт получателя'),
                    ('op-refund', 'compensate', 'operation', 'Вернуть сумму отправителю'),
                    ('op-ok', 'finish', 'end', 'Перевод выполнен'),
                    ('op-fail', 'finish', 'end', 'Перевод не выполнен')]
RUN05_TRANSITIONS = [
    ('t1', 'op-start', 'op-debit', 'всегда после запуска процесса перевода'),
    ('t2', 'op-debit', 'op-credit', 'списание прошло успешно (исключения не возникло)'),
    ('t3', 'op-debit', 'op-fail', 'списание завершилось ошибкой (НеверныйСчётОшибка, НедостаточноСредствОшибка '
                                  'или неповторяемый сбой после всех попыток)'),
    ('t4', 'op-credit', 'op-ok', 'зачисление прошло успешно'),
    ('t5', 'op-credit', 'op-refund', 'зачисление завершилось с ОшибкойДействия'),
    ('t6', 'op-refund', 'op-fail', 'всегда — возврат не меняет общий исход на успешный; независимо от результата '
                                   'возврата процесс завершается исключением')]
_REFUND = (['op-start', 'op-debit', 'op-credit', 'op-refund', 'op-fail'], ['t1', 't2', 't5', 't6'])
RUN05_SCENARIOS = [('s1', 'Успешный перевод', ['op-start', 'op-debit', 'op-credit', 'op-ok'], ['t1', 't2', 't4']),
                   ('s2', 'Отказ списания', ['op-start', 'op-debit', 'op-fail'], ['t1', 't3']),
                   ('s3', 'Неуспешное зачисление с успешным возвратом', *_REFUND),
                   ('s4', 'Неуспешное зачисление с неуспешным возвратом', *_REFUND)]


def run05_bundle():
    return graph_bundle(RUN05_STAGES, RUN05_OPERATIONS, RUN05_TRANSITIONS, RUN05_SCENARIOS)


# 25 operations, 6 scenarios, three rework loops, a refusal, a cancellation and one operation outside scenarios.
LARGE_STAGES = [('receive', 'Приём обращения'), ('prepare', 'Подготовка сведений'), ('review', 'Рассмотрение'),
                ('decision', 'Решение'), ('execute', 'Выполнение'), ('finish', 'Завершение')]
LARGE_OPERATIONS = [
    ('get', 'receive', 'start', 'Получить обращение'), ('register', 'receive', 'operation', 'Зарегистрировать обращение'),
    ('ack', 'receive', 'operation', 'Подтвердить приём'), ('check', 'prepare', 'operation', 'Проверить комплектность'),
    ('complete', 'prepare', 'decision', 'Сведений достаточно?'), ('ask', 'prepare', 'operation', 'Запросить дополнение'),
    ('receive-more', 'prepare', 'operation', 'Получить дополнение'),
    ('assign', 'review', 'operation', 'Назначить ответственного'), ('examine', 'review', 'operation', 'Рассмотреть обращение'),
    ('need', 'review', 'decision', 'Нужны уточнения?'), ('clarify', 'review', 'operation', 'Запросить уточнение'),
    ('clarified', 'review', 'operation', 'Получить уточнение'), ('draft', 'decision', 'operation', 'Подготовить решение'),
    ('agree', 'decision', 'operation', 'Согласовать решение'), ('positive', 'decision', 'decision', 'Решение положительное?'),
    ('refuse', 'decision', 'end', 'Зафиксировать отказ'),
    ('handoff', 'execute', 'operation', 'Передать решение на исполнение'),
    ('perform', 'execute', 'operation', 'Выполнить решение'), ('done', 'execute', 'decision', 'Исполнение завершено?'),
    ('retry', 'execute', 'operation', 'Повторить исполнение'), ('notify', 'finish', 'operation', 'Уведомить клиента'),
    ('confirm', 'finish', 'operation', 'Подтвердить результат'), ('close', 'finish', 'end', 'Закрыть обращение'),
    ('cancel', 'finish', 'end', 'Подтвердить отмену'), ('pause', 'review', 'operation', 'Приостановить обработку')]
LARGE_TRANSITIONS = [
    ('e1', 'get', 'register', None), ('e2', 'register', 'ack', None), ('e3', 'ack', 'check', None),
    ('e4', 'check', 'complete', None), ('e5', 'complete', 'assign', 'Да'), ('e6', 'assign', 'examine', None),
    ('e7', 'examine', 'need', None), ('e8', 'need', 'draft', 'Нет'), ('e9', 'draft', 'agree', None),
    ('e10', 'agree', 'positive', None), ('e11', 'positive', 'handoff', 'Да'), ('e12', 'handoff', 'perform', None),
    ('e13', 'perform', 'done', None), ('e14', 'done', 'notify', 'Да'), ('e15', 'notify', 'confirm', None),
    ('e16', 'confirm', 'close', None), ('e17', 'complete', 'ask', 'Нужны сведения'), ('e18', 'ask', 'receive-more', None),
    ('e19', 'receive-more', 'check', 'Повторная проверка'), ('e20', 'need', 'clarify', 'Да'),
    ('e21', 'clarify', 'clarified', None), ('e22', 'clarified', 'examine', 'Повторное рассмотрение'),
    ('e23', 'positive', 'refuse', 'Нет'), ('e24', 'done', 'retry', 'Повторная попытка'), ('e25', 'retry', 'perform', None),
    ('e26', 'register', 'cancel', 'Отмена')]
_MAIN = ['get', 'register', 'ack', 'check', 'complete', 'assign', 'examine', 'need', 'draft', 'agree', 'positive']
_SUCCESS = (_MAIN + ['handoff', 'perform', 'done', 'notify', 'confirm', 'close'], [f'e{i}' for i in range(1, 17)])
LARGE_SCENARIOS = [
    ('ok', 'Успешное завершение', *_SUCCESS),
    ('more', 'Дополнение сведений', _SUCCESS[0] + ['ask', 'receive-more'], _SUCCESS[1] + ['e17', 'e18', 'e19']),
    ('clarify', 'Уточнение при рассмотрении', _SUCCESS[0] + ['clarify', 'clarified'], _SUCCESS[1] + ['e20', 'e21', 'e22']),
    ('refuse', 'Отказ', _MAIN + ['refuse'], [f'e{i}' for i in range(1, 11)] + ['e23']),
    ('retry', 'Повторное исполнение', _SUCCESS[0] + ['retry'], _SUCCESS[1] + ['e24', 'e25']),
    ('cancel', 'Отмена', ['get', 'register', 'cancel'], ['e1', 'e26'])]


def large_bundle():
    return graph_bundle(LARGE_STAGES, LARGE_OPERATIONS, LARGE_TRANSITIONS, LARGE_SCENARIOS)


TOKEN = 'ПодтверждениеПлатежаПослеПовторнойПроверкиКлиентаИБанкаВерсия2'
CODE_TOKEN = 'payment_confirmation_handler_after_repeated_client_and_bank_checks'
LONG = ('если заявитель предоставил все обязательные документы, проверка подлинности подписи прошла без замечаний, '
        'срок рассмотрения ещё не истёк, по заявке нет открытых обращений в службу поддержки и решений о приостановке, '
        'полномочия представителя подтверждены доверенностью, а согласие на обработку персональных данных получено '
        'до начала проверки')
STRESS_STAGES = [('first', 'Первый этап'),
                 ('second', 'Второй этап с очень длинным названием, которое переносится целиком, без сокращения и многоточия'),
                 ('empty', 'Этап без операций'), ('last', 'Последний этап')]
STRESS_OPERATIONS = [('a1', 'first', 'start', 'Начать обработку'),
                     ('a2', 'first', 'decision', 'Нужна ли повторная проверка всех приложенных документов заявителя?'),
                     ('b1', 'second', 'operation', 'Обработать ' + TOKEN),
                     ('b2', 'second', 'operation', 'Проверить результат обработки'),
                     ('d1', 'last', 'end', 'Завершить'),
                     ('d2', 'last', 'operation', 'Ручная сверка вне сценариев'),
                     ('x1', 'absent', 'operation', 'Операция без известного этапа')]
STRESS_TRANSITIONS = [('p1', 'a1', 'a2', 'Да'), ('p2', 'a1', 'a2', 'Нет, но продолжить с замечанием'),
                      ('loop', 'a2', 'a2', 'Повторить проверку'), ('f1', 'a2', 'b1', LONG),
                      ('f2', 'b1', 'b2', 'вызов ' + CODE_TOKEN + ' завершён'),
                      ('back', 'b2', 'a1', 'Вернуть на доработку'), ('rework', 'b2', 'b1', 'Исправить'),
                      ('skip', 'a2', 'd1', 'Пропустить проверку'), ('o1', 'b2', 'x1', 'В неизвестный этап'),
                      ('o2', 'x1', 'd1', None), ('dangling', 'a1', 'missing', 'Не рисуется')]
_CORE = ['a1', 'a2', 'b1', 'b2']
STRESS_SCENARIOS = [('s1', 'Основной', _CORE, ['p1', 'f1', 'f2']), ('s2', 'Параллельная ветка', ['a1', 'a2'], ['p2']),
                    ('s3', 'Повтор проверки', ['a1', 'a2'], ['p1', 'loop']),
                    ('s4', 'Возврат', _CORE, ['p1', 'f1', 'f2', 'back']),
                    ('s5', 'Исправление', _CORE, ['p1', 'f1', 'f2', 'rework']),
                    ('s6', 'Пропуск', ['a1', 'a2', 'd1'], ['p1', 'skip']),
                    ('s7', 'Неизвестный этап', _CORE + ['x1', 'd1'], ['p1', 'f1', 'f2', 'o1', 'o2'])]


def stress_bundle():
    return graph_bundle(STRESS_STAGES, STRESS_OPERATIONS, STRESS_TRANSITIONS, STRESS_SCENARIOS)


def svg_of(page):
    return page[page.index('<svg'):page.index('</svg>')]


def proposal():
    data=map_response()
    data['operations']=[
        {'id':'submit-op','name':'Подать заявку','stage_id':'submit','kind':'start','description':'Начать обработку.',
         'actor':None,'event_keys':['order.submitted'],'refs':[ref(1,3,'submit')]},
        {'id':'approve-op','name':'Принять решение','stage_id':'approve','kind':'decision','description':'Проверить заявку.',
         'actor':None,'event_keys':[],'refs':[ref(5,6,'approve')]},
        {'id':'ship-op','name':'Отгрузить заказ','stage_id':'ship','kind':'end','description':'Отправить заказ.',
         'actor':None,'event_keys':[],'refs':[]}]
    data['operation_transitions']=[
        {'id':'a','from_operation':'submit-op','to_operation':'approve-op','condition':None,'refs':[]},
        {'id':'b','from_operation':'approve-op','to_operation':'ship-op','condition':'Одобрено','refs':[]}]
    data['scenarios']=[{'id':'main','name':'Основной','description':'От подачи до отгрузки.',
                        'operation_ids':['submit-op','approve-op','ship-op'],'transition_ids':['a','b']}]
    return data


class ScenarioGraphTests(unittest.TestCase):
    def run_proposal(self,data):
        return analyse(data,instrumentation_response(),metrics_response())[0]

    def test_operations_are_inside_regions_and_keep_required_unimplemented_action(self):
        bundle=self.run_proposal(proposal())
        page=build_html(bundle)
        from test_report import ReportTests
        ReportTests.assertSafeMarkup(self,page)
        self.assertEqual(page.count('class="stage-region"'),3)
        self.assertEqual(page.count('class="node operation-node"'),3)
        self.assertEqual(page.count('class="scenario-edge"'),2)
        self.assertIn('Отгрузить заказ',page)
        self.assertEqual(bundle['process_map']['operations'][2]['evidence_ids'],[])
        self.assertEqual(bundle['process_map']['stages'][2]['observed']['state'],'unknown')
        self.assertIn('Отгрузить заказ',build_markdown(bundle))

    def test_no_order_is_invented_from_operation_or_logging_lists(self):
        data=proposal();data['operation_transitions']=[];data['scenarios'][0]['transition_ids']=[]
        page=build_html(self.run_proposal(data))
        self.assertNotIn('class="scenario-edge"',page)
        self.assertEqual(page.count('class="node operation-node"'),3)
        self.assertNotIn('Место в сценарии уточняется',page)

    def test_unassigned_operations_stay_visible_without_inventing_a_route(self):
        data=proposal();data['scenarios']=[]
        page=build_html(self.run_proposal(data))
        self.assertEqual(page.count('class="node operation-node"'),3)
        self.assertEqual(page.count('class="scenario-edge"'),2)
        self.assertIn('data-scenario="all"',page)
        self.assertNotIn('data-scenario="1"',page)

    def test_source_coverage_uses_pale_colors_and_no_code_binding_stays_visible(self):
        from process_observability import report as r
        from process_observability.scenario_graph import operation_status
        bundle=self.run_proposal(proposal())
        run=r._Run(bundle)
        operations=bundle['process_map']['operations']
        self.assertEqual(operation_status(run,operations[0]),'code_logs')
        self.assertEqual(operation_status(run,operations[1]),'code_only')
        self.assertEqual(operation_status(run,operations[2]),'unlinked')
        page=build_html(bundle)
        self.assertIn('fill="#edf6ef"',page)
        self.assertIn('fill="#fff3e7"',page)
        self.assertIn('data-coverage="unlinked"',page)
        self.assertIn('stroke-dasharray="5 4"',page)

    def test_operation_needing_an_unemitted_key_is_not_painted_as_logged(self):
        from process_observability import report as r
        from process_observability.scenario_graph import operation_status
        data=proposal()
        data['event_mappings'].append({**data['event_mappings'][0],'event_key':'order.completed'})
        data['facts'][0]['event_keys'].append('order.completed')
        data['operations'][0]['event_keys'].append('order.completed')
        bundle=self.run_proposal(data)
        fact=bundle['log_coverage']['facts'][0]
        self.assertEqual((fact['event_keys'],fact['instrumentation']),(['order.submitted','order.completed'],'unknown'))
        self.assertEqual(fact['evidence_ids'],['code:app.py:2-2:submit'])
        self.assertEqual(operation_status(r._Run(bundle),bundle['process_map']['operations'][0]),'code_only')
        self.assertRegex(build_html(bundle),r'data-node-id="submit-op"[^>]*data-coverage="code_only"')
        # With its own static emit in the cited range, the second key makes the operation green again.
        data['event_mappings'][-1]['refs']=[ref(3,3,'submit')]
        candidates=[{'path':'app.py','line':2,'event_key':'order.submitted'},
                    {'path':'app.py','line':3,'event_key':'order.completed'}]
        bundle=analyse(data,instrumentation_response(),metrics_response(),snapshot=FakeSnapshot(candidates=candidates))[0]
        self.assertEqual(bundle['log_coverage']['facts'][0]['instrumentation'],'found')
        self.assertEqual(operation_status(r._Run(bundle),bundle['process_map']['operations'][0]),'code_logs')

    def test_scenario_buttons_enumerate_actual_routes_and_have_matching_markers(self):
        data=proposal();data['scenarios'].append({'id':'review-only','name':'Только проверка',
            'description':'До решения.','operation_ids':['submit-op','approve-op'],'transition_ids':['a']})
        page=build_html(self.run_proposal(data))
        self.assertIn('Только проверка',page)
        self.assertIn('data-scenario="2" data-route-color="#a58eb6"',page)
        self.assertIn('id="scenario-arrow-2"',page)
        self.assertIn('data-scenarios="1 2"',page)

    def test_map_evidence_includes_sources_cited_only_by_operations(self):
        data=proposal();data['operations'][0]['refs']=[ref(3,3,'submit')]
        bundle=self.run_proposal(data)
        self.assertIn('code:app.py:3-3:submit',bundle['process_map']['evidence_ids'])

    def test_dangling_and_ambiguous_graph_references_reject_the_phase(self):
        changes=[
            lambda p:p['operations'][0].update(stage_id='absent'),
            lambda p:p['operations'][0].update(event_keys=['not.mapped']),
            lambda p:p['operations'][1].update(id='submit-op'),
            lambda p:p['operations'][0].update(kind='confirmed'),
            lambda p:p['operation_transitions'][0].update(to_operation='missing'),
            lambda p:p['scenarios'][0].update(operation_ids=['submit-op']),
            lambda p:p['scenarios'][0].update(transition_ids=['missing'])]
        for change in changes:
            data=proposal();change(data)
            with self.subTest(change=change):
                bundle=self.run_proposal(data)
                self.assertEqual(bundle['manifest']['stages']['map']['status'],'incomplete')

    def test_source_only_table_has_no_execution_opportunity_or_delivery_columns(self):
        bundle=self.run_proposal(proposal())
        page,text=build_html(bundle),build_markdown(bundle)
        self.assertNotIn('<th>Возможности</th>',page)
        self.assertNotIn('<th>Доставка</th>',page)
        table=next(line for line in text.splitlines() if line.startswith('| Факт |'))
        self.assertNotIn('Возможности',table);self.assertNotIn('Доставка',table)
        self.assertEqual(bundle['log_coverage']['facts'][0]['delivery'],'unknown')
        self.assertEqual(bundle['log_coverage']['facts'][0]['opportunities']['state'],'unknown')

    def test_labels_are_escaped_and_boilerplate_is_absent(self):
        data=proposal();data['operations'][0]['name']='</text><script>steal()</script>'
        bundle=self.run_proposal(data);original=copy.deepcopy(bundle)
        page=build_html(bundle)
        self.assertEqual(page.count('<script>'),1)
        self.assertIn('&lt;script&gt;steal()&lt;/script&gt;',page)
        for phrase in ('Проверенная цитата:','смысл и выполнение не подтверждены','Черновик по исходникам','Карта и рекомендации — черновик'):
            self.assertNotIn(phrase,page)
        self.assertEqual(bundle,original)

    def test_logging_table_has_only_useful_columns_and_plain_titles(self):
        bundle=self.run_proposal(proposal())
        bundle['logging_inventory']={'scope':{'sites_returned':1,'files_scanned':1},'sites':[
            {'path':'app.py','start_line':2,'callee':'emit','event_key':'order.submitted',
             'fields':['order_id'],'kind':'generic_emit','extractor':'python_ast'}]}
        page=build_html(bundle)
        for text in ('<th>Тип</th>','<th>Извлечение</th>','generic_emit','python_ast','инструментирован'):
            self.assertNotIn(text,page.lower() if text=='инструментирован' else page)
        self.assertIn('<th>Где записывается</th>',page)
        self.assertIn('3. Доработки логирования',page)
        self.assertIn('3. Доработки логирования',build_markdown(bundle))
        self.assertEqual(bundle['logging_inventory']['sites'][0]['extractor'],'python_ast')


class ScenarioLayoutTests(unittest.TestCase):
    FIXTURES = {'run05': run05_bundle, 'large': large_bundle, 'stress': stress_bundle}

    def layout(self, name):
        return build_layout(r._Run(self.FIXTURES[name]()))

    def test_every_view_of_every_fixture_satisfies_the_layout_invariants(self):
        expected = {'run05': ['all', '1', '2', '3', '4'],
                    'large': ['all', '1', '2', '3', '4', '5', '6', 'unassigned'],
                    'stress': ['all', '1', '2', '3', '4', '5', '6', '7', 'unassigned']}
        for name in self.FIXTURES:
            with self.subTest(fixture=name):
                lay = self.layout(name)
                self.assertEqual([view.key for view in lay.views], expected[name])
                self.assertEqual(verify(lay), [])

    def test_fixtures_exercise_every_route_kind(self):
        kinds, modes = set(), set()
        for name in self.FIXTURES:
            for view in self.layout(name).views:
                kinds |= {plan.kind for plan in view.plans.values()}
                modes |= {plan.mode for plan in view.plans.values()}
        self.assertEqual(kinds, {'A', 'B', 'C', 'D'})
        self.assertLessEqual({'straight', 'middle', 'vertical', 'lane', 'band'}, modes)
        stress = self.layout('stress')
        numbers = {key: number for number, (key, *_) in enumerate(STRESS_TRANSITIONS, 1)}
        everything = stress.view('all')
        self.assertEqual(everything.plans[numbers['loop']].kind, 'D')    # self-loop in the stage's lane
        self.assertEqual(everything.plans[numbers['back']].kind, 'B')    # back to an earlier stage
        self.assertEqual({everything.plans[numbers['p1']].kind, everything.plans[numbers['p2']].kind}, {'C', 'D'})

    def test_names_conditions_and_titles_are_rendered_in_full(self):
        page = build_html(stress_bundle())
        from test_report import ReportTests
        ReportTests.assertSafeMarkup(self, page)
        svg = svg_of(page)
        self.assertNotIn('…', svg)
        lay = self.layout('stress')
        texts = [node.text for node in lay.nodes] + [edge.label for edge in lay.edges if edge.label]
        texts += [column.title for column in lay.columns]
        for text in texts:
            self.assertEqual(text.rebuilt(), text.source)
            for line in text.lines:
                self.assertTrue(fits(line, text.size, text.limit), line)
                self.assertIn('>' + html.escape(line) + '</tspan>', svg)
        sources = {text.source for text in texts}
        for expected in [name for *_, name in STRESS_OPERATIONS] + [name for _, name in STRESS_STAGES]:
            self.assertIn(normalize(expected), sources)
        for *_, condition in STRESS_TRANSITIONS[:-1]:
            if condition:
                self.assertIn(normalize(condition), sources)
        self.assertGreater(len(next(column.title for column in lay.columns if 'длинным' in column.name).lines), 1)

    def test_long_tokens_and_conditions_wrap_without_losing_characters(self):
        self.assertGreaterEqual(len(TOKEN), 60)
        self.assertGreaterEqual(len(LONG), 300)
        for token, size, limit in ((TOKEN, NAME, 180), (CODE_TOKEN, LABEL, LABEL_LIMIT)):
            text = wrap(token, *size, limit)
            self.assertGreater(len(text.lines), 1)
            self.assertEqual(set(text.joins), {''})
            self.assertEqual(''.join(text.lines), token)
        condition = wrap(LONG, *LABEL, LABEL_LIMIT)
        self.assertGreater(len(condition.lines), 5)
        self.assertEqual(condition.rebuilt(), normalize(LONG))
        self.assertTrue(all(fits(line, condition.size, condition.limit) for line in condition.lines))

    def test_scenario_views_keep_stage_columns_and_put_scenario_operations_first(self):
        lay = self.layout('large')
        index = {key: number for number, (key, *_) in enumerate(LARGE_OPERATIONS)}
        for view in lay.views:
            for node in lay.nodes:
                self.assertEqual(node.x, lay.columns[node.column].node_x)
            if view.key == 'all':
                self.assertIsNone(view.separator)
                continue
            self.assertIsNotNone(view.separator)
            for column in range(len(lay.columns)):
                chosen = [view.node_y[n.key] for n in lay.nodes if n.column == column and n.key in view.active_nodes]
                other = [view.node_y[n.key] for n in lay.nodes if n.column == column and n.key not in view.active_nodes]
                if chosen and other:
                    self.assertLess(max(chosen), min(other))
        self.assertEqual(lay.view('6').active_nodes, frozenset(index[key] for key in ('get', 'register', 'cancel')))
        self.assertEqual(lay.view('unassigned').active_nodes, frozenset({index['pause']}))
        # Within a stage, explicit order wins over list order; the rework edge does not reorder.
        everything = lay.view('all')
        prepare = sorted((everything.rows[index[key]], key) for key in ('check', 'complete', 'ask', 'receive-more'))
        self.assertEqual([key for _, key in prepare], ['check', 'complete', 'ask', 'receive-more'])

    def test_every_view_carries_geometry_and_route_colours_match_chips(self):
        page = build_html(stress_bundle())
        views = re.search(r'data-views="([^"]+)"', page).group(1).split(' ')
        self.assertEqual(views, ['all'] + [str(i) for i in range(1, 8)] + ['unassigned'])
        chips = dict(re.findall(r'data-scenario="(\d+)" data-route-color="(#[0-9a-f]{6})"', page))
        markers = dict(re.findall(r'<marker id="scenario-arrow-(\d+)"[^>]*><path [^>]*stroke="(#[0-9a-f]{6})"', page))
        self.assertEqual(len(chips), 7)
        self.assertEqual(chips, markers)
        self.assertEqual(chips['7'], ROUTES[0][0])
        styles = re.findall(r'data-style="([^"]+)"', page)
        self.assertEqual(len(styles), page.count('class="scenario-edge"'))
        for value in styles:
            parts = value.split('|')
            self.assertEqual(len(parts), len(views))
            for key, style in zip(views, parts):
                color, marker, _, _, active = style.split(' ')
                if key.isdigit() and active == '1':
                    self.assertEqual((color, marker), (chips[key], f'scenario-arrow-{key}'))
                elif key != 'all' and active == '0':
                    self.assertEqual(marker, 'scenario-arrow-context')
        for attribute, separator in (('data-ys', ' '), ('data-as', ' '), ('data-order', ' '),
                                     ('data-d', '|'), ('data-l', '|'), ('data-hs', ' '), ('data-counts', '|')):
            values = re.findall(' ' + attribute + r'="([^"]+)"', page)
            self.assertTrue(values, attribute)
            for value in values:
                self.assertEqual(len(value.split(separator)), len(views), attribute)

    def test_only_explicit_transitions_are_drawn(self):
        page = build_html(stress_bundle())
        svg = svg_of(page)
        self.assertEqual(svg.count('class="scenario-edge"'), len(STRESS_TRANSITIONS) - 1)
        self.assertNotIn('Не рисуется', svg)
        lay = self.layout('large')
        self.assertEqual(len(lay.edges), len(LARGE_TRANSITIONS))
        for view in lay.views:
            self.assertEqual(sorted(view.routes), [edge.number for edge in lay.edges])

    def test_empty_stage_and_unknown_stage_operation_stay_visible(self):
        svg = svg_of(build_html(stress_bundle()))
        self.assertEqual(svg.count('class="stage-region"'), len(STRESS_STAGES) + 1)
        self.assertEqual(svg.count('class="node operation-node"'), len(STRESS_OPERATIONS))
        self.assertIn('Операции пока не описаны', svg)
        self.assertIn('Этап не указан', svg)
        self.assertIn('aria-label="Операция без известного этапа · Этап не указан · ', svg)

    def test_long_failure_branch_keeps_its_label_in_the_source_stage(self):
        lay = self.layout('run05')
        failure = next(edge for edge in lay.edges if edge.number == 3)
        self.assertEqual(failure.label.source, normalize(RUN05_TRANSITIONS[2][3]))
        column = lay.columns[failure.source.column]
        for view in lay.views:
            box = view.labels[3]
            self.assertGreaterEqual(box.x, column.node_x + 220)
            self.assertLessEqual(box.x + box.w, column.x + column.w)

    def test_context_is_gray_but_keeps_opaque_legible_text(self):
        from process_observability.compact_report import CSS, SCRIPT
        self.assertIn('.scenario-edge[data-active="0"] .edge-label{fill:#5f6b73}', CSS)
        self.assertIn('.operation-node[data-active="0"] .op-name{fill:#5f6b73}', CSS)
        self.assertNotRegex(CSS, r'\.scenario-edge\[data-active="0"\]\{[^}]*opacity')
        self.assertNotRegex(CSS, r'\.operation-node\[data-active="0"\]\{[^}]*opacity')
        self.assertNotIn('context-dim', CSS + SCRIPT)
        self.assertNotIn("fit-map')?.click()", SCRIPT)
        page = build_html(run05_bundle())
        self.assertIn('data-active="0"', page)
        self.assertEqual(page.count('class="node operation-node"'), len(RUN05_OPERATIONS))

    def test_graph_is_an_accessible_group_with_a_text_equivalent(self):
        page = build_html(run05_bundle())
        self.assertIn('role="group" aria-label="Сценарии процесса: операции внутри этапов"', page)
        self.assertNotIn('role="img"', page)
        self.assertIn('data-scenario="1" data-route-color="#7f99ad" type="button" aria-pressed="true"', page)
        self.assertIn('id="scenario-readout" aria-live="polite"', page)
        routes = page[page.index('<details class="route-list">'):]
        self.assertIn(html.escape('Списать сумму со счёта отправителя → Перевод не выполнен'), routes)
        self.assertIn('<pre id="layout-check-result" class="layout-check" hidden></pre>', page)
        self.assertIn('ProcessGraph Sans', page)

    def test_rendering_is_deterministic_and_leaves_the_bundle_unchanged(self):
        bundle = large_bundle()
        snapshot = copy.deepcopy(bundle)
        self.assertEqual(build_html(bundle), build_html(bundle))
        self.assertEqual(bundle, snapshot)

    def test_verifier_reports_moved_labels_overlaps_and_changed_text(self):
        lay = self.layout('run05')
        view = lay.views[0]
        number, box = sorted(view.labels.items())[0]
        view.labels[number] = Box(box.x, box.y + 40, box.w, box.h)
        view.node_y[5] = view.node_y[4]
        node = lay.nodes[1]
        node.text = dataclasses.replace(node.text, lines=(node.text.lines[0] + '…',) + node.text.lines[1:])
        problems = verify(lay)
        self.assertIn(f'all: label-detached: {number}', problems)
        self.assertIn('all: overlap: node 4 / node 5', problems)
        self.assertIn('*: text-changed: node 1', problems)


if __name__=='__main__':unittest.main()
