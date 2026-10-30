"""R08 operation inspector: grounded card, exact log/fact links, logging changes, routes and code links (R1, R3,
R4, R7) through the ordinary atomic run_analysis and the offline report.

Tests-first. Synthetic payment code and scripted code/business models reuse the helpers of test_direct_atomic;
nothing here reads private data or calls a model. The new presentation API is imported lazily, so every test fails
on its own missing behaviour: process_observability.operation_view.{operation_views, related_operations} and the
data-section markers inside the existing operation cards (#op-N). These checks cover linkage, honesty labels and
links; they say nothing about live-model quality, runtime execution or browser interaction (checked separately).

Code-led fixture (direct-atomic-validator/9): the page reply proposes actions, working groups, exits and
source-based transitions; one composition derives the stage and the boundary facts (host fact IDs, found by their
meaning); the instrumentation and metrics replies read those IDs from their own requests. A fact no supplied code
realizes exists only as a human-approved obligation, supplied explicitly.
"""

import copy
import html
from html.parser import HTMLParser
import re
import unittest

from process_observability import report as r
from process_observability.direct_atomic import load_obligations
from process_observability.report import build_html
from process_observability.report_sources import collect_report_sources
from process_observability.source_tools import snapshot_repository
from test_action_refinement import HEADER, REFUND, RELEASE, finding
from test_direct_atomic import AtomicCase, Business, account, atomic_handlers

WEBHOOK, TASKS, AUDIT_PATH = "shop/payment/webhooks.py", "shop/payment/tasks.py", "shop/payment/audit.py"
# One event key at two emit sites (lines 9 and 17) of two different operations: the required negative of R1.
SHARED_REFUND = REFUND.replace('"refund.duplicate"', '"refund.event"').replace('"refund.completed"', '"refund.event"')
AUDIT = HEADER + "def record_audit(payment):\n    payment.audited = True\n    payment.save()\n    return payment\n"
FILES = {WEBHOOK: SHARED_REFUND, TASKS: RELEASE, AUDIT_PATH: AUDIT}
FOUND = [finding("integration", "Уведомление о возврате: повтор, ошибка и успешный возврат.", WEBHOOK, 6, 18,
                 "handle_refund"),
         finding("entry_point", "Освобождение средств по транзакциям без заказа.", TASKS, 6, 14, "release_funds_task"),
         finding("entry_point", "Отметка аудита платежа.", AUDIT_PATH, 6, 9, "record_audit")]

NAMES = {"accept": "Принять уведомление о возврате", "refund": "Выполнить возврат средств",
         "release": "Освободить средства корзин без заказа"}
TEXT = {"accept": "Платёжная система сообщает о возврате; повторное уведомление по обработанному платежу отклоняется.",
        "refund": "Сервис запрашивает возврат суммы в платёжном шлюзе и фиксирует успех или ошибку.",
        "release": "Задача освобождает средства по транзакциям, у которых нет заказа."}
# Operation key -> (symbol of the realizing unit, model refs, initiation, actor).
OPERATIONS = {"accept": ("handle_refund", [(WEBHOOK, 8, 10)], "external_message", "Платёжная система"),
              "refund": ("handle_refund", [(WEBHOOK, 11, 17)], "api_call", None),
              "release": ("release_funds_task", [(TASKS, 6, 14)], "automatic", None)}
OCCURRENCE = "Одна запись на обработку уведомления или запуск задачи"
CONDITION = "Платёж по уведомлению ещё не возвращался"
# (path, line, operation key, lifecycle role) of every keyed emit site.
SITES = ((WEBHOOK, 7, "accept", "request"), (WEBHOOK, 9, "accept", "duplicate"), (WEBHOOK, 11, "refund", "start"),
         (WEBHOOK, 15, "refund", "error"), (WEBHOOK, 17, "refund", "result"), (TASKS, 9, "release", "no_op"),
         (TASKS, 12, "release", "start"))
# (from key, to key, condition, basis, refs) of accepted operation transitions; every basis is in the source. accept
# and refund are realized by one unit (handle_refund) and the cited line lies in it: structurally supported (/10).
EDGES = (("accept", "refund", CONDITION, "source_condition", [(WEBHOOK, 8, 8)]),)
# Plus a second proposed route out of the same action whose condition the cited code does not state. release is an
# independently invoked task in another file that handle_refund never calls: since direct-atomic-validator/10 this
# proposal stays an inactive, addressed proposal, never a map route (OperationRouteTests negative control).
OPEN_EDGES = EDGES + (("accept", "release", None, "source_condition", [(WEBHOOK, 8, 10)]),)
# The working group of every action (kept as their derived stage) and another one for a crossing route.
GROUP, ORDER_GROUP = "Оплата и возвраты", "Обработка заказа"
CONTRACTS = {GROUP: "Платёжная система сообщает о возвратах; средства без заказа освобождаются.",
             ORDER_GROUP: "Магазин обрабатывает заказ после оплаты."}
# Boundary facts the composition derives: former semantic key -> (operation key, meaning, lifecycle roles, metric
# IDs). A code-led fact observes exactly its action's records of its roles: the repeat fact the duplicate record, the
# refund result the result record, the release start the start record. The "new refund" and the release result have
# no record of their role (the no-op record never observes the release result).
FACTS = {"refund.repeat": ("accept", "Получено повторное уведомление о возврате", ["duplicate"], []),
         "refund.new": ("accept", "Уведомление принято как новый возврат", ["result"], []),
         "refund.done": ("refund", "Возврат выполнен", ["result"], ["refund-lead-time"]),
         "release.start": ("release", "Начато освобождение средств корзин без заказа", ["start"], []),
         "funds.released": ("release", "Средства корзин без заказа освобождены", ["result"], [])}
