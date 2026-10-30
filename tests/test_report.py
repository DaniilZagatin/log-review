from copy import deepcopy
import html
from html.parser import HTMLParser
import re
import unittest

from process_observability.contracts import (
    Applicability,
    ArtifactStatus,
    Assessment,
    CoverageSummary,
    EventMapping,
    EvidenceRef,
    FindingState,
    InstrumentationAction,
    InstrumentationCard,
    InstrumentationPlan,
    LogCoverage,
    MetricCatalog,
    MetricSpec,
    Opportunities,
    OpportunityState,
    ProcessMap,
    Readiness,
    RequiredFact,
    SourceAnchor,
    SourceKind,
    Stage,
    Transition,
    ValidationState,
)
from process_observability.manifest import (
    DataOrigin,
    InputFingerprint,
    ModelMode,
    Provenance,
    RunManifest,
    Stages,
    StageState,
    StageStatus,
)
from process_observability.report import build_html, build_markdown

RUN = "demo-run-1"
SHA = "a" * 64
NO_ARTIFACT = "Результат раздела не получен."
FENCE = re.compile(r"^(`{3,})text\n.*?\n\1$", re.M | re.S)
ALLOWED_TAGS = {
    "script", "aside", "small", "b", "strong", "button", "input", "output", "html", "head", "meta", "title", "style", "body", "main", "header", "nav", "section", "article", "div", "p",
    "h1", "h2", "h3", "h4", "span", "code", "pre", "dl", "dt", "dd", "ul", "li", "details", "summary", "figure",
    "figcaption", "table", "thead", "tbody", "tr", "th", "td", "a", "svg", "g", "title", "rect", "text", "path",
    "polygon", "circle", "defs", "marker", "tspan",
}

ANCHOR = SourceAnchor(snapshot_id="snap-1", path="app/orders.py", symbol="submit_order", start_line=10, end_line=14)
CODE = EvidenceRef(id="ev-code", kind=SourceKind.CODE, snapshot_id="snap-1", locator="app/orders.py:10-14",
                   safe_quote='logger.info("order_submitted", case_id=order.id)', raw_span_hash="c" * 64,
                   source_anchor=ANCHOR, text_validation=ValidationState.VERIFIED)
DOC = EvidenceRef(id="ev-doc", kind=SourceKind.DOCUMENT, snapshot_id="doc-1", locator="proposal.docx#p3",
                  safe_quote="Событие подачи должно содержать идентификатор заявки")
TELEMETRY = EvidenceRef(id="ev-tel", kind=SourceKind.TELEMETRY, snapshot_id="logs-1", locator="logs/day1.jsonl:1-40",
                        safe_quote='{"event": "order_submitted"}', raw_span_hash="d" * 64,
                        text_validation=ValidationState.VERIFIED, semantic_validation=ValidationState.VERIFIED,
                        claim_state=FindingState.OBSERVED)


def artifact(**fields):
    return {"run_id": RUN, "input_fingerprints": {"app/orders.py": SHA}, "evidence_ids": [], **fields}


def manifest(origin=DataOrigin.SYNTHETIC, mode=ModelMode.FAKE):
    return RunManifest(
        run_id=RUN, data_origin=origin, model_mode=mode,
        input_fingerprints=(InputFingerprint("app/orders.py", SHA),),
        provenance=Provenance("0.1.0", "3.12.1", "b" * 64),
        stages=Stages(map=StageState(StageStatus.COMPLETED),
                      coverage=StageState(StageStatus.INCOMPLETE, ("Нет данных о доставке",)),
                      instrumentation=StageState(StageStatus.COMPLETED),
                      metrics=StageState(StageStatus.BLOCKED, ("Окно наблюдения не задано",))),
    ).to_dict()


