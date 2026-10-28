"""Route regressions of a queue worker, independent of one language or project.

The synthetic Java worker in fixtures/scenario_java_worker repeats the structure
where a real run went wrong: a compound dispatch condition, an empty search
result, successive searches, a handler chosen by status, shared close commands
in different orders, the exception boundary and the queue result. The rubric
holds an expected and a forbidden variant of each; it does not prove semantics.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pko.llm.client import ChatResult
from pko.render.scenario_map import render_scenario_map
from pko.scenario_map.agent import run_agent
from scenario_script import SPEC
from pko.scenario_map.code_references import symbol_valid
from pko.scenario_map.evaluation import evaluate_map
from pko.scenario_map.quality import QualityError, ResearchQuality
from pko.scenario_map.schema import MapState
from pko.scenario_map.sources import build_workspace
from quality_helpers import explain_args
import test_scenario_schema

FIXTURES = Path(__file__).parent / "fixtures"
WORKER = FIXTURES / "scenario_java_worker"
RUBRIC = json.loads((FIXTURES / "scenario_java_worker_rubric.json").read_text(encoding="utf-8"))

NAMES = {
    "take_task": "Взятие задачи из очереди", "take_to_work": "Взятие задачи в работу",
    "error_check": "Проверка причины отклонения", "kind_check": "Проверка вида платежа",
    "manual_info": "Получение сведений о задаче", "manual_close": "Закрытие задачи перед передачей сотруднику",
    "manual_create": "Создание задачи сотруднику", "document_fetch": "Получение платёжного документа",
    "agency_check": "Проверка агентского платежа", "agency_contract_check": "Проверка договора агентского платежа",
    "details_check": "Проверка наличия реквизитов для поиска", "extract": "Извлечение реквизитов из назначения платежа",
    "enough_check": "Проверка достаточности реквизитов", "search_both": "Поиск договора по счёту и номеру договора",
    "search_contract_names": "Поиск договора по номеру договора и ФИО",
    "search_account_names": "Поиск договора по счёту и ФИО", "search_names": "Поиск договора по ФИО и известным реквизитам",
    "rows_check": "Проверка найденных договоров", "state_check": "Проверка состояния договора",
    "reject": "Отказ по платежу", "reject_close": "Закрытие задачи после отказа",
    "resubmit": "Отправка платежа на повторную обработку", "resubmit_close": "Закрытие задачи после повторной обработки",
    "failure_create": "Создание задачи сотруднику после сбоя", "failure_close": "Закрытие задачи после сбоя",
    "mark_completed": "Отметка задачи очереди обработанной", "mark_exception": "Отметка задачи очереди ошибочной",
}
EDGES = [
    ("take_task", "take_to_work", ""), ("take_to_work", "error_check", "Задача взята в работу"),
    ("take_to_work", "mark_exception", "Сбой взятия задачи в работу"),
    ("error_check", "kind_check", "Платёж не сопоставлен или мало реквизитов"),
    ("error_check", "manual_info", "Другая причина отклонения"),
    ("kind_check", "document_fetch", "Вид платежа поддерживается"),
    ("kind_check", "manual_info", "Вид платежа не поддерживается"),
    ("manual_info", "manual_close", ""), ("manual_close", "manual_create", ""), ("manual_create", "mark_completed", ""),
    ("document_fetch", "agency_check", ""), ("document_fetch", "failure_create", "Сбой при обработке платежа"),
    ("agency_check", "agency_contract_check", "Агентский платёж"), ("agency_check", "details_check", "Платёж не агентский"),
    ("agency_contract_check", "reject", "Договор в платеже не указан"),
    ("agency_contract_check", "resubmit", "Договор в платеже указан"),
    ("details_check", "reject", "Реквизитов для поиска нет"), ("details_check", "extract", "Есть хотя бы один реквизит"),
    ("extract", "enough_check", ""),
    ("enough_check", "reject", "Реквизитов меньше двух, платёж стандартный"),
    ("enough_check", "manual_close", "Реквизитов меньше двух, платёж не стандартный"),
    ("enough_check", "search_both", "Есть счёт и номер договора"),
    ("enough_check", "search_names", "Нет счёта или номера договора, есть ФИО"),
    ("search_both", "search_contract_names", "Договоры не найдены, есть ФИО"),
    ("search_both", "rows_check", "Договоры найдены, либо не найдены и ФИО нет"),
    ("search_contract_names", "search_account_names", "Договоры не найдены"),
    ("search_contract_names", "rows_check", "Договоры найдены"),
    ("search_account_names", "rows_check", ""), ("search_names", "rows_check", ""),
    ("rows_check", "reject", "Договоры не найдены, платёж стандартный"),
    ("rows_check", "manual_close", "Договоры не найдены, платёж не стандартный"),
    ("rows_check", "manual_close", "Найдено больше трёх договоров"),
    ("rows_check", "state_check", "Найдено от одного до трёх договоров"),
    ("state_check", "resubmit", "Договор действует"), ("state_check", "reject", "Договор закрыт"),
    ("state_check", "manual_close", "Состояние договора не определено"),
    ("reject", "reject_close", ""), ("reject_close", "mark_completed", ""),
    ("resubmit", "resubmit_close", ""), ("resubmit_close", "mark_completed", ""),
    ("failure_create", "failure_close", ""), ("failure_close", "mark_exception", ""),
]
COMMANDS = {"reject", "reject_close", "resubmit", "resubmit_close", "manual_close", "manual_create",
            "failure_create", "failure_close", "mark_completed", "mark_exception"}
# The business result belongs to the operation that forms it, not to the queue mark after it.
OUTCOMES = {"reject": "refusal", "resubmit": "success", "manual_create": "deferred"}


def route_map(names=NAMES, edges=EDGES):
    nodes = [{"id": key, "name": name, "effect_kind": "command" if key in COMMANDS else "unknown",
              "outcome": OUTCOMES.get(key, "unknown")} for key, name in names.items()]
    return {"nodes": nodes, "edges": [{"id": f"edge-{index}", "source": source, "target": target,
                                       "kind": "error" if "Сбой" in condition else "branch" if condition else "sequence",
                                       "condition": condition}
                                      for index, (source, target, condition) in enumerate(edges, 1)]}


def without(edges, *pairs):
    return [edge for edge in edges if (edge[0], edge[1]) not in pairs]


class RouteRubricTests(unittest.TestCase):
    """Each defect has its correct route and a close wrong one."""

    def findings(self, model):
        return evaluate_map(model, RUBRIC)["findings"]

    def assertDefect(self, model, code, **match):
        found = [row for row in self.findings(model) if row["code"] == code and all(
            row.get(key) == value or row.get("rule", {}).get(key) == value for key, value in match.items())]
        self.assertTrue(found, (code, match, self.findings(model)))

    def test_correct_routes_pass(self):
        result = evaluate_map(route_map(), RUBRIC)
        self.assertTrue(result["passed"], result["findings"])

    def test_choice_of_matching_needs_both_the_reason_and_the_payment_kind(self):
        names = {key: value for key, value in NAMES.items() if key != "kind_check"}
        edges = without(EDGES, ("error_check", "kind_check"), ("kind_check", "document_fetch"), ("kind_check", "manual_info"))
        model = route_map(names, edges + [("error_check", "document_fetch", "Платёж не сопоставлен или мало реквизитов")])
        self.assertDefect(model, "missing_operation", operation="kind_check")
        self.assertDefect(model, "forbidden_transition", source="error_check", target="document_fetch")

    def test_empty_result_branches_do_not_overlap(self):
        edges = [edge for edge in EDGES if edge[2] != "Договоры не найдены, платёж не стандартный"]
        model = route_map(edges=edges + [("rows_check", "manual_close", "Договоры не найдены")])
        self.assertDefect(model, "forbidden_transition", source="rows_check", target="manual_close")
        self.assertDefect(model, "missing_transition", source="rows_check", target="manual_close")

    def test_empty_search_without_names_reaches_the_empty_result_handling(self):
        edges = [edge for edge in EDGES if edge[2] != "Договоры найдены, либо не найдены и ФИО нет"]
        model = route_map(edges=edges + [("search_both", "rows_check", "Договоры найдены")])
        self.assertDefect(model, "missing_transition", source="search_both", target="rows_check")

    def test_successive_searches_stay_separate_operations(self):
        names = {key: value for key, value in NAMES.items() if key not in ("search_contract_names", "search_account_names")}
        names["search_names"] = "Поиск договора по ФИО"
        edges = [edge for edge in EDGES if "search_contract_names" not in edge[:2] and "search_account_names" not in edge[:2]]
        model = route_map(names, edges + [("search_both", "search_names", "Договоры не найдены, есть ФИО")])
        for alias in ("search_contract_names", "search_account_names"):
            self.assertDefect(model, "missing_operation", operation=alias)

    def test_actions_of_different_systems_keep_their_order(self):
        names = {key: value for key, value in NAMES.items() if key not in ("reject_close", "manual_close")}
        edges = [(source, "manual_create" if target == "manual_close" else target, condition)
                 for source, target, condition in EDGES if "reject_close" not in (source, target) and source != "manual_close"]
        model = route_map(names, edges + [("reject", "mark_completed", "")])
        self.assertDefect(model, "missing_operation", operation="reject_close")
        self.assertDefect(model, "missing_operation", operation="manual_close")
        swapped = route_map(edges=without(EDGES, ("failure_create", "failure_close"), ("failure_close", "mark_exception"))
                            + [("failure_close", "failure_create", ""), ("failure_create", "mark_exception", "")])
        self.assertDefect(swapped, "missing_path")

    def test_the_queue_result_is_recorded(self):
        names = {key: value for key, value in NAMES.items() if not key.startswith("mark_")}
        model = route_map(names, [edge for edge in EDGES if not edge[1].startswith("mark_")])
        for alias in ("mark_completed", "mark_exception"):
            self.assertDefect(model, "missing_operation", operation=alias)

    def test_missing_details_lead_to_the_refusal_not_to_the_agency_check(self):
        model = route_map(edges=without(EDGES, ("details_check", "reject"))
                          + [("details_check", "agency_contract_check", "Реквизитов для поиска нет")])
        self.assertDefect(model, "forbidden_transition", source="details_check", target="agency_contract_check")
        self.assertDefect(model, "missing_transition", source="details_check", target="reject")

    def test_too_few_details_lead_to_refusal_or_manual_task_by_payment_kind(self):
        edges = [edge for edge in EDGES if edge[0] != "enough_check" or "меньше двух" not in edge[2]]
        model = route_map(edges=edges + [("enough_check", "reject", "Реквизитов меньше двух")])
        self.assertDefect(model, "missing_transition", source="enough_check", target="reject")
        self.assertDefect(model, "missing_transition", source="enough_check", target="manual_close")

    def test_the_map_is_one_payment_not_the_worker_loop(self):
        names = {**NAMES, "fill": "Добавление задач в очередь", "none_left": "Сообщение об отсутствии элементов для обработки"}
        edges = EDGES + [("fill", "take_task", ""), ("take_task", "none_left", "Элементов нет"),
                         ("mark_completed", "take_task", "Элемент отмечен, цикл продолжается")]
        model = route_map(names, edges)
        self.assertDefect(model, "forbidden_operation", label="Наполнение очереди другим компонентом")
        self.assertDefect(model, "forbidden_operation", label="Отсутствие элементов очереди")
        self.assertDefect(model, "forbidden_transition", source="mark_completed", target="take_task")

    def test_results_keep_their_outcome_before_the_queue_mark(self):
        model = route_map()
        for node in model["nodes"]:
            node["outcome"] = "unknown"
        for alias in ("reject", "resubmit", "manual_create"):
            self.assertDefect(model, "property_mismatch", operation=alias)

    def test_failure_before_the_guarded_block_is_not_the_manual_task_path(self):
        model = route_map(edges=without(EDGES, ("take_to_work", "mark_exception"))
                          + [("take_to_work", "failure_create", "Сбой при обработке задачи")])
        self.assertDefect(model, "forbidden_transition", source="take_to_work", target="failure_create")
        self.assertDefect(model, "missing_transition", source="take_to_work", target="mark_exception")


class Tree:
    def __init__(self, content):
        self.content, self.files = content, list(content)

    def read(self, path):
        return self.content.get(path)


def worker_state():
    root = WORKER / "worker" / "src" / "main" / "java" / "demo" / "worker"
    content = {f"source-1/{path.relative_to(WORKER).as_posix()}": path.read_text(encoding="utf-8")
               for path in root.rglob("*.java")}
    space = SimpleNamespace(tree=Tree(content), notes=[], extraction=None,
                            sources=[{"id": "source-1", "name": "worker", "kind": "directory", "digest": "1", "file_count": len(content)}])
    state = MapState(space, "Обработка нераспознанного платежа по договору аренды")
    for path, text in content.items():
        state.observe_read(path, 1, len(text.splitlines()))
    return state


def lines(state, filename, start_snippet, end_snippet=None):
    path = next(path for path in state.workspace.tree.files if path.endswith("/" + filename))
    rows = state.workspace.tree.read(path).splitlines()
    start = next(index for index, row in enumerate(rows) if start_snippet in row)
    end = start if end_snippet is None else next(index for index in range(start, len(rows)) if end_snippet in rows[index])
    return [{"path": path, "line_start": start + 1, "line_end": end + 1}]


class SharedExecutorHintTests(unittest.TestCase):
    def setUp(self):
        self.state = worker_state()
        close = lines(self.state, "CloseTaskCommand.java", "public void execute()", "board.closeTask")
        result = self.state.dispatch("upsert_step", explain_args({
            "name": "Закрытие задачи перед передачей сотруднику", "description": "Задача закрывается с комментарием.",
            "evidence_status": "observed", "evidence": close, "effect_kind": "command",
            "external_interactions": [{"name": "Доска задач", "purpose": "Закрытие задачи с комментарием",
                                       "direction": "send", "contract": "closeTask", "evidence": close[-1:]}]}))
        self.assertTrue(result["ok"], result)
        self.close_id = result["id"]

    def step(self, name, evidence):
        result = self.state.dispatch("upsert_step", explain_args({
            "name": name, "description": "Платёж отклоняется с причиной.", "evidence_status": "observed",
            "evidence": evidence, "effect_kind": "command"}))
        self.assertTrue(result["ok"], result)
        return result

    def test_an_operation_that_also_closes_the_task_is_pointed_at_the_shared_command(self):
        merged = self.step("Отказ по платежу", lines(self.state, "TaskOperations.java", "public void rejectAndClose", "Payment rejected"))
        hints = " ".join(merged.get("hints", []))
        self.assertIn("CloseTaskCommand", hints)
        self.assertIn(self.close_id, hints)

    def test_the_executor_is_found_at_the_call_site_the_other_operation_cites(self):
        # In the P1679 run the closing operation cited the call site, not the command's own file.
        call = lines(self.state, "TaskOperations.java", "new CloseTaskCommand(clients.board(), item, \"Manual task created\")")
        result = self.state.dispatch("upsert_step", explain_args({
            "name": "Закрытие задачи перед созданием задачи сотруднику", "description": "Задача закрывается с комментарием.",
            "evidence_status": "observed", "evidence": call, "effect_kind": "command",
            "external_interactions": [{"name": "Доска задач", "purpose": "Закрытие задачи с комментарием",
                                       "direction": "send", "contract": "closeTask", "evidence": call}]}))
        self.assertTrue(result["ok"], result)
        merged = self.step("Отказ по платежу", lines(self.state, "TaskOperations.java", "public void rejectAndClose", "Payment rejected"))
        self.assertIn(result["id"], " ".join(merged.get("hints", [])))

    def test_a_hint_is_given_once_and_not_for_the_following_operation(self):
        evidence = lines(self.state, "TaskOperations.java", "public void rejectAndClose", "Payment rejected")
        merged = self.step("Отказ по платежу", evidence)
        self.assertIn(self.close_id, " ".join(merged.get("hints", [])))
        again = self.state.dispatch("upsert_step", explain_args({
            "id": merged["id"], "name": "Отказ по платежу", "description": "Платёж отклоняется с причиной.",
            "evidence_status": "observed", "evidence": evidence, "effect_kind": "command"}))
        self.assertNotIn(self.close_id, " ".join(again.get("hints", [])))
        other = self.step("Отказ по платежу без договора", evidence)
        self.state.dispatch("upsert_transition", explain_args({
            "source": other["id"], "target": self.close_id, "kind": "sequence", "description": "Затем задача закрывается",
            "evidence_status": "observed", "evidence": evidence}))
        self.state._executor_hints.clear()
        review = self.state._shared_executor_hints(next(n for n in self.state.model["nodes"] if n["id"] == other["id"]))
        self.assertFalse([hint for hint in review if self.close_id in hint])

    def test_a_separate_refusal_gets_no_shared_command_hint(self):
        separate = self.step("Отказ по платежу", lines(self.state, "TaskOperations.java", "new RejectCommand"))
        self.assertFalse([hint for hint in separate.get("hints", []) if "CloseTaskCommand" in hint])

    def test_the_hint_follows_function_names_in_other_languages(self):
        content = {"source-1/tickets.py": "def close_ticket(ticket_id):\n    return board.post('/close', ticket_id)\n\n\n"
                                          "def reject_and_close(ticket_id):\n    ledger.reject(ticket_id)\n    close_ticket(ticket_id)\n"}
        space = SimpleNamespace(tree=Tree(content), notes=[], extraction=None,
                                sources=[{"id": "source-1", "name": "py", "kind": "directory", "digest": "1", "file_count": 1}])
        state = MapState(space, "Отказ по заявке")
        state.observe_read("source-1/tickets.py", 1, 7)
        ref = {"path": "source-1/tickets.py", "line_start": 1, "line_end": 2}
        close = state.dispatch("upsert_step", explain_args({
            "name": "Закрытие заявки", "description": "Заявка закрывается.", "evidence_status": "observed",
            "evidence": [ref], "code_explanations": [{"kind": "function", "symbol": "close_ticket",
                                                      "explanation": "Закрывает заявку", "evidence": [ref]}],
            "external_interactions": [{"name": "Доска заявок", "purpose": "Закрытие заявки", "direction": "send",
                                       "endpoint": "/close", "evidence": [ref]}]}))
        self.assertTrue(close["ok"], close)
        merged = state.dispatch("upsert_step", explain_args({
            "name": "Отказ по заявке", "description": "Заявка отклоняется.", "evidence_status": "observed",
            "evidence": [{"path": "source-1/tickets.py", "line_start": 5, "line_end": 7}]}))
        self.assertIn("close_ticket", " ".join(merged.get("hints", [])))


class LanguageTests(unittest.TestCase):
    def test_declarations_of_common_languages_name_real_functions(self):
        for name, text in (("loadOrder", "override suspend fun loadOrder(id: String): Order {"),
                           ("firstOk", "fun <T> List<T>.firstOk(): T = first()"),
                           ("CloseTask", "func (s *Service) CloseTask(ctx context.Context) error {"),
                           ("submitForm", "export async function submitForm(data) {"),
                           ("parsePayment", "const parsePayment = async (text) => {"),
                           ("resolveRows", "  private resolveRows(rows: Row[]): Promise<Row[]> {"),
                           ("settle_payment", "pub(crate) async fn settle_payment(p: &Payment) -> Result<()> {"),
                           ("close_task", "def close_task"), ("createTask", "public function createTask($item) {"),
                           ("LoadOrder", "public async Task<Order> LoadOrder(string id) {")):
            with self.subTest(name=name):
                self.assertTrue(symbol_valid(name, "function", text))

    def test_calls_and_control_statements_are_not_declarations(self):
        for name, text in (("closeTask", "  closeTask(item);"), ("if", "  if (ready) {"),
                           ("build", "  return build(x) {"), ("catch", "  } catch (error) {")):
            with self.subTest(name=name):
                self.assertFalse(symbol_valid(name, "function", text))

    def test_every_language_is_offered_to_the_researcher_alike(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "app"
            (source / "web").mkdir(parents=True)
            (source / "web" / "checkout.ts").write_text("export function pay() { return api.post('/pay'); }\n", encoding="utf-8")
            (source / "jobs").mkdir()
            (source / "jobs" / "settle.go").write_text("package jobs\n\nfunc Settle() error { return nil }\n", encoding="utf-8")
            workspace = build_workspace([source], Path(folder) / "snapshot")
            notes = " ".join(workspace.notes)
            self.assertIn("независимо от языка", notes)
            self.assertNotIn("Вне периметра", notes)
            self.assertNotIn("фронтенд", notes)
            self.assertEqual(sorted(workspace.tree.files), ["source-1/jobs/settle.go", "source-1/web/checkout.ts"])


class PublicTextTests(unittest.TestCase):
    def test_code_jargon_is_rejected_and_business_names_remain(self):
        for value in ("Парсинг назначения платежа", "Проверка валидной комбинации реквизитов", "Резолв найденных записей"):
            with self.subTest(value=value), self.assertRaises(QualityError):
                ResearchQuality.business_text(value, "name")
        for value in ("Извлечение реквизитов из назначения платежа", "Запрос к GigaChat о реквизитах",
                      "Проверка достаточности реквизитов для поиска договора"):
            with self.subTest(value=value):
                self.assertEqual(ResearchQuality.business_text(value, "name"), value)

    def test_names_describing_code_get_a_hint(self):
        state = worker_state()
        result = state.dispatch("upsert_step", explain_args({
            "name": "Выбор стратегии обработки", "description": "Выбирается дальнейшая обработка.",
            "evidence_status": "observed", "evidence": lines(state, "Dispatcher.java", "Strategy strategy = isSupportedKind")}))
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("устройство кода" in hint for hint in result.get("hints", [])), result)


class InstanceHarnessTests(unittest.TestCase):
    def test_checks_hidden_in_one_operation_block_finish_until_split_or_addressed(self):
        state = worker_state()
        settle = lines(state, "ContractSearch.java", "public void settle", "default ->")
        result = state.dispatch("upsert_step", explain_args({
            "name": "Проверка найденных договоров", "description": "Выбирается продолжение по найденным договорам.",
            "evidence_status": "observed", "evidence": settle}))
        self.assertTrue(result["ok"], result)
        hidden = [error for error in state._finish_errors() if error.startswith(result["id"] + ": в основаниях")]
        self.assertTrue(hidden, state._finish_errors())
        gap = state.dispatch("add_gap", {"description": "Ветви выбора по найденным договорам не раскрыты",
                                         "node_ids": [result["id"]], "category": "unexplored"})
        self.assertTrue(gap["ok"], gap)
        self.assertFalse([error for error in state._finish_errors() if error.startswith(result["id"] + ": в основаниях")])

    def test_a_return_to_the_entry_is_pointed_out(self):
        tests = ScopeAndRegistryTests("test_a_map_without_branches_has_no_registry_note")
        tests.setUp()
        system, first, last = tests.complete_small_map()
        response = tests.state.dispatch("upsert_transition", explain_args({
            "source": last, "target": first, "kind": "sequence", "description": "Следующий элемент очереди",
            "evidence_status": "observed", "evidence": tests.cite()}))
        self.assertTrue(response["ok"], response)
        self.assertTrue(any("путь одного экземпляра" in hint for hint in response.get("hints", [])), response)

    def test_three_quarters_of_the_steps_turn_the_research_to_reviews(self):
        seen = []

        class Browsing:
            spec = SPEC

            def chat(self, messages, **kwargs):
                seen.append([m.get("content", "") for m in messages if m["role"] == "user"])
                offset = len(seen)
                return ChatResult(text="", usage={}, tool_calls=[{"id": f"call-{offset}", "type": "function", "function": {
                    "name": "inspect_map", "arguments": json.dumps({"section": "nodes", "offset": offset})}}])

        run_agent(test_scenario_schema.workspace(1), "Обработка обращения", SPEC, client=Browsing(), max_steps=8)
        notices = [index for index, contents in enumerate(seen) if any("три четверти шагов" in str(c) for c in contents)]
        self.assertTrue(notices, "the budget notice must reach the model")
        self.assertEqual(notices[0], 6)


class ScopeAndRegistryTests(unittest.TestCase):
    # Map-building helpers only; inheriting or importing the class would rerun all of its tests.
    ScenarioMapTests = test_scenario_schema.ScenarioMapTests
    setUp, good, cite, system, node, edge = (ScenarioMapTests.setUp, ScenarioMapTests.good, ScenarioMapTests.cite,
                                             ScenarioMapTests.system, ScenarioMapTests.node, ScenarioMapTests.edge)
    complete_small_map, review_payload, review_current_operations = (
        ScenarioMapTests.complete_small_map, ScenarioMapTests.review_payload, ScenarioMapTests.review_current_operations)

    def test_a_researched_part_of_the_process_is_stated_and_keeps_the_result_partial(self):
        system, first, last = self.complete_small_map()
        self.good("set_scope", title="Оформление карты", coverage="Только выпуск после одобрения заявки",
                  start="Получена заявка", end="Карта выпущена", exclusions=["Перевыпуск"])
        self.assertEqual(self.good("finish", summary="Исследован участок выпуска")["status"], "partial")
        page = render_scenario_map(self.state.snapshot())
        self.assertIn("Только выпуск после одобрения заявки", page)
        self.assertIn("'Исследованный участок процесса'", page)

    def test_an_empty_task_registry_of_a_branching_map_is_stated(self):
        system, first, last = self.complete_small_map()
        decision = self.node(system, "decision")
        self.edge(first, decision)
        self.edge(decision, last, "branch", condition="Выпуск разрешён")
        other = self.node(system)
        self.edge(decision, other, "branch", condition="Выпуск отложен")
        self.edge(other, last)
        self.good("finish", summary="Ветви восстановлены")
        self.assertTrue(any("Реестр задач исследования пуст" in item for item in self.state.model["limitations"]))

    def test_a_map_without_branches_has_no_registry_note(self):
        self.complete_small_map()
        self.good("finish", summary="Цепочка восстановлена")
        self.assertFalse(any("Реестр задач" in item for item in self.state.model["limitations"]))

    def test_a_successful_command_is_not_labelled_as_a_confirmed_result(self):
        page = render_scenario_map({"schema_version": "2.0", "scenario": {}, "nodes": [], "edges": []})
        self.assertIn("command:'Успех · команда отправлена'", page)


if __name__ == "__main__":
    unittest.main()