# The one human-approved obligation of the fixture: no supplied code realizes it, so it keeps its own required fact
# (meaning = its text, no operation, no stage).
ORDER_OBLIGATION = "Создавать заказ из оформленной корзины"
ORDER_QUESTION = "Где в анализируемом коде создаётся заказ?"
MEANINGS = {**{key: spec[1] for key, spec in FACTS.items()}, "order.created": ORDER_OBLIGATION}
SPLIT = "Одна функция принимает уведомление и выполняет возврат: два самостоятельных результата"
AUDIT_QUESTION = "Относится ли отметка аудита платежа к процессу возврата?"
COMPLETION = "Задача завершается без действий, если транзакций без заказа нет"


def ref(path, first, last=None):
    return {"path": path, "start_line": first, "end_line": first if last is None else last}


def details(**values):
    """One complete optional "details" object of an atomic reply operation: everything unknown unless given.

    Public reply shape (R2): input, expected_result, precondition, completion_condition: str|null;
    effect_kind: decision|recommendation|command|result|unknown; outcomes: [success|refusal|failure|cancelled|deferred];
    continuation: choice|all|unknown; join: all|any|unknown; external_interactions: [{system, direction
    request|send|receive, endpoint str|null, contract str|null, refs [REF]}]; code_explanations: [{symbol, explanation,
    refs [REF]}]; questions: [str]; refs: [REF] grounding every non-null text, a known effect, outcomes or mode.
    """
    base = {"input": None, "expected_result": None, "precondition": None, "completion_condition": None,
            "effect_kind": "unknown", "outcomes": [], "continuation": "unknown", "join": "unknown",
            "external_interactions": [], "code_explanations": [], "questions": [], "refs": []}
    unexpected = set(values) - set(base)
    if unexpected:
        raise KeyError(f"not a details key: {sorted(unexpected)}")
    return {**base, **values}


EXTERNAL = {"system": "Платёжный шлюз", "direction": "request", "endpoint": None,
            "contract": "Запрос возврата суммы платежа", "refs": [ref(WEBHOOK, 13)]}
EXPLANATION = {"symbol": "handle_refund", "explanation": "Функция принимает уведомление и запускает возврат в шлюзе",
               "refs": [ref(WEBHOOK, 6)]}
REFUND_DETAILS = details(
    input="Уведомление платёжной системы и сумма возврата", expected_result="Сумма возвращена через платёжный шлюз",
    precondition="Уведомление по этому платежу ещё не обрабатывалось", effect_kind="command",
    outcomes=["success", "failure"], external_interactions=[EXTERNAL], code_explanations=[EXPLANATION],
    questions=["Подтверждает ли шлюз возврат до ответа сервису?"], refs=[ref(WEBHOOK, 8, 13)])
RELEASE_DETAILS = details(completion_condition=COMPLETION, effect_kind="result", outcomes=["success"],
                          refs=[ref(TASKS, 7, 10)])
LEAD_TIME = {"id": "refund-lead-time", "guidance_ids": ["METRIC-DEFINITION"],
             "purpose": "Время от уведомления о возврате до выполненного возврата", "formula_text": None,
             "counting_unit": "возврат", "cohort": None, "window": None, "time_semantics": None,
             "open_case_policy": None, "late_data_policy": None}


def known_facts(brief):
    """Meaning -> host fact ID of the log_coverage facts a later-stage request supplies."""
    return {item["meaning"]: item["id"] for item in brief["log_coverage"]["facts"]}


def metrics_reply(brief, attempt=1):
    """The metrics reply: one lead-time metric over the refund result and the repeated notification, by meaning."""
    ids = known_facts(brief)
    return {"metrics": [{**LEAD_TIME, "dependencies": [ids[MEANINGS[key]] for key in ("refund.done", "refund.repeat")
                                                       if MEANINGS[key] in ids]}]}


def cards(release_ref=None):
    """The instrumentation handler: four logging changes, exact by fact, overlap only, fact-linked without location
    and one on the obligation's fact (no operation, no location). Fact IDs are read by meaning from the request; a
    change whose fact is absent (its action was not accepted) is not proposed."""
    specs = (("card-done-amount", "refund.done", "add_field", "Сумма выполненного возврата",
              {"amount": "Сумма возврата"}, ref(WEBHOOK, 17), ["refund-lead-time"]),
             ("card-new-refund", "refund.new", "add_event", "Выполнен новый возврат",
              {"payment_id": "Идентификатор платежа"}, ref(WEBHOOK, 17), []),
             ("card-release", "release.start", "add_field", "Число транзакций без заказа при начале освобождения",
              {"pending_count": "Число транзакций без заказа"}, release_ref, []),
             ("card-order", "order.created", "add_event", "Заказ создан", {"order_id": "Идентификатор заказа"},
              None, []))

    def reply(brief, attempt=1):
        ids = known_facts(brief)
        return {"cards": [{"id": identifier, "fact_id": ids[MEANINGS[key]], "guidance_ids": ["INST-MINIMUM"],
                           "action": action, "rationale": "Нужна запись, по которой виден результат",
                           "semantics": semantics, "fields": fields, "transaction_timing": "После фиксации изменения",
                           "dependencies": [], "metric_ids": list(metrics),
                           "validation_plan": "Прогнать синтетический случай и сверить запись", "ref": location}
                          for identifier, key, action, semantics, fields, location, metrics in specs
                          if MEANINGS[key] in ids]}
    return reply


