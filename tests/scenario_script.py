"""Educational deterministic tool script, not an assessment of LLM quality.

Quotes are found by source snippets. Stores are in-memory dictionaries; the
fixture has no connection to real bank systems or customer data.
"""
import json
from pathlib import Path

from pko.llm.client import ChatResult
from pko.llm.registry import ModelSpec
from pko.scenario_map.code_references import symbols
from pko.scenario_map.quality import SEMANTIC_CHECKS

# A small window on purpose: scripted runs must exercise context compaction.
SPEC = ModelSpec(role="scenario_researcher", base_url="https://stub.invalid/v1", model="fixture", api_key="not-needed",
                 context_window=64000)
FIXTURE = Path(__file__).parent / "fixtures" / "scenario_banking"
SCENARIO = "Выпуск новой дебетовой карты действующему клиенту через мобильный канал, до выпуска либо отказа. Перевыпуск исключён."
NODE_COUNT, EDGE_COUNT, OPERATION_COUNT = 18, 17, 18


def sources():
    return [FIXTURE / name for name in ("channel", "applications", "issuance")]


def evidence(source, filename, start_snippet, end_snippet=None):
    lines = (sources()[source - 1] / filename).read_text(encoding="utf-8").splitlines()
    matches = [index for index, line in enumerate(lines) if start_snippet in line]
    if len(matches) != 1:
        raise AssertionError(f"Expected unique {start_snippet!r} in {filename}; found {len(matches)}")
    start = end = matches[0]
    if end_snippet is not None:
        matches = [index for index in range(start, len(lines)) if end_snippet in lines[index]]
        if not matches:
            raise AssertionError(f"Missing {end_snippet!r} after {start_snippet!r}")
        end = matches[0]
    return [{"path": f"source-{source}/{filename}", "line_start": start + 1, "line_end": end + 1,
             "quote": "\n".join(lines[start:end + 1])}]


