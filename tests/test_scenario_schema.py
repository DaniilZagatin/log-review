"""The scenario gate verifies provenance and graph invariants, without an LLM."""
from copy import deepcopy
from types import SimpleNamespace
import unittest

from pko.scenario_map.schema import EXECUTION_CHECKS, MapState, MapValidationError
from pko.scenario_map.tools import TOOLS
from quality_helpers import explain_args, review_payload as semantic_payload


class MemoryTree:
    def __init__(self):
        self.content = {
            "source-1/channel.py": "def submit(request):\n    return bank.post('/cards', request)\npassword = 'not-for-output'\n",
            "source-2/cards.py": "@route('/cards')\ndef accept(request):\n    bus.publish('card.issue', request.id)\n",
            "source-3/worker.py": "@consume('card.issue')\ndef issue(request_id):\n    return persist_card(request_id)\n",
            "source-1/.env": "SECRET=sensitive\n",
        }
        self.files = list(self.content)

    def read(self, path):
        return self.content.get(path)


def workspace(count=3):
    return SimpleNamespace(
        tree=MemoryTree(), notes=[], extraction=None,
        sources=[{"id": f"source-{i}", "name": f"system-{i}", "kind": "directory", "digest": str(i), "file_count": 1}
                 for i in range(1, count + 1)],
    )