def refund_compose(facts=None):
    """The map-compose reply: one stage per working group, the boundary facts of FACTS (or facts) for the actions
    the request lists, and not_found for every approved obligation. Actions are recognised by their description."""
    chosen = FACTS if facts is None else facts

    def reply(brief, attempt=1):
        keys = {text: key for key, text in TEXT.items()}
        numbers = {keys[op["description"]]: op["number"] for op in brief["operations"] if op["description"] in keys}
        names = {item["number"]: item["name"] for item in brief["working_groups"]}
        by_group = {}
        for op in brief["operations"]:
            by_group.setdefault(op["group"], []).append(op["number"])
        stages = [{"name": names.get(group, "Действия без группы"),
                   "contract": CONTRACTS.get(names.get(group), "Принятые действия без группы"), "operations": ops}
                  for group, ops in by_group.items()]
        stage_of = {number: index for index, stage in enumerate(stages, 1) for number in stage["operations"]}
        rows = [{"stage": stage_of[numbers[key]], "operations": [numbers[key]], "obligation": None, "meaning": meaning,
                 "rationale": "Граница принятого действия", "scope": "Каждое выполнение", "condition": None,
                 "metric_ids": list(metric_ids), "evidence_alternatives": [], "guidance_ids": ["FACT-QUESTION"],
                 "lifecycle_roles": list(roles)}
                for key, meaning, roles, metric_ids in chosen.values() if key in numbers]
        return {"process": {"purpose": "Возвраты и освобождение средств", "boundaries": None,
                            "case_definition": "Один платёж"},
                "stages": stages, "facts": rows,
                "obligations": [{"obligation": item["number"], "status": "not_found", "operations": [],
                                 "question": ORDER_QUESTION} for item in brief["approved_obligations"]],
                "hypotheses": [], "scenarios": [], "groups": [], "unknowns": []}
    return reply


def refund_reply(brief, attempt=1, *, with_details=None, sites=SITES, edges=EDGES, groups=None, names=None,
                 ambiguity=None, broken=()):
    """The code-led map-atomic reply for whichever synthetic units this page supplies.

    Every action joins the working group GROUP (groups: operation key -> another group name), proposed once by name
    and reused by number later; an action is continues when one of its own transitions leaves it, else terminal.
    Mappings carry no facts (composition derives them). with_details: operation key -> details object (or None)
    added to that operation; other operations omit the key. broken: unit symbols left without accounting, so their
    page is rejected on every attempt (missing_units).
    """
    known = {item["name"]: item["number"] for item in brief["working_groups"]}
    new_groups = []

    def group(key):
        name = (groups or {}).get(key, GROUP)
        if name not in known:
            new_groups.append({"name": name, "contract": CONTRACTS.get(name, f"Действия: {name}")})
            known[name] = len(brief["working_groups"]) + len(new_groups)
        return known[name]
    units = {item["symbol"]: item["number"] for item in brief["units"]}
    operations, position = [], {}
    for key, (symbol, spans, initiation, actor) in OPERATIONS.items():
        if symbol not in units:
            continue
        item = {"name": (names or {}).get(key, NAMES[key]), "description": TEXT[key], "actor": actor,
                "kind": "operation", "group": group(key), "initiation": initiation, "initiation_basis": None,
                "exit": "terminal", "exit_question": None, "refs": [ref(*span) for span in spans]}
        if with_details is not None and key in with_details:
            item["details"] = with_details[key]
        operations.append(item)
        position[key] = len(operations)
    accounting = []
    for symbol, number in units.items():
        if symbol in broken:
            continue
        owned = [position[key] for key, spec in OPERATIONS.items() if spec[0] == symbol and key in position]
        accounting.append(account(number, "realized", owned, reason=SPLIT if len(owned) > 1 else None) if owned
                          else account(number, "unresolved", question=AUDIT_QUESTION))
    mappings = []
    for path, line, key, role in sites:
        if key not in position:
            continue
        [site] = [item for item in brief["sites"] if item["path"] == path and item["start_line"] == line]
        mappings.append({"site": site["number"], "new_operation": position[key], "known_operation": None,
                         "lifecycle_role": role,
                         "role_basis": None if role == "unknown" else "Условие вокруг вызова в переданном коде",
                         "occurrence_rule": OCCURRENCE, "ambiguity": (ambiguity or {}).get((path, line)),
                         "refs": [] if role == "unknown" else [ref(path, site["start_line"], site["end_line"])]})
    transitions = [{"from_new": position[source], "from_known": None, "to_new": position[target], "to_known": None,
                    "condition": condition, "basis": basis, "contract": None,
                    "basis_note": "Синтетическое основание порядка", "refs": [ref(*span) for span in spans]}
                   for source, target, condition, basis, spans in edges if source in position and target in position]
    for item in transitions:  # an action with an accepted next step in the supplied code continues
        operations[item["from_new"] - 1]["exit"] = "continues"
    return {"operations": operations, "new_groups": new_groups, "unit_accounting": accounting,
            "event_mappings": mappings, "operation_transitions": transitions, "unknowns": []}


# Lookups in a bundle.


def operation_ids(bundle):
    """Operation key -> host ID, recognised by the synthetic description (a test may vary the names)."""
    keys = {text: key for key, text in TEXT.items()}
    return {keys[op["description"]]: op["id"] for op in bundle["process_map"]["operations"]}


def fact_id(bundle, key):
    """Host ID of the fixture fact with this former semantic key (FACTS or the obligation), found by its meaning."""
    [identifier] = [item["id"] for item in bundle["log_coverage"]["facts"] if item["meaning"] == MEANINGS[key]]
    return identifier


