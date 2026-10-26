"""Educational deterministic tool script, not an assessment of LLM quality.

Quotes are found by source snippets. Stores are in-memory dictionaries; the
fixture has no connection to real bank systems or customer data.
"""
import json
from pathlib import Path

from pko.llm.client import ChatResult
from pko.llm.registry import ModelSpec

SPEC = ModelSpec(role="scenario_researcher", base_url="https://stub.invalid/v1", model="fixture", api_key="not-needed")
FIXTURE = Path(__file__).parent / "fixtures" / "scenario_banking"
SCENARIO = "Выпуск новой дебетовой карты действующему клиенту через мобильный канал, до выпуска либо отказа. Перевыпуск исключён."
NODE_COUNT, EDGE_COUNT, OPERATION_COUNT = 22, 21, 18


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
                           "start": "Клиент отправил заявку", "end": "Карта выпущена либо сформирован окончательный отказ",
                           "exclusions": ["Перевыпуск", "Самостоятельное оформление новым клиентом", "Доставка карты"]})]
    for number, filename in ((1, "app.py"), (2, "app.py"), (3, "worker.py")):
        calls.append(("read_file", {"path": f"source-{number}/{filename}"}))
    for number, name in ((1, "Мобильный канал"), (2, "Обработка заявок"), (3, "Выпуск карт")):
        calls.append(("upsert_system", {"name": name, "source_ids": [f"source-{number}"],
                                       "description": name + " — учебная реализация", "evidence_status": "observed", "evidence": scopes[number]}))
    # Business groups are separate from executable nodes; two operations cross AS.
    for kind, name, parent, result, systems in [
        ("stage", "Подача заявки", None, "Обращение принято для рассмотрения либо завершено отказом по варианту", [1, 2]),
        ("operation", "Определить вариант обращения", "group-1", "Выбран поддерживаемый вариант либо сформирован отказ", [1]),
        ("operation", "Передать заявку на рассмотрение", "group-1", "Подготовленный запрос принят АС заявок", [1, 2]),
        ("stage", "Рассмотрение заявки", None, "Заявка направлена на выпуск либо отклонена", [2, 3]),
        ("operation", "Проверить возможность выпуска", "group-4", "Проверен допуск клиента; при отрицательном результате сохранён отказ", [2]),
        ("operation", "Передать заявку на выпуск", "group-4", "Принятая заявка доставлена обработчику выпуска", [2, 3]),
        ("stage", "Выпуск карты", None, "Карта и результат выпуска сохранены", [3]),
        ("operation", "Подготовить карту", "group-7", "Из сообщения получены параметры и подготовлены данные карты", [3]),
        ("operation", "Зафиксировать выпуск", "group-7", "Карта сохранена, результат связан с заявкой", [3]),
    ]:
        calls.append(("upsert_group", {"name": name, "kind": kind, "result": result,
                                      "evidence": [ref for system in systems for ref in scopes[system]],
                                      **({"parent_id": parent} if parent else {})}))
    # key, AS, business name, graph kind, operation kind, start/end snippets, effect
    specs = [
        ("start", 1, "Заявка отправлена клиентом", "start", None,
         "@app.post('/cards/debit/new')", "def submit(request):", "Начало выбранного варианта: запрос направлен в обработчик новой карты."),
        ("channel_receive", 1, "Приём заявки в мобильном канале", "action", "action",
         "@app.post('/cards/debit/new')", "def submit(request):", "Параметры запроса доступны обработчику мобильного канала."),
        ("variant_check", 1, "Проверка варианта оформления", "decision", "validation",
         "if request['client_type'] != 'existing':", None, "Выбран допуск действующего клиента либо отказ по неподдерживаемому варианту."),
        ("application_build", 1, "Формирование запроса на оформление", "action", "transform",
         "application = {", "    }", "Подготовлены request_id, client_id и продукт debit для запроса в АС заявок."),
        ("application_send", 1, "Передача запроса в АС заявок", "action", "integration",
         "return http.post('https://applications.example/applications/debit'", None, "Подготовленный запрос передан по HTTP; это ещё не подтверждение выпуска."),
        ("variant_rejection", 1, "Формирование отказа по варианту", "action", "error_handling",
         "rejection = {'request_id':", "return rejection", "Возвращён идентификатор заявки со статусом unsupported_variant."),
        ("variant_end", 1, "Отказ по варианту сформирован", "end", None,
         "return rejection", None, "Выбранный обработчик завершился окончательным отказом по варианту."),
        ("application_receive", 2, "Приём запроса в АС заявок", "action", "action",
         "@app.post('/applications/debit')", "def accept(request):", "АС заявок приняла request_id, client_id и product по HTTP-контракту."),
        ("profile_read", 2, "Получение профиля клиента", "action", "read",
         "profile = client_profiles[request['client_id']]", None, "Из учебного локального справочника получены признаки активности и допуска клиента."),
        ("eligibility_check", 2, "Проверка возможности выпуска", "decision", "validation",
         "if not (profile['active'] and profile['debit_allowed']):", None, "По активности клиента и разрешению выпуска выбрана положительная либо отказная ветка."),
        ("rejection_write", 2, "Сохранение отказа по заявке", "action", "write",
         "applications[request['request_id']] = {'status': 'rejected'", None, "В учебном хранилище по заявке сохранены статус rejected и клиент."),
        ("rejection_response", 2, "Формирование ответа об отказе", "action", "error_handling",
         "rejection = {'request_id':", "return rejection", "Сформирован и возвращён ответ с request_id и статусом rejected."),
        ("rejection_end", 2, "Заявка завершена отказом", "end", None,
         "return rejection", None, "По заявке сформирован окончательный отказ."),
        ("acceptance_write", 2, "Сохранение принятой заявки", "action", "write",
         "applications[request['request_id']] = {'status': 'accepted'", None, "В учебном хранилище сохранены request_id, клиент и статус accepted."),
        ("message_build", 2, "Формирование сообщения на выпуск", "action", "transform",
         "message = {", "    }", "Подготовлено сообщение с request_id, client_id и product."),
        ("message_publish", 2, "Публикация заявки на выпуск", "action", "publish",
         "broker.publish('card.issue.requested', message)", None, "Сообщение отправлено в card.issue.requested; результат выпуска ещё не подтверждён."),
        ("message_receive", 3, "Приём сообщения в АС выпуска", "action", "action",
         "@subscriber('card.issue.requested')", "def issue(message):", "Обработчик выпуска получил сообщение канала card.issue.requested."),
        ("issue_parameters", 3, "Извлечение параметров выпуска", "action", "transform",
         "parameters = {'request_id':", None, "Из сообщения выделены request_id, client_id и product для выпуска."),
        ("card_build", 3, "Формирование данных новой карты", "action", "transform",
         "card = {", "    }", "Подготовлена учебная запись карты: идентификатор, клиент, продукт и статус issued."),
        ("card_issue", 3, "Сохранение выпущенной карты", "action", "write",
         "cards[card['card_id']] = card", None, "Учебная карта со статусом issued сохранена по card_id в хранилище карт."),
        ("result_write", 3, "Сохранение результата выпуска", "action", "write",
         "results[parameters['request_id']] =", None, "По request_id сохранены статус issued и card_id, связывающие результат с заявкой."),
        ("issued_end", 3, "Карта выпущена", "end", None,
         "results[parameters['request_id']] =", None, "Учебная карта сохранена, результат выпуска связан с исходной заявкой."),
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
    memberships = {
        **dict.fromkeys(["start", "channel_receive", "variant_check", "variant_rejection", "variant_end"], "group-2"),
        **dict.fromkeys(["application_build", "application_send", "application_receive"], "group-3"),
        **dict.fromkeys(["profile_read", "eligibility_check", "rejection_write", "rejection_response", "rejection_end"], "group-5"),
        **dict.fromkeys(["acceptance_write", "message_build", "message_publish", "message_receive"], "group-6"),
        **dict.fromkeys(["issue_parameters", "card_build"], "group-8"),
        **dict.fromkeys(["card_issue", "result_write", "issued_end"], "group-9"),
    }
    for index, (key, system, name, kind, operation_kind, start, end, effect) in enumerate(specs, 1):
        node = {"id": f"step-{index}", "system": system, "name": name, "kind": kind,
                "operation_kind": operation_kind, "evidence": quote(system, start, end), "effect": effect,
                "failure": failures.get(key, "В обследованном участке отдельная обработка технического отказа не показана.")}
        nodes[key] = node
        calls.append(("upsert_step", {"name": name, "kind": kind, "system_id": f"sys-{system}", "description": effect,
                                      "group_id": memberships[key],
                                      **({"outcome": "success" if key == "issued_end" else "refusal"} if kind == "end" else {}),
                                      "input": "request_id, client_id, client_type" if system == 1 else "request_id, client_id, product",
                                      "output": effect, "evidence_status": "observed", "evidence": node["evidence"]}))
    edge_specs = [
        ("entry", "start", "channel_receive", "sequence", ""),
        ("variant", "channel_receive", "variant_check", "sequence", ""),
        ("variant_ok", "variant_check", "application_build", "branch", "Клиент банка"),
        ("variant_no", "variant_check", "variant_rejection", "branch", "Другой вариант клиента"),
        ("variant_finish", "variant_rejection", "variant_end", "sequence", ""),
        ("send", "application_build", "application_send", "sequence", ""),
        ("http", "application_send", "application_receive", "call", ""),
        ("profile", "application_receive", "profile_read", "sequence", ""),
        ("eligibility", "profile_read", "eligibility_check", "sequence", ""),
        ("eligible", "eligibility_check", "acceptance_write", "branch", "Клиент активен, выпуск разрешён"),
        ("ineligible", "eligibility_check", "rejection_write", "branch", "Клиент неактивен или выпуск запрещён"),
        ("reject_answer", "rejection_write", "rejection_response", "sequence", ""),
        ("reject_finish", "rejection_response", "rejection_end", "sequence", ""),
        ("build_message", "acceptance_write", "message_build", "sequence", ""),
        ("publish", "message_build", "message_publish", "sequence", ""),
        ("event", "message_publish", "message_receive", "event", ""),
        ("parameters", "message_receive", "issue_parameters", "sequence", ""),
        ("card_data", "issue_parameters", "card_build", "sequence", ""),
        ("card_store", "card_build", "card_issue", "sequence", ""),
        ("result", "card_issue", "result_write", "sequence", ""),
        ("success", "result_write", "issued_end", "sequence", ""),
    ]
    edge_ids = {}
    for index, (key, source_key, target_key, kind, condition) in enumerate(edge_specs, 1):
        source, target = nodes[source_key], nodes[target_key]
        args = {"source": source["id"], "target": target["id"], "kind": kind, "condition": condition,
                "description": f"{source['name']} → {target['name']}", "evidence_status": "observed",
                "evidence": scopes[source["system"]]}
        if source["system"] != target["system"]:
            args.update(sender_evidence=source["evidence"], receiver_evidence=target["evidence"],
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
        calls.append(("review_execution", {"name": f"Атомарные операции учебной АС {number}", "system_id": f"sys-{number}",
                                           "evidence": scopes[number], "operations": operations, "checks": checks}))
        calls.append(("review_source", {"source_id": f"source-{number}", "disposition": "reviewed",
                                        "reason": "Прочитан выбранный обработчик и его операции. Соседний перевыпуск и неподключённый helper не включены."}))
    calls.append(("finish", {"summary": "Учебный детерминированный пример: 18 отдельных операций, 22 узла, 21 переход и три АС. "
                                       "Показаны проверки, формирование данных, HTTP-вызов, записи, публикация, приём сообщения и выпуск, включая отказы. "
                                       "Учебные хранилища находятся в памяти. Это проверка сборки по заранее заданным ответам, не оценка качества реальной модели."}))
    if len(nodes) != NODE_COUNT or len(edge_ids) != EDGE_COUNT:
        raise AssertionError("Update fixture counts when changing the educational scenario")
    return calls


class ScriptedClient:
    spec = SPEC

    def __init__(self, calls=None):
        self.calls = iter(research_calls() if calls is None else calls)
        self.seen = []

    def chat(self, messages, **kwargs):
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