def process_map(observed=True):
    receive_observed = (Assessment(state=FindingState.OBSERVED, evidence_ids=["ev-tel"], reason="Записи есть в журнале")
                        if observed else Assessment())
    stages = [
        Stage(id="receive", name="Приём заявки",
              required=Assessment(state=FindingState.PROPOSED, reason="Описано в предложении"),
              implemented=Assessment(state=FindingState.IMPLEMENTED, evidence_ids=["ev-code"], reason="Найден обработчик"),
              observed=receive_observed, roles=["Клиент"], systems=["Портал"]),
        Stage(id="check", name="Проверка комплектности",
              required=Assessment(state=FindingState.ACCEPTED, decision_id="dec-1", reason="Утверждено владельцем"),
              implemented=Assessment(state=FindingState.NOT_FOUND, reason="Обработчик не найден"),
              alternatives_complete=True),
        Stage(id="approve", name=None),
    ]
    transitions = [
        Transition(from_stage="receive", to_stage="check", condition="Заявка подана",
                   evidence_ids=["ev-code"], state=FindingState.IMPLEMENTED),
        Transition(from_stage="check", to_stage="receive", condition="Нужна доработка"),
        Transition(from_stage="check", to_stage="approve", condition=None, state=FindingState.UNKNOWN),
    ]
    mappings = [EventMapping(event_key="order_submitted", business_action="Подача заявки", operation="submit_order",
                             stage_id="receive", lifecycle_role="request", occurrence_rule="Одна запись на попытку подачи",
                             evidence_ids=["ev-code"])]
    return ProcessMap(**artifact(status=ArtifactStatus.INCOMPLETE, evidence_ids=["ev-code"],
                                 blockers=["Журнал этапа проверки не предоставлен"]),
                      purpose="Обработка заявки клиента", boundaries=None, case_definition="Одна заявка",
                      stages=stages, transitions=transitions, event_mappings=mappings,
                      unknowns=["Ветка отказа не найдена"]).to_dict()


def coverage():
    facts = [
        RequiredFact(id="fact-submit", meaning="Заявка подана", rationale="Нужен старт цикла", scope="Все заявки",
                     condition=None, requirement=Assessment(state=FindingState.PROPOSED, reason="Из предложения DOCX"),
                     applicability=Applicability.UNKNOWN, metric_ids=["cycle-time"],
                     evidence_alternatives=["Журнал портала"], instrumentation="found", opportunities=Opportunities(),
                     delivery="unknown", quality="unknown", evidence_ids=["ev-code"]),
        RequiredFact(id="fact-check", meaning="Проверка завершена", rationale="Нужен конец этапа", scope="Все заявки",
                     condition="Если заявка полная",
                     requirement=Assessment(state=FindingState.ACCEPTED, decision_id="dec-1", reason="Утверждено"),
                     applicability=Applicability.APPLICABLE, metric_ids=["approval-share"], evidence_alternatives=[],
                     instrumentation="missing",
                     opportunities=Opportunities(state=OpportunityState.ZERO, count=0, independent_evidence_ids=["ev-tel"],
                                                 scope_complete=True, window="2026-10-01/2026-10-07"),
                     delivery="missing", quality="unusable"),
    ]
    summary = CoverageSummary(scope_provisional=True, numerator=0, denominator=1, unknown=1,
                              candidate_fact_ids=["fact-submit"], excluded_fact_ids=[])
    return LogCoverage(**artifact(status=ArtifactStatus.INCOMPLETE, blockers=["Доставка не проверена"]),
                       facts=facts, summary=summary).to_dict()


def plan():
    cards = [
        InstrumentationCard(id="card-check", fact_id="fact-check", action=InstrumentationAction.ADD_EVENT,
                            rationale="Нет записи о завершении проверки", semantics="Проверка завершена",
                            fields={"case_id": "Идентификатор заявки"}, transaction_timing="После фиксации транзакции",
                            dependencies=[], metric_ids=["approval-share"],
                            validation_plan="Прогнать синтетическую заявку и сверить запись",
                            location="anchored", source_anchor=ANCHOR, evidence_ids=["ev-code"]),
        InstrumentationCard(id="card-map", fact_id="fact-submit", action=InstrumentationAction.MAP,
                            rationale="Запись подачи уже есть", semantics="Подача заявки", fields={},
                            transaction_timing=None, dependencies=["Схема канала"], metric_ids=[],
                            validation_plan="Сверить ключ события с контрактом канала"),
    ]
    return InstrumentationPlan(**artifact(status=ArtifactStatus.DRAFT), cards=cards).to_dict()


def metric_specs():
    return [
        MetricSpec(id="cycle-time", purpose="Время цикла заявки", formula_id=None, formula_text="end - start",
                   counting_unit=None, cohort=None, window=None, time_semantics=None, open_case_policy=None,
                   late_data_policy=None, dependencies=["fact-submit", "fact-check"],
                   readiness=Readiness(primary="telemetry_missing",
                                       blockers=["Нет события завершения", "Не задано окно наблюдения"]),
                   value_reason="Телеметрия не измерялась"),
        MetricSpec(id="approval-share", purpose="Доля одобренных заявок", formula_id="share/1",
                   formula_text="approved / submitted", counting_unit="заявка", cohort="Поданные за окно",
                   window="2026-10-01/2026-10-07", time_semantics="Время подачи", open_case_policy="Исключить открытые",
                   late_data_policy="Не учитывать после закрытия окна", dependencies=["fact-check"],
                   readiness=Readiness(primary="ready", blockers=[]), value=0.25, computed_from=["ev-tel"]),
    ]