def stage_id(bundle, name=GROUP):
    """Host ID of the derived stage with this name."""
    [identifier] = [item["id"] for item in bundle["process_map"]["stages"] if item["name"] == name]
    return identifier


def number_of(bundle, operation_id):
    """Local card number of #op-N: the 1-based position in process_map.operations."""
    return next(index for index, op in enumerate(bundle["process_map"]["operations"], 1) if op["id"] == operation_id)


def fact_number(bundle, fact_id):
    return next(index for index, item in enumerate(bundle["log_coverage"]["facts"], 1) if item["id"] == fact_id)


def card_number(bundle, card_id):
    return next(index for index, item in enumerate(bundle["instrumentation_plan"]["cards"], 1)
                if item["id"] == card_id)


def views(bundle):
    from process_observability.operation_view import operation_views
    return operation_views(bundle)


def with_view_route(bundle, source, target, condition=None):
    """A PURE SYNTHETIC UI-VIEW INPUT, never a pipeline result: a copy of bundle with one more map route.

    The ordinary pipeline keeps a route between independently invoked commands inactive and addressed (see
    OperationRouteTests.test_an_unsupported_route_between_independent_commands_stays_inactive_and_addressed); this copy
    only exercises how the presentation shows a second route out of one action, here without a stated condition. It
    reuses the grounded route's basis and citations for display, is labelled synthetic_view_only and is never exported
    or validated as a run.
    """
    view = copy.deepcopy(bundle)
    ids = operation_ids(view)
    [grounded] = [edge for edge in view["atomic_map"]["transitions"] if edge["from"] == ids[source]]
    identifier = "aot-synthetic-view-route"
    view["process_map"]["operation_transitions"].append({
        "id": identifier, "from_operation": ids[source], "to_operation": ids[target], "condition": condition,
        "evidence_ids": list(grounded["evidence_ids"])})
    view["atomic_map"]["transitions"].append({**copy.deepcopy(grounded), "id": identifier, "from": ids[source],
                                              "to": ids[target], "condition": condition,
                                              "state": "synthetic_view_only", "support": None})
    return view


# Markup helpers: no browser, only the generated document.


def position(fragment, marker):
    index = fragment.find(marker)
    if index < 0:
        raise AssertionError(f"{marker} is not rendered")
    return index


def _element_at(page, begin):
    tag = re.match(r"<([a-zA-Z][a-zA-Z0-9]*)", page[begin:]).group(1)
    depth = 0
    for match in re.compile(rf"<(/?){tag}\b[^>]*>").finditer(page, begin):
        depth += -1 if match.group(1) else 1
        if depth == 0:
            return page[begin:match.end()]
    raise AssertionError(f"<{tag}> is not closed")


def element(page, marker):
    """The complete element whose start tag contains marker, closed by counting its own tag name."""
    found = position(page, marker)
    return _element_at(page, page.rindex("<", 0, found))


def card_of(page, number):
    return element(page, f' id="op-{number}"')


def section(fragment, name):
    return element(fragment, f'data-section="{name}"')


def enclosing(page, text, tag):
    """Every innermost <tag> element whose content contains text."""
    found, start = [], 0
    while (index := page.find(text, start)) >= 0:
        begin = index
        while (begin := page.rfind(f"<{tag}", 0, begin)) >= 0:
            whole = _element_at(page, begin)
            if begin + len(whole) > index:
                found.append(whole)
                break
        start = index + 1
    return found


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        self.skip += tag in ("script", "style")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())


def text_of(fragment):
    parser = _Text()
    parser.feed(fragment)
    parser.close()
    return " ".join(parser.parts)


class _Tags(HTMLParser):
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.found = []
        self.feed(page)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.found.append((tag, dict(attrs)))


def node_attribute(page, operation_id, name):
    [attrs] = [attrs for _, attrs in _Tags(page).found
               if attrs.get("data-node-id") == operation_id and "operation-node" in (attrs.get("class") or "")]
    return attrs.get(name)


def hrefs_in(fragment):
    return set(re.findall(r'href="#[a-z]+-\d+"', fragment))


def source_hrefs(bundle, path, first, last):
    """Existing report anchors of exactly this source range: report_sources excerpts, then the evidence registry."""
    found = set()
    for prefix, items in (("code", (bundle.get("report_sources") or {}).get("excerpts", [])),
                          ("ev", bundle.get("evidence", []))):
        for number, item in enumerate(items, 1):
            anchor = item.get("source_anchor") or {}
            if (anchor.get("path"), anchor.get("start_line"), anchor.get("end_line")) == (path, first, last):
                found.add(f'href="#{prefix}-{number}"')
    return found


class InspectorCase(AtomicCase):
    """Helpers only (no tests): the synthetic refund run with report sources attached as the CLI attaches them.

    The run carries one approved obligation (ORDER_OBLIGATION) that no supplied code realizes: the only honest way a
    code-led map holds a required fact without an operation.
    """

    def inspected(self, *, reply=None, compose=None, instrumentation=None, **settings):
        settings.setdefault("approved_obligations", load_obligations(ORDER_OBLIGATION + "\n",
                                                                     source="approved-obligations.txt"))
        self.business = Business(**atomic_handlers(
            map_atomic=reply or refund_reply, map_compose=compose or refund_compose(),
            instrumentation=instrumentation or cards(), metrics=metrics_reply))
        bundle, self.out = self.run_case(self.business, files=dict(FILES), found=copy.deepcopy(FOUND), **settings)
        snapshot = snapshot_repository(self.out.parent / "repo", max_files=None, max_chars=None)
        bundle["report_sources"] = collect_report_sources(snapshot, bundle)
        return bundle

    def assertComplete(self, bundle):
        """Fixture precondition: every stage ran and every unit was decided.

        It holds before R08 for replies without "details"; a reply with details also needs R2, so the message
        names the rejected page attempts (for example unexpected_keys at operations[1]) instead of a bare status.
        """
        rejected = [(attempt.get("code"), attempt.get("path")) for request in bundle["atomic_map"]["requests"]
                    for attempt in request["attempts"] if attempt.get("outcome") == "rejected"]
        self.assertEqual({name: state["status"] for name, state in bundle["manifest"]["stages"].items()},
                         dict.fromkeys(("map", "coverage", "instrumentation", "metrics"), "completed"),
                         f"rejected atomic page attempts: {rejected}")
        self.assertEqual(bundle["atomic_map"]["status"], "complete", f"rejected atomic page attempts: {rejected}")