class ScenarioMapTests(unittest.TestCase):
    def setUp(self):
        self.state = MapState(workspace(), "Выпуск дебетовой карты действующему клиенту")

    def good(self, tool_name, **args):
        if tool_name in ('upsert_step', 'upsert_transition'):
            args = explain_args(args)
        if tool_name == "finish":
            self.review_current_operations()
        response = self.state.dispatch(tool_name, args)
        self.assertTrue(response["ok"], response)
        if tool_name == "review_execution":
            result = self.state.dispatch("review_semantics", semantic_payload(self.state))
            self.assertTrue(result["ok"], result)
        return response

    def cite(self, path="source-1/channel.py", start=1, end=1):
        self.state.observe_read(path, start, end - start + 1)
        from pko.code_tools.tools import _mask_secrets
        quote = "\n".join(_mask_secrets(s) for s in self.state.workspace.tree.read(path).splitlines()[start - 1:end])
        return [{"path": path, "line_start": start, "line_end": end, "quote": quote}]

    def system(self, source=1):
        # Historical fixture selector only, never a domain object or tool call.
        return f"source-{source}"

    def node(self, system=None, kind="operation", evidence=None):
        identifier = self.good("upsert_step", name="Получение результата", kind="operation",
                         outcome="success" if kind == "end" else "unknown",
                         continuation_mode="choice" if kind == "decision" else "all" if kind == "fork" else "sequence",
                         description="Самостоятельное действие над обращением", evidence_status="observed",
                         evidence=evidence or self.cite())["id"]
        if kind in ("start", "end"):
            field = 'entry_operation_ids' if kind == 'start' else 'terminal_operation_ids'
            self.state.model['scenario'][field].append(identifier)
        return identifier

    def edge(self, left, right, kind="sequence", **extra):
        if left in self.state.model['scenario']['entry_operation_ids'] and kind != 'data':
            extra.setdefault('condition', 'Выбран тестовый маршрут к операции '+right)
            if any(e['source']==left and e['kind']!='data' for e in self.state.model['edges']):
                current=next(n for n in self.state.model['nodes'] if n['id']==left)
                self.good('upsert_step', **{**current, 'continuation_mode':'choice'})
        return self.good("upsert_transition", source=left, target=right, kind=kind,
                         description="Следующий шаг", evidence_status="observed", evidence=self.cite(), **extra)["id"]

    def complete_small_map(self):
        self.good("set_scope", title="Оформление карты", start="Получена заявка", end="Карта выпущена", exclusions=["Перевыпуск"])
        system = self.system()
        first, last = self.node(system, "start"), self.node(system, "end")
        operation = self.node(system)
        self.edge(first, operation)
        self.edge(operation, last)
        self.good("review_source", source_id="source-1", disposition="reviewed", reason="Прочитана точка входа")
        for source, path in (("source-2", "source-2/cards.py"), ("source-3", "source-3/worker.py")):
            self.state.observe_read(path, 1, 1)
            self.good("review_source", source_id=source, disposition="out_of_scope", reason="Другой вариант сценария")
        self.group_current_actions()
        return system, first, last

    def review_payload(self, system=None, node_ids=None):
        nodes = [node for node in self.state.model['nodes'] if node['evidence_status'] != 'unknown'
                 and (node_ids is None or node['id'] in node_ids)]
        evidence = []
        for node in nodes:
            for ref in node['evidence']:
                if ref not in evidence: evidence.append(deepcopy(ref))
        operations = [{'node_id': node['id'], 'name':node['name'], 'kind':'action',
                       'action':'Выполнить самостоятельное действие над обращением', 'effect':'Получен результат действия',
                       'failure':'Обработка отказов не найдена в данном участке', 'evidence':deepcopy(node['evidence'])} for node in nodes]
        checks = {name:{'status':'not_found','explanation':'В прочитанном участке не найдено','evidence':deepcopy(evidence)} for name in EXECUTION_CHECKS}
        selected_ids = {n['id'] for n in nodes}
        incident = [e for e in self.state.model['edges'] if e['source'] in selected_ids or e['target'] in selected_ids]
        for category, kinds in [('branches',('branch',)),('failures',('error','retry')),('integrations',('call','event','return'))]:
            selected = [e['id'] for e in incident if e['kind'] in kinds or (category=='branches' and sum(x['source']==e['source'] and x['kind']!='data' for x in incident)>1)]
            if selected:checks[category].update(status='present',explanation='Найдено и отражено переходами',edge_ids=selected)
        payload={'name':'Исследованный участок','evidence':evidence,'operations':operations,'checks':checks}
        if self.state.model['execution_reviews']:payload['id']=self.state.model['execution_reviews'][0]['id']
        return payload

    def review_current_operations(self):
        payload=self.review_payload()
        if payload['operations']:self.good('review_execution',**payload)
        self.good('review_semantics',**semantic_payload(self.state))

    def group_current_actions(self):
        nodes = [n for n in self.state.model["nodes"] if n["kind"] == "operation"
                 and n["evidence_status"] != "unknown" and not n.get("group_id")]
        if not nodes:
            return
        stage = self.good("upsert_group", name="Подача заявки", kind="stage", result="Обращение принято",
                          evidence=nodes[0]["evidence"])["id"]
        for node in nodes:
            self.good("upsert_step", **{**node, "group_id": stage})

    def test_unread_quote_is_rejected_and_failed_mutation_is_atomic(self):
        before = self.state.snapshot()
        response = self.state.dispatch("upsert_step", {"name": "Получение обращения", "evidence_status": "observed",
            "evidence": [{"path": "source-1/channel.py", "line_start": 1, "line_end": 1, "quote": "def submit(request):"}]})
        self.assertFalse(response["ok"])
        self.assertIn("read_file", response["error"])
        self.assertEqual(before, self.state.snapshot())
        self.assertEqual(self.node(), "step-1")

    def test_stages_are_separate_from_flow_and_can_span_sources(self):
        first, second = self.system(), self.system(2)
        evidence = self.cite() + self.cite("source-2/cards.py")
        stage = self.good("upsert_group", name="Принять заявку", kind="stage", result="Заявка доставлена", evidence=evidence)["id"]
        for system, refs in ((first, evidence[:1]), (second, evidence[1:])):
            self.good("upsert_step", name="Обработка запроса", kind="operation",
                      group_id=stage, evidence_status="observed", evidence=refs)
        self.assertEqual(len(self.state.model["nodes"]), 2)
        self.assertEqual(self.state.model["edges"], [])
        self.assertEqual(self.good("inspect_map", section="groups")["total"], 1)
        before = self.state.snapshot()
        self.assertFalse(self.state.dispatch("remove_element", {"kind": "group", "id": stage})["ok"])
        self.assertEqual(before, self.state.snapshot())

    def test_stage_membership_and_evidence_are_checked_transactionally(self):
        evidence = self.cite()
        payload = dict(name="Этап", kind="stage", result="Результат", evidence=evidence)
        self.assertFalse(self.state.dispatch("upsert_group", {**payload, "evidence": []})["ok"])
        stage = self.good("upsert_group", **payload)["id"]
        before = self.state.snapshot()
        for parent in (None, "missing", stage):
            self.assertFalse(self.state.dispatch("upsert_group", {**payload, "kind": "operation", "parent_id": parent})["ok"])
            self.assertEqual(before, self.state.snapshot())
        self.assertFalse(self.state.dispatch("upsert_group", {**payload, "id": stage, "parent_id": stage})["ok"])
        node = self.node(self.system())
        current = next(n for n in self.state.model["nodes"] if n["id"] == node)
        self.good("upsert_step", **{**current, "group_id": stage})
        before = self.state.snapshot()
        self.assertFalse(self.state.dispatch("upsert_step", {**current, "group_id": "missing"})["ok"])
        self.assertEqual(before, self.state.snapshot())


    def test_empty_groups_cannot_finish(self):
        self.complete_small_map()
        self.review_current_operations()
        self.good("upsert_group", name="Неиспользуемый этап", kind="stage", result="Ожидаемый результат", evidence=self.cite())
        response = self.state.dispatch("finish", {"summary": "Исследовано"})
        self.assertFalse(response["ok"])
        self.assertIn("пустая группа", response["error"])

    def test_terminal_outcome_is_explicit_and_not_inferred_from_name(self):
        system = self.system()
        first = self.good("upsert_step", name="Успех", kind="operation", evidence=self.cite(), evidence_status="observed")["id"]
        node = next(n for n in self.state.model["nodes"] if n["id"] == first)
        self.assertEqual(node["outcome"], "unknown")
        for outcome in ("success", "refusal", "failure", "cancelled"):
            self.good("upsert_step", **{**node, "outcome": outcome})
        self.assertFalse(self.state.dispatch("upsert_step", {**node, "outcome": "invented"})["ok"])
        self.assertTrue(self.state.dispatch("upsert_step", {**node, "kind": "operation", "outcome": "deferred"})["ok"])

    def test_unknown_terminal_outcome_requires_an_addressed_gap_and_stays_partial(self):
        _, first, last = self.complete_small_map()
        node = next(node for node in self.state.model['nodes'] if node['id'] == last)
        self.good('upsert_step', **{**node, 'outcome': 'unknown'})
        self.review_current_operations()
        response = self.state.dispatch('finish', {'summary': 'Исследованы переданные источники'})
        self.assertFalse(response['ok'])
        self.assertIn('бизнес-исход конечной операции не установлен', response['error'])
        self.good('add_gap', description='Не установлен итог получения результата', node_ids=[first], evidence=self.cite())
        self.assertTrue(any('бизнес-исход конечной операции' in error for error in self.state.quality_errors()),
                        'An unrelated gap must not stand in for the terminal outcome')
        self.good('add_gap', description='Не установлен бизнес-итог последней операции', node_ids=[last], evidence=self.cite())
        self.assertEqual(self.good('finish', summary='Неуточнённый исход сохранён явно')['status'], 'partial')

    def test_decision_requires_labels_even_for_non_branch_edges(self):
        system, first, last = self.complete_small_map()
        decision = self.node(system, "decision")
        self.edge(first, decision)
        retry = self.edge(decision, decision, "retry")
        self.edge(decision, last, "branch", condition="Проверки пройдены")
        self.review_current_operations()
        rejected = self.state.dispatch("finish", {"summary": "Проверено"})
        self.assertFalse(rejected["ok"])
        self.assertIn("условий на всех выходах", rejected["error"])
        edge = next(e for e in self.state.model["edges"] if e["id"] == retry)
        self.good("upsert_transition", **{**edge, "condition": "Временная ошибка; повтор разрешён"})
        self.assertEqual(self.good("finish", summary="Подписаны исходы")["status"], "completed")

    def test_only_masked_literal_exact_window_is_accepted(self):
        evidence = self.cite(start=3, end=3)
        self.assertNotIn("not-for-output", evidence[0]["quote"])
        args = {"name": "Получение обращения", "evidence_status": "observed", "evidence": evidence}
        raw = deepcopy(args)
        raw["evidence"][0]["quote"] = "password = 'not-for-output'"
        self.assertFalse(self.state.dispatch("upsert_step", explain_args(raw))["ok"])
        self.assertTrue(self.state.dispatch("upsert_step", explain_args(args))["ok"])
        other = deepcopy(args)
        other["evidence"][0]["line_start"] = 1
        self.assertFalse(self.state.dispatch("upsert_step", other)["ok"])

    def test_registered_ranges_do_not_bridge_unread_lines(self):
        self.cite(start=1)
        self.cite(start=3, end=3)
        response = self.state.dispatch("upsert_step", {"name": "Получение обращения", "evidence_status": "inferred",
            "evidence": [{"path": "source-1/channel.py", "line_start": 1, "line_end": 3, "quote": "some quote"}]})
        self.assertFalse(response["ok"])
        self.assertEqual(self.state.snapshot()["read_coverage"][0]["ranges"], [[1, 1], [3, 3]])

    def test_blocked_file_never_registers(self):
        self.state.observe_read("source-1/.env")
        response = self.state.dispatch("upsert_step", {"name": "АС", "source_ids": ["source-1"], "evidence_status": "observed",
            "evidence": [{"path": "source-1/.env", "line_start": 1, "line_end": 1, "quote": "SECRET=sensitive"}]})
        self.assertFalse(response["ok"])
        self.assertEqual(self.state.snapshot()["read_coverage"], [])

    def test_duplicate_and_unknown_sources_are_rejected(self):
        bad = workspace()
        bad.sources.append(deepcopy(bad.sources[0]))
        with self.assertRaises(MapValidationError):
            MapState(bad, "Сценарий")
        self.assertFalse(self.state.dispatch("upsert_step", {"name": "АС", "source_ids": ["source-1", "source-1"]})["ok"])
        self.assertFalse(self.state.dispatch("upsert_step", {"name": "АС", "source_ids": ["source-44"]})["ok"])




    def test_finish_requires_scope_sources_and_boundaries(self):
        self.node(self.system())
        response = self.state.dispatch("finish", {"summary": "Исследовано"})
        self.assertFalse(response["ok"])
        self.assertIn("scenario.start", response["error"])
        self.assertIn("entry_operation_ids", response["error"])
        self.assertIn("Не рассмотрены источники", response["error"])
        self.assertEqual(self.state.model["run"]["status"], "running")

    def test_completed_only_means_completed_supplied_materials(self):
        self.complete_small_map()
        result = self.good("finish", summary="Цепочка подачи заявки восстановлена")
        self.assertEqual(result["status"], "completed")
        self.assertIn("не подтверждение семантической полноты", result["reason"])
        self.assertFalse(self.state.snapshot()["validation"]["semantic_verification"])

    def test_control_flow_cycles_are_legal_when_they_have_an_exit(self):
        system, first, last = self.complete_small_map()
        operation = self.node(system)
        decision = self.node(system, "decision")
        self.edge(first, operation)
        self.edge(operation, decision)
        self.edge(decision, operation, "retry", condition="Временный отказ; повтор разрешён")
        self.edge(decision, last, "branch", condition="Выпуск завершён")
        self.assertEqual(self.good("finish", summary="Повтор имеет выход")["status"], "completed")


    def test_independent_complete_chains_do_not_form_one_connected_scenario(self):
        system, _, _ = self.complete_small_map()
        other_start = self.node(system, "start")
        other_end = self.node(system, "end")
        self.edge(other_start, other_end)
        result = self.state.dispatch("finish", {"summary": "Исследовано"})
        self.assertFalse(result["ok"])
        self.assertIn("Несвязанный фрагмент", result["error"])
        self.good("add_gap", description="Связь найденного фрагмента с основным сценарием не установлена",
                  node_ids=[other_start, other_end])
        self.assertEqual(self.good("finish", summary="Связь фрагмента неизвестна")["status"], "partial")

    def test_unknown_edge_needs_explicit_gap_with_both_nodes(self):
        _, first, last = self.complete_small_map()
        last = self.state.model["edges"][0]["target"]
        existing = self.state.model["edges"][0]["id"]
        self.good("upsert_transition", id=existing, source=first, target=last, kind="sequence", evidence_status="unknown")
        self.assertFalse(self.state.dispatch("finish", {"summary": "Исследовано"})["ok"])
        self.good("add_gap", description="Не найден вызов", node_ids=[first])
        self.assertFalse(self.state.dispatch("finish", {"summary": "Исследовано"})["ok"])
        self.good("add_gap", description="Нужна реализация перехода", node_ids=[first, last])
        self.assertEqual(self.good("finish", summary="Переход не подтверждён")["status"], "partial")

    def test_inference_and_assumptions_are_not_reported_as_complete(self):
        _, first, _ = self.complete_small_map()
        original = next(n for n in self.state.model["nodes"] if n["id"] == first)
        original = deepcopy(original)
        original["evidence_status"] = "inferred"
        self.good("upsert_step", **original)
        self.assertEqual(self.good("finish", summary="Начало выведено из кода")["status"], "partial")

    def test_removal_is_not_cascading_and_stable_ids_are_not_reused(self):
        system, first, last = self.complete_small_map()
        before = self.state.snapshot()
        self.assertFalse(self.state.dispatch("remove_element", {"kind": "step", "id": first})["ok"])
        self.assertEqual(before, self.state.snapshot())
        edge = self.state.model["edges"][0]["id"]
        self.good("remove_element", kind="transition", id=edge)
        self.good("remove_element", kind="step", id=first)
        self.state.model["scenario"]["entry_operation_ids"].remove(first)
        new = self.node(system, "start")
        self.assertNotEqual(first, new)
        operation = next(n["id"] for n in self.state.model["nodes"] if n["id"] not in self.state.model["scenario"]["entry_operation_ids"] + self.state.model["scenario"]["terminal_operation_ids"])
        self.edge(new, operation)
        self.assertEqual(self.good("finish", summary="Исправлена карта")["status"], "completed")


    def test_unread_source_cannot_be_reviewed_or_used_source_excluded(self):
        self.assertFalse(self.state.dispatch("review_source", {"source_id": "source-1", "disposition": "reviewed", "reason": "Посмотрел"})["ok"])
        self.node()
        self.assertFalse(self.state.dispatch("review_source", {"source_id": "source-1", "disposition": "out_of_scope", "reason": "Лишнее"})["ok"])

    def test_invalid_tools_fields_and_references_are_actionable(self):
        self.assertFalse(self.state.dispatch("made_up", {})["ok"])
        self.assertFalse(self.state.dispatch("set_scope", {"process": "Неверное поле"})["ok"])
        self.assertFalse(self.state.dispatch("upsert_step", {"name": "Шаг", "system_id": "sys-999"})["ok"])
        self.assertFalse(self.state.dispatch("add_gap", {"description": "Пробел", "node_ids": ["step-999"]})["ok"])
        self.assertFalse(self.state.dispatch("upsert_step", {"id": "step-100", "name": "Придуманный ID"})["ok"])
        self.assertFalse(self.state.dispatch("upsert_step", {"name": "Шаг", "kind": "imaginary"})["ok"])
        self.assertFalse(self.state.dispatch("upsert_transition", {"source": "step-1", "target": "step-2", "kind": "branch"})["ok"])

    def test_tool_contract_lists_all_mutation_handlers(self):
        names = [tool["function"]["name"] for tool in TOOLS]
        self.assertEqual(len(set(names)), 14)
        self.assertIn("save_research_brief", names)
        self.assertIn("save_research_note", names)
        self.assertIn("inspect_map", names)
        self.assertIn("finish", names)
        for tool in TOOLS:
            self.assertFalse(tool["function"]["parameters"]["additionalProperties"])

    def test_neutral_notes_do_not_make_complete_map_partial_but_omissions_do(self):
        self.complete_small_map()
        self.state.model["source_notes"] = ["Digest относится к снимку текста, не к ZIP"]
        self.assertEqual(self.good("finish", summary="Исследовано")["status"], "completed")
        self.state.model["source_omissions"] = ["Файл реализации слишком велик и пропущен"]
        self.assertEqual(self.good("finish", summary="Часть реализации недоступна")["status"], "partial")

    def test_inspect_is_paginated_and_does_not_reemit_large_quotes(self):
        self.complete_small_map()
        summary = self.good("inspect_map")
        self.assertEqual(summary["model"]["counts"]["nodes"], 3)
        self.assertNotIn("nodes", summary["model"])
        page = self.good("inspect_map", section="nodes", limit=1)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["next_offset"], 2)
        self.assertNotIn("quote", page["items"][0]["evidence"][0])
        next_page = self.good("inspect_map", section="nodes", offset=3, limit=1)
        self.assertIsNone(next_page["next_offset"])
        self.assertNotEqual(page["items"][0]["id"], next_page["items"][0]["id"])
        self.assertIn("quote", self.state.snapshot()["nodes"][0]["evidence"][0])
        self.assertFalse(self.state.dispatch("inspect_map", {"section": "nodes", "offset": 0})["ok"])
        self.assertFalse(self.state.dispatch("inspect_map", {"section": "not_a_section"})["ok"])


    def test_unread_source_exclusion_is_an_assumption_until_reviewed_again(self):
        self.good("set_scope", title="Оформление карты", start="Получена заявка", end="Карта выпущена")
        system = self.system()
        first, last = self.node(system, "start"), self.node(system, "end")
        operation = self.node(system)
        self.edge(first, operation)
        self.edge(operation, last)
        self.good("review_source", source_id="source-1", disposition="reviewed", reason="Прочитана точка входа")
        self.state.observe_read("source-2/cards.py", 1, 2)
        self.good("review_source", source_id="source-2", disposition="out_of_scope", reason="Другой вариант сценария")
        review = self.good("review_source", source_id="source-3", disposition="out_of_scope", reason="По названию это соседний сценарий")
        self.assertFalse(review["inspected"])
        self.assertEqual(self.good("finish", summary="Один источник исключён предположительно")["status"], "partial")
        self.state.observe_read("source-3/worker.py", 1, 3)
        # Reading alone does not silently rewrite the earlier judgment.
        self.assertEqual(self.good("finish", summary="Нужно обновить вывод по источнику")["status"], "partial")
        review = self.good("review_source", source_id="source-3", disposition="out_of_scope", reason="Прочитан обработчик, относится к соседнему варианту")
        self.assertTrue(review["inspected"])
        self.assertEqual(self.good("finish", summary="Источники рассмотрены")["status"], "completed")



    def test_finish_rejects_coarse_map_without_execution_inventory(self):
        self.complete_small_map()
        response = self.state.dispatch("finish", {"summary": "Общий шаг обработан"})
        self.assertFalse(response["ok"])
        self.assertIn("review_execution", response["error"])
        self.assertEqual(self.state.snapshot()["detail_review"]["status"], "pending")

    def test_two_atomic_effects_cannot_be_hidden_in_one_map_node(self):
        system, _, _ = self.complete_small_map()
        payload = self.review_payload(system)
        second = deepcopy(payload["operations"][0])
        second.update(name="Отправить уведомление", action="Отправить уведомление клиенту", effect="Уведомление передано")
        payload["operations"].append(second)
        before = self.state.snapshot()
        response = self.state.dispatch("review_execution", payload)
        self.assertFalse(response["ok"])
        self.assertIn("несколько действий", response["error"])
        self.assertEqual(self.state.snapshot(), before)

    def test_decision_requires_its_own_operation_record(self):
        system, first, last = self.complete_small_map()
        decision = self.node(system, "decision")
        action = next(node["id"] for node in self.state.model["nodes"] if node["id"] not in self.state.model["scenario"]["entry_operation_ids"] + self.state.model["scenario"]["terminal_operation_ids"])
        self.edge(first, decision)
        self.edge(decision, action, "branch", condition="Проверка пройдена")
        self.edge(decision, last, "branch", condition="Получен отказ")
        self.good("review_execution", **self.review_payload(system, [action]))
        rejected = self.state.dispatch("finish", {"summary": "Проверка не раскрыта"})
        self.assertFalse(rejected["ok"])
        self.assertIn(f"{decision}: операция не сопоставлена", rejected["error"])
        self.review_current_operations()
        self.assertTrue(self.state.dispatch("finish", {"summary": "Проверка показана отдельно"})["ok"])
        detail = self.state.snapshot()["detail_review"]
        self.assertEqual(detail["operation_count"], 4)
        self.assertFalse(detail["semantic_completeness_verified"])

    def test_inventory_requires_read_evidence_and_an_explicit_execution_scope(self):
        system, _, _ = self.complete_small_map()
        payload = self.review_payload(system)
        payload["operations"][0]["evidence"] = [{"path": "source-1/channel.py", "line_start": 1, "line_end": 2}]
        self.assertFalse(self.state.dispatch("review_execution", payload)["ok"])
        self.state.observe_read("source-1/channel.py", 2, 1)
        rejected = self.state.dispatch("review_execution", payload)
        self.assertFalse(rejected["ok"])
        self.assertIn("область обзора", rejected["error"])
        payload["evidence"] = self.cite(start=1, end=2)
        self.assertTrue(self.state.dispatch("review_execution", payload)["ok"])

    def test_detail_review_requires_all_five_investigations(self):
        system, _, _ = self.complete_small_map()
        payload = self.review_payload(system)
        payload["checks"].pop("failures")
        response = self.state.dispatch("review_execution", payload)
        self.assertFalse(response["ok"])
        self.assertIn("failures", response["error"])

    def test_unknown_failure_analysis_needs_addressed_gap_and_stays_partial(self):
        system, _, _ = self.complete_small_map()
        payload = self.review_payload(system)
        payload["checks"]["failures"] = {"status": "unknown", "explanation": "Реализация обработчика исключений отсутствует", "evidence": []}
        self.assertFalse(self.state.dispatch("review_execution", payload)["ok"])
        task=self.good('save_research_task',kind='question',target='Другой участок',status='out_of_scope',resolution='За границами фикстуры',evidence=self.cite())['id']
        unrelated = self.good("add_gap", description="Неясный бизнес-термин", task_ids=[task])["id"]
        payload["checks"]["failures"]["gap_ids"] = [unrelated]
        self.assertFalse(self.state.dispatch("review_execution", payload)["ok"])
        gap = self.good("add_gap", description="Неизвестно поведение при отказе вызова", node_ids=[payload["operations"][0]["node_id"]])["id"]
        payload["checks"]["failures"]["gap_ids"] = [gap]
        self.good("review_execution", **payload)
        result = self.state.dispatch("finish", {"summary": "Обработка отказа требует изучения"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.state.snapshot()["detail_review"]["status"], "partial")

    def test_changed_operation_requires_new_review_but_keeps_operation_id(self):
        system, _, _ = self.complete_small_map()
        first_review = self.good("review_execution", **self.review_payload(system))
        identifier = first_review["operations"][0]["id"]
        node_id = first_review["operations"][0]["node_id"]
        updated = deepcopy(next(node for node in self.state.model["nodes"] if node["id"] == node_id))
        updated["description"] = "Уточнённое действие по коду"
        self.good("upsert_step", **updated)
        response = self.state.dispatch("finish", {"summary": "Уточнена карта"})
        self.assertFalse(response["ok"])
        self.assertIn("изменена после обзора", response["error"])
        updated_review = self.good("review_execution", **self.review_payload(system))
        self.assertEqual(updated_review["operations"][0]["id"], identifier)
        self.assertTrue(self.state.dispatch("finish", {"summary": "Обзор обновлён"})["ok"])

    def test_not_found_cannot_contradict_explicit_retry_in_the_map(self):
        system, _, _ = self.complete_small_map()
        action = next(node["id"] for node in self.state.model["nodes"] if node["id"] not in self.state.model["scenario"]["entry_operation_ids"] + self.state.model["scenario"]["terminal_operation_ids"])
        self.edge(action, action, "retry", condition="Повтор после временного сбоя")
        payload = self.review_payload(system)
        payload["checks"]["failures"] = {"status": "not_found", "explanation": "Не нашёл", "evidence": deepcopy(payload["evidence"])}
        self.good("review_execution", **payload)
        response = self.state.dispatch("finish", {"summary": "Проверено"})
        self.assertFalse(response["ok"])
        self.assertIn("not_found противоречит", response["error"])

    def test_changed_sequence_edge_not_listed_in_checks_requires_new_review(self):
        system, _, _ = self.complete_small_map()
        self.good("review_execution", **self.review_payload(system))
        edge = deepcopy(self.state.model["edges"][0])
        self.assertFalse(any(check["edge_ids"] for check in self.state.model["execution_reviews"][0]["checks"].values()))
        edge["condition"] = "Только после подтверждения заявки"
        self.good("upsert_transition", **edge)
        result = self.state.dispatch("finish", {"summary": "Уточнён порядок исполнения"})
        self.assertFalse(result["ok"])
        self.assertIn(f"переход {edge['id']} изменён после обзора", result["error"])
        self.good("review_execution", **self.review_payload(system))
        self.assertTrue(self.state.dispatch("finish", {"summary": "Обзор обновлён"})["ok"])



    def test_review_references_prevent_silent_removal_and_operations_are_paged(self):
        system, first, last = self.complete_small_map()
        self.review_current_operations()
        operation = self.state.model["execution_reviews"][0]["operations"][0]
        for edge in list(self.state.model["edges"]):
            self.good("remove_element", kind="transition", id=edge["id"])
        response = self.state.dispatch("remove_element", {"kind": "step", "id": operation["node_id"]})
        self.assertFalse(response["ok"])
        page = self.good("inspect_map", section="operations", limit=1)
        self.assertEqual(page["items"][0]["id"], operation["id"])
        self.assertIn("effect", page["items"][0])
        self.assertNotIn("quote", page["items"][0]["evidence"][0])
        self.good("remove_element", kind="execution_review", id=self.state.model["execution_reviews"][0]["id"])
        self.good("remove_element", kind="step", id=operation["node_id"])




class OperationSchemaV2Tests(unittest.TestCase):
    def setUp(self):
        self.state=MapState(workspace(1),'Исследование обработки обращения')
        self.state.observe_read('source-1/channel.py',1,2)
        self.ref={'path':'source-1/channel.py','line_start':1,'line_end':2}

    def node(self, **extra):
        result=self.state.dispatch('upsert_step',explain_args({'name':'Получение сведений об обращении','description':'Запрашивает необходимые сведения.',
            'evidence_status':'observed','evidence':[self.ref], **extra}))
        self.assertTrue(result['ok'],result)
        return result['id']

    def test_new_schema_has_only_operations_and_no_system_catalog(self):
        self.node()
        model=self.state.snapshot()
        self.assertEqual(model['schema_version'],'2.0');self.assertNotIn('systems',model)
        self.assertEqual(model['nodes'][0]['kind'],'operation');self.assertNotIn('system_id',model['nodes'][0])
        names={x['function']['name'] for x in TOOLS};self.assertNotIn('upsert_system',names)
        self.assertFalse(self.state.dispatch('upsert_system',{'name':'Система'})['ok'])

    def test_old_node_kinds_and_system_membership_rejected(self):
        for kind in ['start','end','decision','fork','join','wait','external','action']:
            result=self.state.dispatch('upsert_step',{'name':'Получение сведений','kind':kind,'evidence_status':'observed','evidence':[self.ref]})
            self.assertFalse(result['ok'],result)
        self.assertFalse(self.state.dispatch('upsert_step',{'name':'Получение сведений','system_id':'sys-1'})['ok'])
        self.assertEqual(self.state.model['nodes'],[])

    def test_data_only_relation_is_not_a_v2_execution_transition(self):
        a=self.node();b=self.node()
        result=self.state.dispatch('upsert_transition',{'source':a,'target':b,'kind':'data','evidence_status':'observed','evidence':[self.ref]})
        self.assertFalse(result['ok']);self.assertEqual(self.state.model['edges'],[])

    def test_boundary_ids_are_explicit_and_validated_at_finish(self):
        n=self.node()
        self.state.dispatch('set_scope',{'title':'Обработка обращения','start':'Получены сведения','end':'Обработка закончена',
            'entry_operation_ids':[n],'terminal_operation_ids':['missing']})
        result=self.state.dispatch('finish',{'summary':'Исследовано'})
        self.assertFalse(result['ok']);self.assertIn('неизвестные операции',result['error'])
        self.assertEqual(self.state.graph_frontier()['starts'],[n])

    def test_stages_are_optional(self):
        self.node()
        self.assertFalse(any('group_id' in e for e in self.state._finish_errors()))

    def external(self, **extra):
        return {'name':'Сервис сведений об обращениях','purpose':'Запрашивает сведения об обращении',
                'direction':'request','endpoint':'POST /cases','contract':'Запрос сведений по номеру обращения','evidence':[self.ref],**extra}

    def test_external_request_needs_only_proven_caller_not_fabricated_receiver(self):
        n=self.node(external_interactions=[self.external()])
        node=self.state.model['nodes'][0]
        self.assertEqual(node['external_interactions'][0]['direction'],'request')
        self.assertNotIn('systems',self.state.model)
        self.assertIn(n,self.state._reviewable_operations())
        self.assertEqual(len(self.state.model['nodes']),1)

    def test_external_call_requires_evidence_and_endpoint_or_contract(self):
        for external in [self.external(evidence=[]),self.external(endpoint='',contract='')]:
            result=self.state.dispatch('upsert_step',{'name':'Получение сведений','evidence_status':'observed','evidence':[self.ref],
                'external_interactions':[external]})
            self.assertFalse(result['ok']);self.assertEqual(self.state.model['nodes'],[])

    def test_external_call_cannot_cite_an_unread_remote_implementation(self):
        result=self.state.dispatch('upsert_step',{'name':'Получение сведений','evidence_status':'observed','evidence':[self.ref],
            'external_interactions':[self.external(evidence=[{'path':'source-1/channel.py','line_start':3,'line_end':3}])]})
        self.assertFalse(result['ok']);self.assertIn('read_file',result['error'])

    def test_public_explanations_do_not_invalidate_execution_fingerprints(self):
        self.node();before=deepcopy(self.state.model['nodes'][0]);after=deepcopy(before)
        after['code_explanations'][0]['explanation']='Публичное пояснение стало понятнее.'
        after['basis']='Уточнено объяснение прочитанного факта.'
        after['name']='Получение данных обращения'
        self.assertEqual(self.state._fingerprint(before),self.state._fingerprint(after))
        after['output']='Отправлена команда вместо полученных данных'
        self.assertNotEqual(self.state._fingerprint(before),self.state._fingerprint(after))

    def test_deferred_result_is_distinct_from_cancellation(self):
        self.node(outcome='deferred',effect_kind='decision')
        self.assertEqual(self.state.model['nodes'][0]['outcome'],'deferred')
        self.assertEqual(self.state.model['nodes'][0]['effect_kind'],'decision')

    def test_gap_must_address_specific_work(self):
        result=self.state.dispatch('add_gap',{'description':'Чего-то не хватает','category':'unresolved_behavior'})
        self.assertFalse(result['ok']);self.assertIn('адресовать',result['error'])
        self.assertFalse(self.state.dispatch('add_gap',{'description':'Нет внешней системы','system_ids':['sys-1']})['ok'])

    def test_parallel_operation_still_requires_meaningful_conditions_on_each_link(self):
        a=self.node(continuation_mode='all');b=self.node();c=self.node()
        for target in [b,c]:
            result=self.state.dispatch('upsert_transition',explain_args({'source':a,'target':target,'kind':'sequence',
                'evidence_status':'observed','evidence':[self.ref]}));self.assertTrue(result['ok'])
        self.assertTrue(any('условий на всех выходах' in e for e in self.state.quality_errors()))
        for edge in list(self.state.model['edges']):
            result=self.state.dispatch('upsert_transition',{**edge,'condition':'После получения обращения выполняется параллельно с другим запросом'})
            self.assertTrue(result['ok'],result)
        self.assertFalse(any('условий на всех выходах' in e for e in self.state.quality_errors()))

if __name__ == "__main__":
    unittest.main()