def research_calls():
    def quote(source, start, end=None):
        return evidence(source, "worker.py" if source == 3 else "app.py", start, end)

    scopes = {
        1: quote(1, "@app.post('/cards/debit/new')", "json=application)"),
        2: quote(2, "@app.post('/applications/debit')", "broker.publish('card.issue.requested', message)"),
        3: quote(3, "@subscriber('card.issue.requested')", "results[parameters['request_id']] ="),
    }
    calls = [("set_scope", {"title": "Новая дебетовая карта действующему клиенту", "process_name": "Оформление дебетовой карты",
                           "start": "Обработчик получил заявку", "end": "Результат выпуска сохранён либо сформирован отказ",
                           "entry_operation_ids": ["step-1"], "terminal_operation_ids": ["step-5", "step-10", "step-18"],
                           "exclusions": ["Перевыпуск", "Самостоятельное оформление новым клиентом", "Доставка карты"]})]
    for number, filename in ((1, "app.py"), (2, "app.py"), (3, "worker.py")):
        calls.append(("read_file", {"path": f"source-{number}/{filename}"}))
    calls.append(("save_research_brief", {
        "project_overview": "Учебные реализации мобильного канала, обработки заявок и выпуска карт связаны HTTP и сообщением на выпуск.",
        "scenario_alignment": "Вход /cards/debit/new относится к новой дебетовой карте действующему клиенту; перевыпуск исключён. Исходы — выпуск либо отказ.",
        "confirmed_facts": [{"finding": "Мобильный обработчик принимает запрос новой карты.", "evidence": scopes[1]}],
        "flow_hypothesis": ["Приём и проверка варианта в мобильном канале.",
                            "Проверка допуска и сохранение заявки либо отказа.",
                            "Передача сообщения и сохранение карты с результатом выпуска."],
        "open_questions": ["Какие условия ветвей и отдельные эффекты представлены в обработчиках?"],
        "next_actions": ["Проследить исполнение от входа, выделяя отдельные действия и подтверждённые переходы."],
        "evidence": [ref for scope in scopes.values() for ref in scope],
    }))
    # key, source, business name, graph kind, operation kind, start/end snippets, effect
    specs = [
        ("channel_receive", 1, "Приём заявки в мобильном канале", "operation", "action",
         "@app.post('/cards/debit/new')", "def submit(request):", "Параметры запроса доступны обработчику мобильного канала."),
        ("variant_check", 1, "Проверка варианта оформления", "operation", "validation",
         "if request['client_type'] != 'existing':", None, "Выбран допуск действующего клиента либо отказ по неподдерживаемому варианту."),
        ("application_build", 1, "Формирование запроса на оформление", "operation", "transform",
         "application = {", "    }", "Подготовлены идентификаторы заявки и клиента и вид карты «дебетовая» для запроса в АС заявок."),
        ("application_send", 1, "Отправка заявки на рассмотрение", "operation", "integration",
         "return http.post('https://applications.example/applications/debit'", None, "Подготовленный запрос передан по HTTP; это ещё не подтверждение выпуска."),
        ("variant_rejection", 1, "Формирование отказа по варианту", "operation", "error_handling",
         "rejection = {'request_id':", "return rejection", "Возвращён идентификатор заявки со статусом «вариант не поддерживается»."),
        ("application_receive", 2, "Получение заявки на рассмотрение", "operation", "action",
         "@app.post('/applications/debit')", "def accept(request):", "АС заявок приняла идентификаторы заявки и клиента и вид карты по HTTP-контракту."),
        ("profile_read", 2, "Получение профиля клиента", "operation", "read",
         "profile = client_profiles[request['client_id']]", None, "Из учебного локального справочника получены признаки активности и допуска клиента."),
        ("eligibility_check", 2, "Проверка возможности выпуска", "operation", "validation",
         "if not (profile['active'] and profile['debit_allowed']):", None, "По активности клиента и разрешению выпуска выбрана положительная либо отказная ветка."),
        ("rejection_write", 2, "Сохранение отказа по заявке", "operation", "write",
         "applications[request['request_id']] = {'status': 'rejected'", None, "В учебном хранилище по заявке сохранены статус rejected и клиент."),
        ("rejection_response", 2, "Формирование ответа об отказе", "operation", "error_handling",
         "rejection = {'request_id':", "return rejection", "Сформирован и возвращён ответ с идентификатором заявки и статусом «отклонена»."),
        ("acceptance_write", 2, "Сохранение принятой заявки", "operation", "write",
         "applications[request['request_id']] = {'status': 'accepted'", None, "В учебном хранилище сохранены идентификатор заявки, клиент и статус «принята»."),
        ("message_build", 2, "Формирование сообщения на выпуск", "operation", "transform",
         "message = {", "    }", "Подготовлено сообщение с идентификаторами заявки и клиента и видом карты."),
        ("message_publish", 2, "Публикация заявки на выпуск", "operation", "publish",
         "broker.publish('card.issue.requested', message)", None, "Сообщение отправлено в card.issue.requested; результат выпуска ещё не подтверждён."),
        ("message_receive", 3, "Получение сообщения о выпуске карты", "operation", "action",
         "@subscriber('card.issue.requested')", "def issue(message):", "Обработчик выпуска получил сообщение канала card.issue.requested."),
        ("issue_parameters", 3, "Извлечение параметров выпуска", "operation", "transform",
         "parameters = {'request_id':", None, "Из сообщения выделены идентификаторы заявки и клиента и вид карты для выпуска."),
        ("card_build", 3, "Формирование данных новой карты", "operation", "transform",
         "card = {", "    }", "Подготовлена учебная запись карты: идентификатор, клиент, продукт и статус issued."),
        ("card_issue", 3, "Сохранение выпущенной карты", "operation", "write",
         "cards[card['card_id']] = card", None, "Учебная карта со статусом «выпущена» сохранена по номеру карты в хранилище карт."),
        ("result_write", 3, "Сохранение результата выпуска", "operation", "write",
         "results[parameters['request_id']] =", None, "По идентификатору заявки сохранены статус «выпущена» и номер карты, связывающие результат с заявкой."),
    ]
    failures = {
        "variant_check": "Для клиента другого типа выбирается окончательный отказ по варианту.",
        "variant_rejection": "Отказ является результатом проверки типа клиента.",
        "profile_read": "Отсутствующий профиль вызывает исключение; отдельный локальный обработчик не показан.",
        "eligibility_check": "Если клиент не активен или выпуск запрещён, заявка отклоняется.",
        "rejection_write": "Запись выполняется по отрицательному результату проверки допуска.",
        "rejection_response": "Формируется окончательный отрицательный исход заявки.",
        "application_send": "Отдельная обработка недоступности HTTP в учебном обработчике не показана.",
        "message_publish": "Обработка сбоя брокера здесь не показана; отдельный retry-helper не подключён к этой цепочке.",
        "issue_parameters": "Отсутствие обязательного поля вызывает исключение; локальная обработка не показана.",
    }
    nodes = {}
    explanations = {
        "variant_check": "Обработчик проверяет тип клиента до подготовки запроса. При неподдерживаемом типе возвращает отказ, поэтому проверка показана отдельной развилкой.",
        "application_send": "Подготовленная заявка передаётся HTTP-вызовом в обработчик системы заявок. Получение ответа ещё не подтверждает выпуск карты.",
        "message_publish": "После сохранения принятой заявки обработчик отправляет сообщение на выпуск. Завершение этой операции означает отправку сообщения, а не изготовление карты.",
        "card_issue": "Обработчик сохраняет подготовленную запись карты в учебном хранилище. Поэтому действие отделено от формирования данных и записи результата заявки.",
    }
    for index, (key, system, name, kind, operation_kind, start, end, effect) in enumerate(specs, 1):
        node = {"id": f"step-{index}", "system": system, "name": name, "kind": kind,
                "operation_kind": operation_kind, "evidence": quote(system, start, end), "effect": effect,
                "failure": failures.get(key, "В обследованном участке отдельная обработка технического отказа не показана.")}
        nodes[key] = node
        calls.append(("upsert_step", {"name": name, "kind": kind, "description": effect,
                                      **({"basis": explanations[key]} if key in explanations else {}),
                                      **({"outcome": "success" if key == "result_write" else "refusal", "effect_kind": "result"}
                                         if key in ("variant_rejection", "rejection_response", "result_write") else {}),
                                      "continuation_mode": "choice" if operation_kind == "validation" else "sequence",
                                      "input": "Идентификатор заявки, сведения о клиенте и вид карты",
                                      "output": effect, "evidence_status": "observed", "evidence": node["evidence"]}))
    for name, args in calls:
        if name != 'upsert_step':
            continue
        if args['name'] == 'Отправка заявки на рассмотрение':
            args['external_interactions'] = [{'name': 'Сервис рассмотрения заявок',
                'purpose': 'Передача заявки на оформление карты', 'direction': 'request',
                'endpoint': 'https://applications.example/applications/debit',
                'contract': 'Идентификаторы заявки и клиента, вид карты', 'evidence': args['evidence']}]
        elif args['name'] == 'Публикация заявки на выпуск':
            args['external_interactions'] = [{'name': 'Очередь заявок на выпуск карт',
                'purpose': 'Передача принятой заявки обработчику выпуска', 'direction': 'send',
                'endpoint': 'card.issue.requested', 'contract': 'Идентификаторы заявки и клиента, вид карты',
                'evidence': args['evidence']}]
    edge_specs = [
        ("variant", "channel_receive", "variant_check", "sequence", ""),
        ("variant_ok", "variant_check", "application_build", "branch", "Клиент банка"),
        ("variant_no", "variant_check", "variant_rejection", "branch", "Другой вариант клиента"),
        ("send", "application_build", "application_send", "sequence", ""),
        ("http", "application_send", "application_receive", "call", ""),
        ("profile", "application_receive", "profile_read", "sequence", ""),
        ("eligibility", "profile_read", "eligibility_check", "sequence", ""),
        ("eligible", "eligibility_check", "acceptance_write", "branch", "Клиент активен, выпуск разрешён"),
        ("ineligible", "eligibility_check", "rejection_write", "branch", "Клиент неактивен или выпуск запрещён"),
        ("reject_answer", "rejection_write", "rejection_response", "sequence", ""),
        ("build_message", "acceptance_write", "message_build", "sequence", ""),
        ("publish", "message_build", "message_publish", "sequence", ""),
        ("event", "message_publish", "message_receive", "event", ""),
        ("parameters", "message_receive", "issue_parameters", "sequence", ""),
        ("card_data", "issue_parameters", "card_build", "sequence", ""),
        ("card_store", "card_build", "card_issue", "sequence", ""),
        ("result", "card_issue", "result_write", "sequence", ""),
    ]
    edge_ids = {}
    for index, (key, source_key, target_key, kind, condition) in enumerate(edge_specs, 1):
        source, target = nodes[source_key], nodes[target_key]
        args = {"source": source["id"], "target": target["id"], "kind": kind, "condition": condition,
                "description": f"{source['name']} → {target['name']}", "evidence_status": "observed",
                "evidence": scopes[source["system"]]}
        if source["system"] != target["system"]:
            args.update(sender_evidence=source["evidence"], receiver_evidence=target["evidence"],
                        basis="В отправителе и получателе совпадают адрес и поля запроса, а обработчик принимает переданную заявку."
                              if key == "http" else "Публикация и подписчик используют один канал; идентификатор заявки передаётся в сообщении и читается обработчиком выпуска.",
                        channel="HTTP POST https://applications.example/applications/debit" if key == "http" else "card.issue.requested",
                        contract="request_id, client_id, product принимаются accept(request) маршрута /applications/debit"
                                 if key == "http" else "request_id, client_id, product принимает подписчик issue(message) канала card.issue.requested",
                        data="request_id, client_id, product", correlation="request_id")
        edge_ids[key] = f"edge-{index}"
        calls.append(("upsert_transition", args))
    check_nodes = {
        1: {"branches": ["variant_check"], "failures": ["variant_rejection"], "integrations": ["application_send"],
            "state_changes": [], "computations": ["application_build"]},
        2: {"branches": ["eligibility_check"], "failures": ["rejection_write", "rejection_response"],
            "integrations": ["application_receive", "message_publish"], "state_changes": ["rejection_write", "acceptance_write"],
            "computations": ["message_build"]},
        3: {"branches": [], "failures": [], "integrations": ["message_receive"],
            "state_changes": ["card_issue", "result_write"], "computations": ["issue_parameters", "card_build"]},
    }
    check_edges = {
        1: {"branches": ["variant_ok", "variant_no"], "failures": ["variant_no"], "integrations": ["http"]},
        2: {"branches": ["eligible", "ineligible"], "failures": ["ineligible"], "integrations": ["http", "event"]},
        3: {"integrations": ["event"]},
    }
    for number in range(1, 4):
        operations = [{"node_id": node["id"], "name": node["name"], "kind": node["operation_kind"],
                       "action": node["name"], "effect": node["effect"], "failure": node["failure"], "evidence": node["evidence"]}
                      for node in nodes.values() if node["system"] == number and node["operation_kind"]]
        checks = {}
        for category, keys in check_nodes[number].items():
            checks[category] = {"status": "present" if keys else "not_found",
                                "explanation": "Поведение отражено отдельными операциями и переходами учебного участка." if keys else
                                               "В обследованном обработчике не найдено; это не утверждение об отсутствии во всей системе.",
                                "evidence": scopes[number], "node_ids": [nodes[key]["id"] for key in keys],
                                "edge_ids": [edge_ids[key] for key in check_edges[number].get(category, [])]}
        calls.append(("review_execution", {"name": f"Операции учебного обработчика {number}",
                                           "evidence": scopes[number], "operations": operations, "checks": checks}))
        calls.append(("review_source", {"source_id": f"source-{number}", "disposition": "reviewed",
                                        "reason": "Прочитан выбранный обработчик и его операции. Соседний перевыпуск и неподключённый helper не включены."}))
    initial_brief = next(args for name, args in calls if name == "save_research_brief")
    calls.append(("save_research_brief", {**initial_brief, "open_questions": [], "next_actions": []}))
    explanations = {
        'outcomes': 'Последние операции ссылаются на возвращённый отказ или запись карты, а не на отправку запроса.',
        'order': 'Последовательность чтения, проверки, записи и публикации сверена с телами трёх обработчиков.',
        'routing': 'Ветви проверки варианта и допуска имеют отдельные положительные и отрицательные условия.',
        'atomicity': 'Подготовка данных, HTTP-вызов, записи и публикация представлены отдельными действиями.',
        'dependencies': 'Три обработчика прочитаны; перевыпуск явно исключён, скрытых вложенных исполнителей в фикстуре нет.'}
    calls.append(('review_semantics', {'checks': {key: {'status': 'reviewed', 'explanation': explanations[key],
        'evidence': [r for scope in scopes.values() for r in scope],
        'node_ids': [n['id'] for n in nodes.values()], 'edge_ids': list(edge_ids.values()), 'gap_ids': []}
        for key in SEMANTIC_CHECKS}, 'route_cases': [
            {'name': 'Неподдерживаемый вариант клиента', 'input': 'Обращается новый клиент',
             'path': [nodes[k]['id'] for k in ['channel_receive', 'variant_check', 'variant_rejection']],
             'expected_result': 'Сформирован отказ по варианту', 'evidence': scopes[1]},
            {'name': 'Выпуск запрещён', 'input': 'Действующий клиент неактивен или выпуск ему запрещён',
             'path': [nodes[k]['id'] for k in ['channel_receive', 'variant_check', 'application_build', 'application_send',
                       'application_receive', 'profile_read', 'eligibility_check', 'rejection_write', 'rejection_response']],
             'expected_result': 'Отказ сохранён и возвращён', 'evidence': scopes[1] + scopes[2]},
            {'name': 'Допуск к выпуску', 'input': 'Действующий активный клиент с разрешением на выпуск',
             'path': [nodes[k]['id'] for k in ['channel_receive', 'variant_check', 'application_build', 'application_send',
                       'application_receive', 'profile_read', 'eligibility_check', 'acceptance_write', 'message_build',
                       'message_publish', 'message_receive', 'issue_parameters', 'card_build', 'card_issue', 'result_write']],
             'expected_result': 'Учебная карта и результат выпуска сохранены',
             'evidence': [r for scope in scopes.values() for r in scope]},
        ]}))
    calls.append(("finish", {"summary": "Учебный детерминированный пример: 18 атомарных операций и 17 переходов по трём источникам кода. "
                                       "Показаны проверки, формирование данных, HTTP-вызов, записи, публикация, приём сообщения и выпуск, включая отказы. "
                                       "Учебные хранилища находятся в памяти. Это проверка сборки по заранее заданным ответам, не оценка качества реальной модели."}))
    if len(nodes) != NODE_COUNT or len(edge_ids) != EDGE_COUNT:
        raise AssertionError("Update fixture counts when changing the educational scenario")
    for name, args in calls:
        if name in ('upsert_step', 'upsert_transition'):
            args.setdefault('basis', args['description'])
            number = int(args['evidence'][0]['path'].split('/')[0].split('-')[1])
            scope = scopes[number]
            args['code_explanations'] = [{'kind':'function', 'symbol':sorted(symbols(scope[0]['quote']))[0],
                'explanation': args['basis'], 'evidence': scope}]
    return calls