class OperationInspectorTests(InspectorCase):
    def test_card_leads_with_meaning_stage_actor_and_initiation_without_inventing_a_click(self):
        bundle = self.inspected()
        self.assertComplete(bundle)
        ids, seen = operation_ids(bundle), views(bundle)
        self.assertEqual(list(seen), [op["id"] for op in bundle["process_map"]["operations"]])
        accept = seen[ids["accept"]]
        self.assertEqual({key: accept[key] for key in ("name", "description", "stage_id", "stage_name", "actor",
                                                       "initiation", "linkage")},
                         {"name": NAMES["accept"], "description": TEXT["accept"], "stage_id": stage_id(bundle),
                          "stage_name": GROUP, "actor": "Платёжная система",
                          "initiation": "external_message", "linkage": "exact"})
        self.assertEqual((seen[ids["refund"]]["initiation"], seen[ids["release"]]["initiation"]),
                         ("api_call", "automatic"))
        card = card_of(build_html(bundle), number_of(bundle, ids["refund"]))
        words = text_of(card)
        self.assertIn("API", words)  # an invoked backend command, never a derived button or click
        self.assertNotRegex(words.lower(), r"клик|нажат|кнопк")
        # Business meaning first; logs, routes, code and service identifiers follow.
        meaning = position(card, html.escape(TEXT["refund"]))
        order = [position(card, f'data-section="{name}"') for name in ("logs", "previous", "next", "code")]
        self.assertLess(meaning, order[0])
        self.assertEqual(order, sorted(order))
        if ids["refund"] in card:
            self.assertLess(meaning, card.index(ids["refund"]))

    def test_one_event_key_on_two_sites_links_each_operation_only_to_its_own_site_and_facts(self):
        bundle = self.inspected()
        self.assertComplete(bundle)
        self.assertEqual(SHARED_REFUND.count('"refund.event"'), 2)
        ids, seen = operation_ids(bundle), views(bundle)

        def logs(key):
            return {(item["event_key"], item["start_line"]): item for item in seen[ids[key]]["logs"]}
        accept, refund = logs("accept"), logs("refund")
        self.assertEqual(set(accept), {("refund.requested", 7), ("refund.event", 9)})
        self.assertEqual(set(refund), {("refund.started", 11), ("refund.failed", 15), ("refund.event", 17)})
        repeat = accept[("refund.event", 9)]
        self.assertEqual((repeat["path"], repeat["lifecycle_role"], repeat["fact_ids"], repeat["occurrence_rule"],
                          repeat["role_support"]),
                         (WEBHOOK, "duplicate", [fact_id(bundle, "refund.repeat")], OCCURRENCE,
                          "emit_within_operation_sources"))
        self.assertEqual(refund[("refund.event", 17)]["fact_ids"], [fact_id(bundle, "refund.done")])
        # Explicit fact ownership also includes required action boundaries without compatible telemetry.
        # Existing log rows above remain tied to their own emit sites and roles.
        for key, expected in (("accept", ["refund.repeat", "refund.new"]), ("refund", ["refund.done"]),
                              ("release", ["release.start", "funds.released"])):
            facts = seen[ids[key]]["facts"]
            self.assertEqual([item["id"] for item in facts], [fact_id(bundle, name) for name in expected], key)
            self.assertEqual({item["linkage"] for item in facts}, {"exact"})
        page = build_html(bundle)
        repeat_link, done_link = (f'href="#fact-{fact_number(bundle, fact_id(bundle, key))}"'
                                  for key in ("refund.repeat", "refund.done"))
        cards_html = {key: card_of(page, number_of(bundle, ids[key])) for key in ("accept", "refund")}
        self.assertIn(repeat_link, cards_html["accept"])
        self.assertNotIn(done_link, cards_html["accept"])
        self.assertIn(done_link, cards_html["refund"])
        self.assertNotIn(repeat_link, cards_html["refund"])
        # Each existing record leads to its own emit site with its role and occurrence rule.
        own = section(cards_html["accept"], "logs")
        self.assertTrue(source_hrefs(bundle, WEBHOOK, 9, 9) & hrefs_in(own))
        self.assertFalse(source_hrefs(bundle, WEBHOOK, 17, 17) & hrefs_in(own))
        self.assertIn(r._STATES["duplicate"][0], text_of(own))
        self.assertIn(OCCURRENCE, text_of(own))

    def test_duplicate_no_op_and_error_records_never_show_a_new_result(self):
        bundle = self.inspected()
        self.assertComplete(bundle)
        ids, seen = operation_ids(bundle), views(bundle)
        shown = {(item["path"], item["start_line"]): item["shows_new_result"]
                 for view in seen.values() for item in view["logs"]}
        self.assertEqual(shown, {(WEBHOOK, 7): False, (WEBHOOK, 9): False, (WEBHOOK, 11): False, (WEBHOOK, 15): False,
                                 (WEBHOOK, 17): True, (TASKS, 9): False, (TASKS, 12): False})
        # The no-op record links no result fact. The required release result stays visible as unconfirmed;
        # showing an action boundary must not create compatible telemetry or a successful result.
        released = fact_id(bundle, "funds.released")
        [noop] = [item for item in seen[ids["release"]]["logs"] if item["lifecycle_role"] == "no_op"]
        self.assertEqual(noop["fact_ids"], [])
        [result] = [item for item in bundle["log_coverage"]["facts"] if item["id"] == released]
        self.assertEqual((result["instrumentation"], result["lifecycle_check"], result["event_keys"]),
                         ("unknown", "no_compatible_site", []))
        [unconfirmed] = [item for item in seen[ids["release"]]["facts"] if item["id"] == released]
        self.assertEqual(unconfirmed["instrumentation"], "unknown")
        [started] = [item for item in seen[ids["release"]]["facts"] if item["id"] != released]
        self.assertEqual((started["id"], started["lifecycle_roles"]), (fact_id(bundle, "release.start"), ["start"]))
        page = build_html(bundle)
        for key, role in (("accept", "duplicate"), ("refund", "error"), ("release", "no_op")):
            logs = text_of(section(card_of(page, number_of(bundle, ids[key])), "logs"))
            self.assertIn(r._STATES[role][0], logs)
            self.assertRegex(logs, r"не подтверждает")

    def test_inspector_markup_is_safe_and_untrusted_names_stay_text(self):
        names = {"accept": "Принять </text><script>steal()</script> уведомление"}
        bundle = self.inspected(reply=lambda brief, attempt: refund_reply(brief, attempt, names=names))
        self.assertComplete(bundle)
        original = copy.deepcopy(bundle)
        page = build_html(bundle)
        from test_report import ReportTests
        ReportTests.assertSafeMarkup(self, page)
        self.assertEqual(page.count("<script>"), 1)
        self.assertIn("&lt;script&gt;steal()&lt;/script&gt;", page)
        self.assertEqual(bundle, original)
        self.assertIn('data-section="logs"', card_of(page, number_of(bundle, operation_ids(bundle)["accept"])))