def complete_bundle():
    return {
        "manifest": manifest(),
        "process_map": process_map(),
        "log_coverage": coverage(),
        "instrumentation_plan": plan(),
        "metric_catalog": MetricCatalog(**artifact(status=ArtifactStatus.BLOCKED, blockers=["Метод расчёта не утверждён"]),
                                        metrics=metric_specs()).to_dict(),
        "evidence": [item.to_dict() for item in (CODE, DOC, TELEMETRY)],
        "insights": [{"id": "ins-1", "kind": "observation", "statement": "Подача заявки видна в журнале",
                      "evidence_ids": ["ev-tel", "ev-missing"]}],
        "scope": {"selected_files": ["app/orders.py"], "max_files": 50, "excluded": ["config/secrets.env"]},
    }


def source_only_bundle():
    return {
        "manifest": manifest(DataOrigin.REAL, ModelMode.LIVE),
        "process_map": process_map(observed=False),
        "metric_catalog": MetricCatalog(**artifact(status=ArtifactStatus.INCOMPLETE, blockers=["Телеметрия не предоставлена"]),
                                        metrics=metric_specs()[:1]).to_dict(),
        "evidence": [CODE.to_dict()],
    }


class Markup(HTMLParser):
    def __init__(self, page):
        super().__init__(convert_charrefs=True)
        self.tags, self.attrs, self.ids, self.hrefs, self.style = [], [], set(), [], []
        self._in_style = False
        self.feed(page)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self._in_style = tag == "style"
        for name, value in attrs:
            self.attrs.append((tag, name, value))
            if name == "id":
                self.ids.add(value)
            if name == "href":
                self.hrefs.append(value)

    def handle_endtag(self, tag):
        self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            self.style.append(data)


def between(text, start, end):
    index = text.index(start)
    return text[index:text.index(end, index)]