# Business stages of the fixture: name, goal, result and member operations by name.
STAGE_PLAN = [
    ("Проверка варианта оформления", "Установить, поддерживается ли запрошенный вариант оформления карты для клиента.",
     "Заявка допущена к передаче на рассмотрение либо завершена отказом по варианту.",
     ["Приём заявки в мобильном канале", "Проверка варианта оформления", "Формирование отказа по варианту"]),
    ("Передача заявки на рассмотрение", "Подготовить заявку и передать её в систему рассмотрения заявок.",
     "Заявка отправлена на рассмотрение; решение по ней ещё не принято.",
     ["Формирование запроса на оформление", "Отправка заявки на рассмотрение"]),
    ("Рассмотрение допуска клиента", "Решить по профилю клиента, можно ли выпустить ему дебетовую карту.",
     "Заявка принята и сохранена либо отклонена с ответом об отказе.",
     ["Получение заявки на рассмотрение", "Получение профиля клиента", "Проверка возможности выпуска",
      "Сохранение отказа по заявке", "Формирование ответа об отказе", "Сохранение принятой заявки"]),
    ("Передача заявки на выпуск", "Передать принятую заявку на выпуск карты.",
     "Сообщение на выпуск отправлено; выпуск карты ещё не подтверждён.",
     ["Формирование сообщения на выпуск", "Публикация заявки на выпуск"]),
    ("Выпуск карты", "Выпустить карту по принятой заявке и связать результат с заявкой.",
     "Карта и результат выпуска по заявке сохранены.",
     ["Получение сообщения о выпуске карты", "Извлечение параметров выпуска", "Формирование данных новой карты",
      "Сохранение выпущенной карты", "Сохранение результата выпуска"]),
]