class LegacyInspectorTests(unittest.TestCase):
    def test_a_map_without_atomic_record_keeps_event_key_links_labelled_as_candidates(self):
        from test_scenario_graph import proposal
        from test_workflow import analyse, instrumentation_response, metrics_response
        bundle = analyse(proposal(), instrumentation_response(), metrics_response())[0]
        self.assertNotIn("atomic_map", bundle)
        seen = views(bundle)
        submit = seen["submit-op"]
        self.assertEqual((submit["linkage"], submit["initiation"]), ("event_key", None))
        self.assertEqual([(item["id"], item["linkage"]) for item in submit["facts"]], [("submitted", "event_key")])
        self.assertEqual([(item["event_key"], item["lifecycle_role"], item["shows_new_result"])
                          for item in submit["logs"]], [("order.submitted", "request", False)])
        self.assertEqual(seen["approve-op"]["facts"], [])
        self.assertRegex(text_of(card_of(build_html(bundle), 1)), r"по ключу события")


class OperationImprovementTests(InspectorCase):
    def test_logging_changes_belong_to_an_operation_only_through_its_fact_and_overlap_stays_a_candidate(self):
        bundle = self.inspected()
        self.assertComplete(bundle)
        ids, seen = operation_ids(bundle), views(bundle)
        accept, refund, release = (seen[ids[key]] for key in ("accept", "refund", "release"))
        self.assertEqual([(item["card_id"], item["fact_id"], item["linkage"]) for item in refund["improvements"]],
                         [("card-done-amount", fact_id(bundle, "refund.done"), "fact")])
        # The amount change cites line 17, which the shared unit range of the accept operation also covers.
        self.assertEqual([item["card_id"] for item in accept["improvements"]], ["card-new-refund"])
        # The unlogged boundary has an explicit code-led owner; source overlap cannot lend it to another action.
        self.assertEqual(accept["improvement_candidates"], [])
        self.assertEqual(refund["improvement_candidates"], [])
        self.assertEqual([(item["card_id"], item["location"]) for item in release["improvements"]],
                         [("card-release", "location_unresolved")])
        self.assertEqual(release["improvement_candidates"], [])
        placed = [item["card_id"] for view in seen.values()
                  for key in ("improvements", "improvement_candidates") for item in view[key]]
        # The change on the approved obligation's fact (no operation realizes it) and without location stays with
        # its fact: no operation owns it and no operation is offered as a candidate.
        plan = {card["id"]: card for card in bundle["instrumentation_plan"]["cards"]}
        self.assertEqual(plan["card-order"]["fact_id"], fact_id(bundle, "order.created"))
        self.assertNotIn("card-order", placed)
        page = build_html(bundle)
        self.assertEqual(page.count('class="card logging-card"'), len(bundle["instrumentation_plan"]["cards"]))
        done, new = (f'href="#card-{card_number(bundle, key)}"' for key in ("card-done-amount", "card-new-refund"))
        refund_changes = section(card_of(page, number_of(bundle, ids["refund"])), "improvements")
        self.assertIn(done, refund_changes)
        self.assertIn("amount", refund_changes)  # what to add, shown with the operation; the plan card stays single
        accept_changes = section(card_of(page, number_of(bundle, ids["accept"])), "improvements")
        self.assertIn(new, accept_changes)
        self.assertNotIn(done, accept_changes)
        self.assertNotRegex(text_of(accept_changes), r"кандидат")  # exact code-led owner, not an overlap guess

    def test_an_invalid_optional_change_location_is_still_recovered_and_stays_honest(self):
        bundle = self.inspected(instrumentation=cards(release_ref=ref(TASKS, 1, 2)))
        self.assertEqual(bundle["validation"]["stages"]["instrumentation"]["outcome"],
                         "accepted_with_warnings_after_repair")  # the existing recovery, unchanged
        plan = {card["id"]: card for card in bundle["instrumentation_plan"]["cards"]}
        self.assertEqual((plan["card-release"]["location"], plan["card-release"]["source_anchor"]),
                         ("location_unresolved", None))
        ids, seen = operation_ids(bundle), views(bundle)
        [item] = seen[ids["release"]]["improvements"]
        self.assertEqual((item["card_id"], item["location"]), ("card-release", "location_unresolved"))
        changes = section(card_of(build_html(bundle), number_of(bundle, ids["release"])), "improvements")
        self.assertIn("Место в коде не определено", text_of(changes))