def plain_markdown(text):
    return html.unescape(text.replace("\\", ""))


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.bundle = complete_bundle()
        self.html = build_html(self.bundle)
        self.md = build_markdown(self.bundle)

    def assertSafeMarkup(self, page):
        markup = Markup(page)
        self.assertLessEqual(set(markup.tags), ALLOWED_TAGS)
        self.assertEqual(markup.tags.count("style"), 1)
        for tag, name, _ in markup.attrs:
            self.assertFalse(name.startswith("on"), (tag, name))
            self.assertNotIn(name, {"src", "srcset", "style", "action", "formaction", "xlink:href"})
        self.assertTrue(markup.hrefs)
        for href in markup.hrefs:
            self.assertRegex(href, r"^#[a-z]+(-[0-9]+)?$")
            self.assertIn(href[1:], markup.ids)
        return markup

    def test_complete_bundle_renders_header_and_all_four_sections(self):
        self.assertTrue(self.html.startswith('<!DOCTYPE html>\n<html lang="ru">'))
        for anchor in ("scope", "map", "coverage", "instrumentation", "metrics", "insights", "evidence"):
            self.assertIn(f'id="{anchor}"', self.html)
        for title in ("1. Карта процесса", "2. Покрытие требуемых фактов", "3. Доработки логирования", "4. Метрики"):
            self.assertIn(f"<h2>{'2. Существующие логи процесса' if title.startswith('2.') else title}</h2>", self.html)
            self.assertIn(f"## {title}", self.md)
        for output in (self.html, self.md):
            self.assertNotIn(NO_ARTIFACT, output)
            self.assertIn("синтетические данные", output)
            self.assertIn("config/secrets.env", output)
        self.assertIn('<span class="b t-warn" title="synthetic">синтетические данные</span>', self.html)
        self.assertIn('<span class="b t-warn" title="fake">сценарная модель (fake)</span>', self.html)

    def test_rendering_is_deterministic_and_does_not_mutate_the_bundle(self):
        bundle = complete_bundle()
        snapshot = deepcopy(bundle)
        self.assertEqual(build_html(bundle), self.html)
        self.assertEqual(build_markdown(bundle), self.md)
        self.assertEqual(bundle, snapshot)

    def test_html_has_only_the_fixed_hashed_script_and_no_network_assets(self):
        from process_observability import graph_font
        self.assertSafeMarkup(self.html)
        # The OFL text names its publishers' sites; it is inert text inside <pre>, never a resource.
        self.assertEqual(self.html.count('<details class="font-license">'), 1)
        license_block = between(self.html, '<details class="font-license">', '</details>')
        self.assertIn(html.escape(graph_font.FONT_LICENSE.lstrip('﻿').strip()), license_block)
        lowered = self.html.replace(license_block, '').lower()
        for marker in ("http://", "https://", "@import", "<link", "<img", "<iframe", "javascript:"):
            self.assertNotIn(marker, lowered)
        # SVG arrowheads use local fragments. The fixed script selects only the
        # renderer-generated marker IDs; the only data URI is the embedded font.
        font = f'data:{graph_font.FONT_MIME};base64,{graph_font.FONT_BASE64}'
        targets = re.findall(r'url\(([^)]*)\)', self.html)
        self.assertEqual(targets.count(font), 1)
        for target in targets:
            self.assertTrue(target == font or target == '#${marker}' or re.fullmatch(r'#[a-z][a-z0-9-]*', target), target)
        import base64,hashlib
        from process_observability.compact_report import SCRIPT
        self.assertEqual(self.html.count("<script>"),1)
        script_hash = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
        self.assertIn("content=\"default-src 'none'; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; "
                      f"form-action 'none'; script-src 'sha256-{script_hash}'\"", self.html)
        self.assertIn("<script>"+SCRIPT+"</script>",self.html)

    def test_untrusted_strings_are_escaped_in_text_attributes_svg_and_markdown(self):
        bundle = complete_bundle()
        payload = '<script>alert(1)</script>"><img src=x onerror=alert(1)>'
        bundle["manifest"]["run_id"] = "</title>" + payload
        bundle["process_map"]["stages"][0]["name"] = payload
        bundle["process_map"]["transitions"][0]["condition"] = "<svg onload=alert(1)>]]><![CDATA["
        bundle["process_map"]["unknowns"].append("<iframe src=//evil.example>")
        code = bundle["evidence"][0]
        code["safe_quote"] = "</pre></details><script>steal()</script>\n```\n<b onclick=x>"
        code["source_anchor"]["path"] = 'app/"><img src=x onerror=alert(1)>.py'
        bundle["evidence"].append({"id": 'ev"<i>', "kind": "document", "snapshot_id": "doc",
                                   "locator": "<a href=javascript:alert(1)>x</a>"})
        bundle["process_map"]["evidence_ids"] = ['ev"<i>']
        bundle["scope"]["<i onmouseover=alert(1)>ключ</i>"] = "[нажми](javascript:alert(1))"
        bundle["insights"].append({"link": "<a href='https://evil.example'>x</a>", "nested": {"deep": ["<style>*{}</style>"]}})
        bundle["metric_catalog"]["metrics"][0]["formula_text"] = "</style><style>body{background:url(https://evil.example/x)}</style>"
        bundle["instrumentation_plan"]["cards"][0]["fields"] = {"<b>field</b>": "<u>value</u>"}

        page = build_html(bundle)
        markup = self.assertSafeMarkup(page)
        self.assertEqual(markup.tags.count("svg"), 1)
        self.assertNotIn("evil", "".join(markup.style))
        self.assertNotIn("<![CDATA[", page)
        self.assertIn(html.escape(payload), page)
        self.assertIn("&lt;svg onload=alert(1)&gt;", between(page, "<svg", "</svg>"))
        self.assertIn(html.escape(code["safe_quote"]), page)

        text = build_markdown(bundle)
        outside = FENCE.sub("", text)
        self.assertNotIn("<", outside)
        self.assertNotIn("](", outside)
        self.assertIn("&lt;script&gt;", outside)
        self.assertIn("````text\n" + code["safe_quote"] + "\n````", text)

    def test_source_only_run_stays_useful_without_claimed_measurements(self):
        bundle = source_only_bundle()
        page, text = build_html(bundle), build_markdown(bundle)
        for output in (page, text):
            self.assertEqual(output.count(NO_ARTIFACT), 2)
            self.assertIn("Область анализа (выбранные файлы, лимиты) не передана", output)
            self.assertNotIn("Значение не рассчитано", output)
            self.assertIn("Приём заявки", output)
            self.assertNotIn("Синтетические входные данные", output)
        self.assertIn("реальные данные", page)
        self.assertIn("живая LLM \\(live\\)", text)
        observed = re.findall(r'<div class="view v-observed">.*?</div>', page)
        self.assertEqual(len(observed), 3)
        for view in observed:
            self.assertIn("t-unknown", view)
            self.assertNotIn("t-ok", view)
        self.assertNotIn('class="num"', page)
        self.assertIn("- Наблюдается: _неизвестно_ — Not evaluated", text)

    def test_empty_bundle_renders_explicit_unknowns(self):
        for output in (build_html({}), build_markdown({})):
            self.assertIn("Манифест запуска не передан", output)
            self.assertEqual(output.count(NO_ARTIFACT), 4)
            self.assertIn("Реестр доказательств не передан", output)
            self.assertIn("неизвестно", output)
        self.assertIn("- Блокеры: _неизвестны_", build_markdown({}))

    def test_unknown_states_are_gray_and_never_zero_or_success(self):
        approve = between(self.html, 'id="st-3"', "</article>")
        self.assertIn("без названия", approve)
        self.assertGreaterEqual(approve.count("t-unknown"), 3)
        self.assertNotIn("t-ok", approve)
        logging = between(self.html, 'id="coverage"', '</section>')
        for heading in ("Применимость", "Качество", "Статус требований"):
            self.assertNotIn(heading, logging)
        self.assertEqual(self.bundle["log_coverage"]["facts"][0]["applicability"], "unknown")
        unready = between(self.html, 'id="m-1"', "</article>")
        self.assertIn("end - start", unready)
        self.assertNotIn("Значение не рассчитано", unready)
        self.assertNotIn("t-ok", unready)
        rows = {line.split(" | ")[0]: line for line in self.md.splitlines() if line.startswith("| fact-")}
        self.assertNotIn("количество", rows["| fact-submit"])
        self.assertIn("_неизвестно_", rows["| fact-submit"])
        self.assertIn("количество: 0", rows["| fact-check"])

    def test_process_map_shows_transitions_labels_and_unknown_branches(self):
        svg = between(self.html, "<svg", "</svg>")
        self.assertEqual(svg.count('class="edge"'), 3)
        self.assertEqual(svg.count('class="node"'), 3)
        self.assertIn("№1: Приём заявки → Проверка комплектности; условие: Заявка подана; состояние: реализовано", svg)
        self.assertIn("условие: Нужна доработка", svg)
        self.assertIn("условие: не указано", svg)
        self.assertIn(": не найдено</text>", svg)
        self.assertIn("Неизвестные ветви и пробелы: 1", svg)
        table = between(self.html, "<h3>Переходы</h3>", "</table>")
        for expected in ("Заявка подана", "Нужна доработка", "условие не указано", 'href="#st-1"', 'href="#st-3"'):
            self.assertIn(expected, table)
        self.assertIn("| 1 | Приём заявки (receive) | Проверка комплектности (check) | Заявка подана | реализовано |", self.md)
        self.assertIn("| 3 | Проверка комплектности (check) | approve (approve) | _условие не указано_ | _неизвестно_ |", self.md)
        self.assertIn("<code>order_submitted</code>", self.html)
        self.assertIn("| order\\_submitted | Подача заявки | submit\\_order | Приём заявки (receive) | запрос |", self.md)
        for output in (self.html, self.md):
            self.assertIn("Ветка отказа не найдена", output)

    def test_instrumentation_cards_show_location_or_unresolved_and_validation(self):
        anchored = between(self.html, 'id="card-1"', "</article>")
        for expected in ("app/orders.py", "submit_order", "10–14", "snap-1", "case_id", "После фиксации транзакции",
                         "Прогнать синтетическую заявку и сверить запись", 'href="#fact-2"'):
            self.assertIn(expected, anchored)
        unresolved = between(self.html, 'id="card-2"', "</article>")
        self.assertIn("Место в коде не определено", unresolved)
        self.assertIn("Нужно определить место изменения", unresolved)
        self.assertEqual(self.bundle["instrumentation_plan"]["cards"][1]["location"],"location_unresolved")
        self.assertIn("Сверить ключ события с контрактом канала", unresolved)
        self.assertIn("путь app/orders.py; символ submit\\_order; строки 10–14; снимок snap-1", self.md)
        self.assertIn("- Место в коде: _не определено_", self.md)

    def test_logging_default_columns_answer_business_questions(self):
        from process_observability.report import _Run, _hcoverage
        bundle = deepcopy(self.bundle)
        bundle["logging_inventory"] = {"sites": [{"path": "app/orders.py", "start_line": 10, "end_line": 14,
            "event_key": "order_submitted", "fields": ["case_id"]}]}
        section = _hcoverage(_Run(bundle), bundle["log_coverage"])
        self.assertEqual(re.findall(r"<th>(.*?)</th>", section), ["Какое действие помогает увидеть", "Что записывается",
                                                               "В какой момент", "Код записи"])
        self.assertIn("Подача заявки", section)
        self.assertIn("order_submitted", section)
        self.assertIn('href="#ev-1"', section)
        for absent in ("Применимость", "Качество", "неизвестно", "Добавить запись"):
            self.assertNotIn(absent, section)

    def test_logging_card_has_human_title_and_does_not_call_unspecified_fields_unnecessary(self):
        card = between(self.html, 'id="card-2"', "</article>")
        self.assertIn("<h3>Заявка подана</h3>", card)
        self.assertIn("Состав данных не указан.", card)
        self.assertNotIn("новых полей нет", card)
        self.assertIn("Момент записи не указан", card)
        self.assertIn("Служебные идентификаторы", card)
        self.assertIn("<code>card-map</code>", card)

    def test_failed_plan_is_not_presented_as_no_changes_needed(self):
        bundle = deepcopy(self.bundle)
        bundle["instrumentation_plan"]["cards"] = []
        bundle["manifest"]["stages"]["instrumentation"]["status"] = "incomplete"
        pane = between(build_html(bundle), 'id="instrumentation"', '</section>')
        self.assertIn("Предложения по доработкам ещё не подготовлены", pane)
        self.assertNotIn("Предложений по логированию нет", pane)

    def test_logging_fields_explain_meaning_before_exact_name(self):
        card = between(self.html, 'id="card-1"', "</article>")
        self.assertIn("<th>Для чего нужны данные</th><th>Имя в записи</th>", card)
        self.assertIn("<td>Идентификатор заявки</td><td><code>case_id</code></td>", card)
        self.assertIn('href="#m-2">Доля одобренных заявок</a>', card)

    def test_logging_dependencies_resolve_to_human_names_without_changing_ids(self):
        bundle = deepcopy(self.bundle)
        bundle["instrumentation_plan"]["cards"][0]["dependencies"] = ["fact-submit", "card-map", "Схема канала"]
        card = between(build_html(bundle), 'id="card-1"', "</article>")
        self.assertIn('href="#fact-1">Заявка подана</a>', card)
        self.assertIn('href="#card-2">Заявка подана</a>', card)
        self.assertIn("Схема канала", card)
        self.assertEqual(bundle["instrumentation_plan"]["cards"][0]["dependencies"],
                         ["fact-submit", "card-map", "Схема канала"])

    def test_blank_human_labels_do_not_expose_identifier_markup(self):
        from process_observability.report import _Run, _hdependency, _hmetric_link
        bundle = deepcopy(self.bundle)
        identifier = '<img src=x onerror=alert(1)>'
        bundle["log_coverage"]["facts"][0].update(id=identifier, meaning=None)
        bundle["metric_catalog"]["metrics"][0].update(id=identifier, purpose=None)
        run = _Run(bundle)
        for rendered in (_hdependency(run, identifier), _hmetric_link(run, identifier)):
            self.assertIn(html.escape(identifier), rendered)
            self.assertNotIn('<img', rendered)

    def test_logging_only_shows_actual_sites_and_preserves_unmapped_recall(self):
        from process_observability.report import _Run, _hcoverage
        bundle = deepcopy(self.bundle)
        fact = bundle["log_coverage"]["facts"][0]
        fact["instrumentation"] = "unknown"
        fact["event_keys"] = ["proposed_new_log"]
        bundle["logging_inventory"] = {"sites": [
            {"path": "app/orders.py", "start_line": 10, "end_line": 14, "event_key": "order_submitted"},
            {"path": "app/orders.py", "start_line": 15, "end_line": 15, "event_key": "technical_timeout"}]}
        section = _hcoverage(_Run(bundle), bundle["log_coverage"])
        self.assertIn("order_submitted", section)
        self.assertNotIn("proposed_new_log", section)
        self.assertNotIn("technical_timeout", section)
        self.assertIn("Записи без сопоставления с действиями: 1", section)
        self.assertIn("technical_timeout", build_html(bundle))
        self.assertEqual(fact["instrumentation"], "unknown")

    def test_logging_long_and_hostile_field_descriptions_stay_complete_and_escaped(self):
        bundle = deepcopy(self.bundle)
        description = "<em>Идентификатор</em> " + "длинное описание " * 200
        bundle["instrumentation_plan"]["cards"][0]["fields"] = {"<b>case</b>": description}
        card = between(build_html(bundle), 'id="card-1"', "</article>")
        self.assertIn(html.escape(description), card)
        self.assertIn("&lt;b&gt;case&lt;/b&gt;", card)
        self.assertNotIn("<em>", card)

    def test_metric_cards_show_definitions_and_inputs_without_runtime_results_or_empty_rows(self):
        snapshot=deepcopy(self.bundle)
        for card_id,formula in [('m-1','end - start'),('m-2','approved / submitted')]:
            card=between(self.html,f'id="{card_id}"','</article>')
            self.assertIn(formula,card)
            for absent in ('Готовность:','Значение:','Значение не рассчитано','Рассчитано из:',
                           'Окно наблюдения','не задано','не задана','Что уточнить'):
                self.assertNotIn(absent,card)
            self.assertIn('Что нужно записывать',card)
        self.assertNotIn('- Значение:',self.md)
        self.assertNotIn('- Готовность:',self.md)
        self.assertEqual(self.bundle,snapshot)
        self.assertEqual(self.bundle['metric_catalog']['metrics'][1]['value'],0.25)

    def test_undefined_metric_host_placeholder_is_localized_without_changing_data(self):
        bundle = deepcopy(self.bundle)
        placeholder = "Referenced by proposed facts or instrumentation; definition not established"
        metric = bundle["metric_catalog"]["metrics"][0]
        metric.update(purpose=placeholder, formula_text=None)
        for output in (build_html(bundle), build_markdown(bundle)):
            self.assertNotIn(placeholder, output)
            self.assertIn("Метрика cycle-time", output)
        self.assertEqual(metric["purpose"], placeholder)

    def test_unrun_metric_stage_shows_one_russian_status_block(self):
        bundle = deepcopy(self.bundle)
        placeholder = "Referenced by proposed facts or instrumentation; definition not established"
        host = "metrics stage not run: instrumentation stage did not complete"
        for metric in bundle["metric_catalog"]["metrics"]:
            metric.update(purpose=placeholder, formula_id=None, formula_text=None, value=None, value_reason="not run")
        bundle["manifest"]["stages"]["metrics"] = {"status": "incomplete", "blockers": [host, "unmapped host detail"]}
        snapshot = deepcopy(bundle)
        page, text = build_html(bundle), build_markdown(bundle)
        section = between(page, '<section id="metrics"', '</section>')
        self.assertEqual(section.count('class="metric-status"'), 1)
        self.assertNotIn('<article class="metric"', section)
        self.assertNotIn("Формула не сформирована", section)
        for output in (section, text):
            self.assertNotIn(placeholder, output)
            self.assertIn("Стадия метрик не запускалась: доработки логирования не завершены.", output)
            self.assertIn("Метрика cycle-time", output)
            self.assertIn("Метрика approval-share", output)
        ordinary = section.split('<details class="technical">')[0]
        self.assertNotIn(host, ordinary)
        self.assertNotIn("unmapped host detail", ordinary)
        self.assertIn("unmapped host detail", section)
        # Fact links keep working: each pending metric keeps its anchor and its required facts.
        self.assertSafeMarkup(page)
        self.assertIn('id="m-1"', section)
        self.assertIn('id="m-2"', section)
        self.assertIn('href="#fact-1"', section)
        self.assertIn('href="#fact-2"', section)
        self.assertEqual(bundle, snapshot)

    def test_defined_metrics_keep_full_cards_next_to_pending_ones(self):
        bundle = deepcopy(self.bundle)
        bundle["metric_catalog"]["metrics"][0].update(
            purpose="Referenced by proposed facts or instrumentation; definition not established", formula_text=None)
        section = between(build_html(bundle), '<section id="metrics"', '</section>')
        self.assertEqual(section.count('class="metric-status"'), 1)
        card = between(section, 'id="m-2"', "</article>")
        for expected in ("Доля одобренных заявок", "approved / submitted", "заявка", "Поданные за окно"):
            self.assertIn(expected, card)

    def test_legacy_stage_map_keeps_full_names_and_states(self):
        bundle = deepcopy(self.bundle)
        name = ("Проверка комплектности документов заявителя, включая доверенности представителей "
                "и согласия на обработку персональных данных")
        bundle["process_map"]["stages"][1]["name"] = name
        bundle["process_map"]["stages"][1]["implemented"]["state"] = "unverified"
        svg = between(build_html(bundle), "<svg", "</svg>")
        self.assertNotIn("…", svg)
        card = between(svg, 'href="#st-2"', "</a>")
        lines = re.findall(r"<tspan[^>]*>([^<]*)</tspan>", card)
        self.assertGreater(len(lines), 1)
        self.assertEqual(" ".join(lines), name)
        self.assertIn("Р: не проверено</text>", svg)
        self.assertNotIn('role="img"', svg)


    def test_values_without_verified_telemetry_are_flagged(self):
        bundle = complete_bundle()
        bundle["process_map"]["stages"][0]["observed"]["evidence_ids"] = ["ev-code"]
        bundle["metric_catalog"]["metrics"][1]["computed_from"] = ["ev-doc"]
        page, text = build_html(bundle), build_markdown(bundle)
        for output in (page, text):
            self.assertEqual(output.count("нет проверенной телеметрии в пакете"), 1)

    def test_evidence_blocks_expand_with_path_quote_and_limits(self):
        for number, evidence_id in enumerate(("ev-code", "ev-doc", "ev-tel"), 1):
            block = between(self.html, f'<details class="evidence" id="ev-{number}">', "</details>")
            self.assertIn(f"<code>{evidence_id}</code>", block)
        code = between(self.html, '<details class="evidence" id="ev-1">', "</details>")
        for expected in ("app/orders.py", "submit_order", "10–14", "Цитата из источника", html.escape(CODE.safe_quote)):
            self.assertIn(expected, code)
        document = between(self.html, '<details class="evidence" id="ev-2">', "</details>")
        self.assertIn("Цитата не проверена по снимку", document)
        self.assertIn('<a href="#ev-1"><code>ev-code</code></a>', self.html)
        self.assertIn("нет в реестре доказательств", self.html)
        self.assertIn("ev-missing (нет в реестре доказательств)", self.md)
        self.assertIn("```text\n" + CODE.safe_quote + "\n```", self.md)
        for output in (self.html, self.md):
            self.assertNotIn("Проверенная цитата: текст есть в снимке", output)
            self.assertNotIn("Она не подтверждает семантическое соответствие", output)
        self.assertEqual(self.bundle['evidence'][0]['text_validation'],'verified')
        self.assertEqual(self.bundle['evidence'][0]['semantic_validation'],'unverified')

    def test_all_blockers_are_visible_in_both_formats(self):
        blockers = ["Нет данных о доставке", "Окно наблюдения не задано", "Журнал этапа проверки не предоставлен",
                    "Доставка не проверена"]
        for blocker in blockers:
            self.assertIn(f"<li>{blocker}</li>", self.html)
            self.assertIn(f"- {blocker}", self.md)

    def test_markdown_carries_the_same_values_as_html(self):
        page, text = html.unescape(self.html), plain_markdown(self.md)
        values = [
            RUN, "Приём заявки", "Проверка комплектности", "receive", "check", "approve", "Заявка подана",
            "Нужна доработка", "order_submitted", "Одна запись на попытку подачи", "fact-submit", "fact-check",
            "card-check", "card-map", "Время цикла заявки", "Доля одобренных заявок",
            "approved / submitted", "app/orders.py", "submit_order", "10–14", "snap-1",
            "c" * 64, SHA, "config/secrets.env", "Подача заявки видна в журнале", "Ветка отказа не найдена", "dec-1",
            "синтетические данные", "сценарная модель (fake)", "текст не проверен",
        ]
        for value in values:
            self.assertIn(value, page)
            self.assertIn(value, text)
        self.assertIn("- Числитель (пригодные факты принятой области): 0", self.md)
        self.assertIn("- Знаменатель (принятые требования применимой или неизвестной области): 1", self.md)
        self.assertIn("- Неизвестно: 1", self.md)


def _unknown_span(text):
    return f'<span class="unk">{text}</span>'


if __name__ == "__main__":
    unittest.main()