def scripted_business_stages(payload):
    """Group operations of the grouping packet by ``STAGE_PLAN``; others stay unassigned."""
    by_name = {row["name"]: row["id"] for row in payload["operations"]}
    stages, used = [], set()
    for number, (name, goal, result, members) in enumerate(STAGE_PLAN, 1):
        ids = [by_name[member] for member in members if member in by_name]
        if ids:
            used.update(ids)
            stages.append({"id": f"stage-{number}", "name": name, "goal": goal, "result": result, "operation_ids": ids,
                           "boundary": "На выходе участка задача решена; следующие операции решают другую задачу."})
    unassigned = [{"operation_id": row["id"], "reason": "Операция не относится к этапам учебного сценария"}
                  for row in payload["operations"] if row["id"] not in used]
    return {"stages": stages, "unassigned": unassigned}


def scripted_summary():
    return ChatResult(text=json.dumps({
        "project_and_scenario": "Учебный выпуск карты действующему клиенту.",
        "completed_work": ["Исследуются обработчики выбранного сценария."],
        "key_findings": ["Точные факты и ID сохранены в состоянии карты."],
        "open_questions": [], "next_actions": ["Продолжить открытые проверки из памяти."],
        "references": [],
    }, ensure_ascii=False), usage={})


class ScriptedClient:
    spec = SPEC

    def __init__(self, calls=None):
        self.calls = iter(research_calls() if calls is None else calls)
        self.seen = []

    def chat(self, messages, **kwargs):
        if not kwargs.get("tools"):
            return scripted_summary()
        if kwargs["tools"][0]["function"]["name"] == "submit_business_stages":
            arguments = scripted_business_stages(json.loads(messages[1]["content"]))
            return ChatResult(text="", usage={}, tool_calls=[{
                "id": "call-stages", "type": "function",
                "function": {"name": "submit_business_stages", "arguments": json.dumps(arguments, ensure_ascii=False)}}])
        self.seen = messages
        for item in messages:
            if item["role"] == "tool":
                response = json.loads(item["content"])
                if not response.get("ok"):
                    raise AssertionError(response)
        name, arguments = next(self.calls)
        return ChatResult(text="", usage={}, tool_calls=[{
            "id": f"call-{len(messages)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}])