class OperationRouteTests(InspectorCase):
    def test_previous_and_next_operations_show_condition_and_basis_and_missing_order_is_explicit(self):
        # Migrated for direct-atomic-validator/10: the ordinary pipeline accepts only the grounded route (accept and
        # refund are one unit). The earlier second route accept -> release joined independently invoked commands and
        # is now an inactive proposal (negative control below); the presentation of a second, unconditioned route
        # out of one action is checked on a pure synthetic view input instead (with_view_route). Every UI check stays.
        grounded = self.inspected(reply=lambda brief, attempt: refund_reply(brief, attempt, edges=EDGES))
        self.assertComplete(grounded)
        # Code-led: every accepted order rests on the source; business text is no basis any more.
        self.assertEqual({item["basis"] for item in grounded["atomic_map"]["transitions"]}, {"source_condition"})
        self.assertEqual({item["support"]["kind"] for item in grounded["atomic_map"]["transitions"]}, {"same_unit"})
        bundle = with_view_route(grounded, "accept", "release")
        ids, seen = operation_ids(bundle), views(bundle)

        def route(items):
            return sorted((item["operation_id"], item["condition"] or "", item["basis"]) for item in items)
        self.assertEqual(route(seen[ids["accept"]]["next"]),
                         sorted([(ids["refund"], CONDITION, "source_condition"), (ids["release"], "", "source_condition")]))
        self.assertEqual(route(seen[ids["refund"]]["previous"]), [(ids["accept"], CONDITION, "source_condition")])
        self.assertEqual((seen[ids["refund"]]["next"], seen[ids["release"]]["next"], seen[ids["accept"]]["previous"]),
                         ([], [], []))
        for view in seen.values():  # defaults never pretend an established choice, parallel start or join
            self.assertEqual((view["continuation"], view["join"]), ("unknown", "unknown"))
        page = build_html(bundle)
        self.assertRegex(page, re.compile(r"не\s+трасс", re.I))  # highlighted paths are map links, not a run trace
        following = section(card_of(page, number_of(bundle, ids["accept"])), "next")
        words = text_of(following)
        for key in ("refund", "release"):
            self.assertIn(f'href="#op-{number_of(bundle, ids[key])}"', following)
        self.assertIn(CONDITION, words)
        self.assertIn("по условию в коде", words)
        self.assertNotIn("по бизнес-описанию", words)
        self.assertRegex(words, re.compile(r"условие[^.]{0,30}не установлен", re.I))
        self.assertRegex(words, re.compile(r"(выбор|способ)\w*[^.]{0,60}не установлен", re.I))
        for phrase in ("Следующий шаг", "После завершения", "параллельн"):
            self.assertNotIn(phrase, words)
        last = text_of(section(card_of(page, number_of(bundle, ids["refund"])), "next"))
        self.assertRegex(last, r"не установлен")
        self.assertNotRegex(last, re.compile(r"заверш|конеч|конец", re.I))  # no arrow is not an end
        earlier = section(card_of(page, number_of(bundle, ids["refund"])), "previous")
        self.assertIn(f'href="#op-{number_of(bundle, ids["accept"])}"', earlier)

    def test_an_unsupported_route_between_independent_commands_stays_inactive_and_addressed(self):
        # Ordinary pipeline, no synthetic view: the accept -> release proposal cites only webhook lines, and no call,
        # return, callback or contract connects handle_refund with release_funds_task.
        bundle = self.inspected(reply=lambda brief, attempt: refund_reply(brief, attempt, edges=OPEN_EDGES))
        ids, record = operation_ids(bundle), bundle["atomic_map"]
        self.assertEqual([(item["from_operation"], item["to_operation"])
                          for item in bundle["process_map"]["operation_transitions"]], [(ids["accept"], ids["refund"])])
        [inactive] = [item for item in record["code_led"]["transition_support"]["proposals"]
                      if item["state"] == "inactive"]
        self.assertEqual((inactive["from"], inactive["to"], inactive["reason"], inactive["condition"]),
                         (ids["accept"], ids["release"], "no_source_chain", None))
        [gap] = [gap for gap in record["code_led"]["gaps"] if gap["kind"] == "unsupported_transition"]
        self.assertEqual((gap["operation_ids"], gap["source"]), ([ids["accept"], ids["release"]], "host"))
        self.assertEqual((record["status"], record["status_reasons"]), ("partial", ["open_gaps"]))
        self.assertEqual(bundle["process_map"]["transitions"], [])
        seen = views(bundle)
        self.assertEqual([item["operation_id"] for item in seen[ids["accept"]]["next"]], [ids["refund"]])
        self.assertEqual(seen[ids["release"]]["previous"], [])
        for key in ("accept", "release"):  # the open question is addressed to both actions, never a route
            self.assertTrue(any(gap["id"] in item["text"] for item in seen[ids[key]]["questions"]), key)
        self.assertTrue(any(gap["id"] in line for line in bundle["process_map"]["unknowns"]))
        self.assertEqual(len([call for call in self.business.calls if call["task"] == "TASK: map-atomic"]),
                         len([item for item in record["requests"] if item["kind"] == "atomic"]))  # no extra call

    def test_choice_join_and_conditional_completion_are_shown_only_when_grounded(self):
        # Migrated for direct-atomic-validator/10: only the grounded route (EDGES); continuation, join and completion
        # come from each action's own cited details, never from the number of routes, so every check stays.
        grounded = {"accept": details(continuation="choice", refs=[ref(WEBHOOK, 8, 8)]), "release": RELEASE_DETAILS}
        bundle = self.inspected(reply=lambda brief, attempt: refund_reply(brief, attempt, edges=EDGES,
                                                                          with_details=grounded))
        self.assertComplete(bundle)
        ids, seen = operation_ids(bundle), views(bundle)
        self.assertEqual(seen[ids["accept"]]["continuation"], "choice")
        self.assertEqual(seen[ids["refund"]]["join"], "unknown")
        self.assertIsNone(seen[ids["refund"]]["details"])
        self.assertEqual(seen[ids["release"]]["details"]["completion_condition"], COMPLETION)
        page = build_html(bundle)
        self.assertIn(COMPLETION, text_of(card_of(page, number_of(bundle, ids["release"]))))
        for key in ("accept", "refund"):
            self.assertNotIn(COMPLETION, text_of(card_of(page, number_of(bundle, ids[key]))))
        self.assertNotRegex(text_of(card_of(page, number_of(bundle, ids["accept"]))), re.compile("параллельн", re.I))


class RelatedPathTests(unittest.TestCase):
    """Structural reachability over accepted transitions, never an observed trace of one case."""

    def related(self, selected, pairs):
        from process_observability.operation_view import related_operations
        transitions = [{"id": pair, "from_operation": pair.split(">")[0], "to_operation": pair.split(">")[1],
                        "condition": None} for pair in pairs]
        found = related_operations(transitions, selected)
        return set(found["upstream"]), set(found["downstream"]), set(found["transition_ids"])

    def test_related_paths_follow_branches_and_cycles_but_never_a_bypass(self):
        branching = ["a>b", "a>c", "b>s", "c>s", "s>d", "s>e", "d>f", "e>f", "a>f", "b>off", "off>other", "x>y"]
        self.assertEqual(self.related("s", branching),
                         ({"a", "b", "c"}, {"d", "e", "f"}, {"a>b", "a>c", "b>s", "c>s", "s>d", "s>e", "d>f", "e>f"}))
        cycles = ["a>s", "s>b", "b>s", "b>c", "c>d", "d>c", "s>s", "x>y", "y>x", "x>x"]
        self.assertEqual(self.related("s", cycles),
                         ({"a", "b"}, {"b", "c", "d"}, {"a>s", "s>b", "b>s", "b>c", "c>d", "d>c", "s>s"}))
        self.assertEqual(self.related("z", ["x>y", "x>x"]), (set(), set(), set()))
        chain = [f"n{index}>n{index + 1}" for index in range(12000)]  # no recursion limit on long maps
        upstream, downstream, transitions = self.related("n6000", chain)
        self.assertEqual((len(upstream), len(downstream), len(transitions)), (6000, 6000, 12000))


class OperationCodeTests(InspectorCase):
    def test_code_links_lead_with_file_symbol_and_lines_and_label_the_model_interpretation(self):
        bundle = self.inspected(reply=lambda brief, attempt: refund_reply(brief, attempt,
                                                                          with_details={"refund": REFUND_DETAILS}))
        self.assertComplete(bundle)
        ids, seen = operation_ids(bundle), views(bundle)
        code = {(item["path"], item["start_line"], item["end_line"]): item for item in seen[ids["refund"]]["code"]}
        self.assertEqual(code[(WEBHOOK, 11, 17)]["cited_by"], "model")
        self.assertEqual(code[(WEBHOOK, 6, 18)]["cited_by"], "host_unit")
        quotes = {item["id"]: item["safe_quote"] for item in bundle["evidence"]}
        for item in code.values():
            lines = item["preview"].splitlines()
            self.assertTrue(1 <= len(lines) <= 5, item)
            self.assertEqual(lines[0], quotes[item["evidence_id"]].splitlines()[0])
        block = section(card_of(build_html(bundle), number_of(bundle, ids["refund"])), "code")
        links = hrefs_in(block)
        for path, first, last in code:  # each fragment opens in full through the existing anchors
            self.assertTrue(source_hrefs(bundle, path, first, last) & links, (path, first, last))
        self.assertTrue(source_hrefs(bundle, WEBHOOK, 6, 6) & links)  # the explanation's own cited line
        words = text_of(block)
        self.assertIn(WEBHOOK, words)
        if "src-" in words:
            self.assertLess(words.index(WEBHOOK), words.index("src-"))
        self.assertIn(EXPLANATION["explanation"], words)
        self.assertLess(words.index("handle_refund"), words.index(EXPLANATION["explanation"]))
        self.assertRegex(words, re.compile(r"интерпретац", re.I))


if __name__ == "__main__":
    unittest.main()
