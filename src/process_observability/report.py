"""Offline HTML and Markdown passport of one run bundle.

Rendering only: nothing here verifies sources, scores results or adds analysis;
missing sections and values stay explicitly unknown. Every bundle string is
untrusted. The HTML escapes all text and attributes, has no network assets or
bundle-supplied links (only fragment anchors generated here), permits only
one fixed UI script by its exact CSP hash and one embedded font from a data
URI. The Markdown escapes markup and fences source quotes.
"""

import html
import json
import re

_UNKNOWN = "неизвестно"
# Control and bidirectional-override characters could hide or reorder displayed paths and quotes.
_BIDI = "".join(map(chr, (0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A))))
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f" + _BIDI + "]")
_REPLACEMENT = chr(0xFFFD)
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+!|~])")
_BACKTICKS = re.compile(r"`+")
_CSP = "default-src 'none'; style-src 'unsafe-inline'; font-src data:; base-uri 'none'; form-action 'none'"

_SECTIONS = (
    ("map", "process_map", "1. Карта процесса"),
    ("coverage", "log_coverage", "2. Покрытие требуемых фактов"),
    ("instrumentation", "instrumentation_plan", "3. Доработки логирования"),
    ("metrics", "metric_catalog", "4. Метрики"),
)
_VIEWS = (("required", "Требуется", "Т"), ("implemented", "Реализовано", "Р"), ("observed", "Наблюдается", "Н"))
_TOC = (("scope", "Область"), ("map", "Карта"), ("coverage", "Покрытие"), ("instrumentation", "Доработки логирования"),
        ("metrics", "Метрики"), ("insights", "Выводы"), ("evidence", "Доказательства"))

# State value -> (Russian label, tone). The tone picks the color; unknown is always gray.
_STATES = {
    "unknown": ("неизвестно", "unknown"),
    "proposed": ("предложено", "info"),
    "accepted": ("принято", "info"),
    "implemented": ("реализовано", "ok"),
    "observed": ("наблюдается", "ok"),
    "not_found": ("не найдено", "bad"),
    "unverified": ("не проверено", "unknown"),
    "verified": ("проверено", "ok"),
    "rejected": ("отклонено", "bad"),
    "draft": ("черновик", "warn"),
    "incomplete": ("не завершено", "warn"),
    "blocked": ("заблокировано", "bad"),
    "complete": ("завершено", "info"),
    "not_started": ("не запускалась", "unknown"),
    "completed": ("выполнена", "info"),
    "applicable": ("применимо", "info"),
    "not_applicable_approved": ("неприменимо (утверждено)", "info"),
    "found": ("найдено", "ok"),
    "missing": ("отсутствует", "bad"),
    "present": ("доставляется", "ok"),
    "usable": ("пригодно", "ok"),
    "unusable": ("непригодно", "bad"),
    "known_zero": ("известно: возможностей нет", "info"),
    "known_n": ("известно", "info"),
    "ready": ("готова к расчёту", "ok"),
    "method_missing": ("расчёт ещё не настроен", "warn"),
    "design_only": ("проект расчёта", "info"),
    "telemetry_missing": ("нет телеметрии", "warn"),
    "partial_input": ("неполные входные данные", "warn"),
    "synthetic": ("синтетические данные", "warn"),
    "real": ("реальные данные", "info"),
    "fake": ("сценарная модель (fake)", "warn"),
    "live": ("живая LLM (live)", "info"),
    "code": ("код", "info"),
    "telemetry": ("телеметрия", "info"),
    "document": ("документ", "info"),
    "derived_upstream": ("производные данные", "info"),
    "request": ("запрос", "info"),
    "start": ("начало", "info"),
    "result": ("результат", "info"),
    "error": ("ошибка", "info"),
    "refusal": ("отказ по бизнес-правилу", "info"),
    "retry": ("повторная попытка", "info"),
    "duplicate": ("повтор уже обработанного события", "info"),
    "no_op": ("действие не потребовалось", "info"),
    "ui": ("интерфейс", "info"),
    "map_existing": ("сопоставить существующее", "info"),
    "join": ("связать с существующим", "info"),
    "enrich": ("обогатить", "info"),
    "add_field": ("добавить поле", "info"),
    "add_event": ("добавить событие", "info"),
    "move_emit": ("перенести точку записи", "info"),
    "fix_delivery": ("исправить доставку", "info"),
}
_MANIFEST = {"incomplete": "не завершена", "blocked": "заблокирована"}
_TEXT = {"verified": "текст проверен", "unverified": "текст не проверен", "rejected": "текст отклонён"}
_MEANING = {"verified": "смысл подтверждён", "unverified": "смысл не проверен", "rejected": "смысл отклонён"}
_INSTRUMENTED = {"found": "найдено в коде", "missing": "не найдено в коде"}
_DELIVERY = {"present": "ключ найден в записях", "missing": "не доставляется", "incomplete": "доставка неполная"}
_READINESS = {"incomplete": "определение неполное"}
_QUOTES = {
    "verified": ("Цитата из источника", "info"),
    "rejected": ("Цитата не прошла проверку", "bad"),
}
_UNCHECKED_QUOTE = ("Цитата не проверена по снимку", "unknown")
# Tone -> (stroke/text, fill) for SVG; the CSS classes t-<tone> use the same colors.
_TONES = {
    "ok": ("#1d6b3a", "#e2f2e7"),
    "info": ("#1f4f8a", "#e6eefa"),
    "warn": ("#7a4f00", "#fcefd2"),
    "bad": ("#9b2525", "#fae3e3"),
    "unknown": ("#5f6670", "#eceef1"),
}

_QUOTE_LIMIT = ""
_NO_MANIFEST = "Манифест запуска не передан: происхождение данных, режим модели и состояние стадий неизвестны."
_NO_SCOPE = "Область анализа (выбранные файлы, лимиты) не передана: неизвестно, что осталось вне анализа."
_NO_INPUTS = "Список входных файлов неизвестен."
_NO_ARTIFACT = "Результат раздела не получен."
_NO_STAGES = "Этапы не определены — карта процесса неизвестна."
_NO_UNKNOWNS = "Открытые вопросы не перечислены."
_NO_TRANSITIONS = "Переходы не определены — связи между этапами неизвестны."
_NO_MAPPINGS = "Сопоставлений событий нет — связь технических записей с этапами неизвестна."
_MAPPING_NOTE = ("Техническая запись не обязательно равна отдельному вхождению бизнес-операции: "
                 "смотрите роль в жизненном цикле и правило вхождения.")
_NO_FACTS = "Требуемые факты не перечислены — покрытие неизвестно."
_COUNT_NOTE = ("Счётчики относятся к реестру требуемых фактов, а не к числу вызовов логгера в коде. "
               "Числитель — пригодные факты в принятой области требований. Кандидаты учитываются отдельно; доля не рассчитывается.")
_ZERO_DENOMINATOR = "Знаменатель равен 0: обязательные факты не установлены, покрытие не определено (это не 0% и не 100%)."
_PLAN_NOTE = ""
_NO_CARDS = "Предложений по логированию нет."
_UNRESOLVED = "Нужно определить место изменения."
_METRIC_NOTE = ""
_NO_METRICS = "Метрик в каталоге нет."
_FORMULA_NOTE = ""
_UNMEASURED = "нет проверенной телеметрии в пакете"
_NO_INSIGHTS = "Выводов пока нет."
_INSIGHT_NOTE = ("Вывод — утверждение анализа. Различайте дефекты данных, наблюдения, ассоциации, "
                 "подтверждённые механизмы отдельных случаев и причинные утверждения.")
_NO_EVIDENCE = "Реестр доказательств не передан: ссылки на доказательства не проверить."
_STAGE_NOTE = ""

_LEGEND = (
    '<p class="legend">Т — требуется, Р — реализовано, Н — наблюдается. '
    '<span class="b t-ok">зелёный</span> подтверждено доказательствами, '
    '<span class="b t-info">синий</span> предложено или принято, '
    '<span class="b t-bad">красный</span> не найдено, '
    '<span class="b t-unknown">серый</span> неизвестно. '
    'Сплошная стрелка — переход реализован или наблюдается, пунктир — предложен или неизвестен; '
    'номер на стрелке — строка таблицы переходов. «?» — альтернативные ветви этапа не подтверждены как полные. '
    'Это модель процесса, а не граф вызовов кода.</p>'
)

_CSS = """
:root{--fg:#1d2433;--muted:#5b6475;--line:#d8dde5;--bg:#f4f6f8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
main{max-width:1200px;margin:0 auto;padding:16px}
header.top,section{margin-bottom:16px;padding:16px 20px;border:1px solid var(--line);border-radius:10px;background:#fff}
h1{margin:0 0 8px;font-size:1.5rem}h2{margin:0 0 12px;font-size:1.25rem}
h3{margin:20px 0 8px;font-size:1.05rem}h4{margin:0 0 6px;font-size:.95rem}
p{margin:6px 0}ul{margin:4px 0;padding-left:22px}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.86em}
code{padding:0 4px;border-radius:4px;background:#eef1f5;overflow-wrap:anywhere}
pre{margin:4px 0;white-space:pre-wrap;overflow-wrap:anywhere}
a{color:#1f4f8a}
.eyebrow{margin:0;color:var(--muted);font-size:.78rem;letter-spacing:.04em;text-transform:uppercase}
.muted,.note,.legend{color:var(--muted)}.note,.legend{font-size:.88rem}
.unk{color:#5f6670;font-style:italic}
.b{display:inline-block;margin:1px 4px 1px 0;padding:0 8px;border:1px solid;border-radius:999px;font-size:.8rem;font-weight:600;line-height:1.6}
.t-ok{color:#1d6b3a;background:#e2f2e7;border-color:#9fd1ae}
.t-info{color:#1f4f8a;background:#e6eefa;border-color:#a9c2e6}
.t-warn{color:#7a4f00;background:#fcefd2;border-color:#e6c47a}
.t-bad{color:#9b2525;background:#fae3e3;border-color:#e2a3a3}
.t-unknown{color:#5f6670;background:#eceef1;border-color:#9aa1ab;border-style:dashed}
.kv{display:grid;grid-template-columns:minmax(120px,max-content) minmax(0,1fr);gap:4px 14px;margin:8px 0}
.kv dt{color:var(--muted)}.kv dd{margin:0;min-width:0}
.tiles,.stages,.cards{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr))}
.views{display:grid;gap:8px;margin-top:8px;grid-template-columns:repeat(auto-fit,minmax(min(100%,180px),1fr))}
.tile,.stage,.card,.metric,.insight,.view{min-width:0;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:#fff}
.tile h3{margin:0 0 4px}
.v-required{border-top:3px solid #6b7fa6}.v-implemented{border-top:3px solid #4f8a6b}.v-observed{border-top:3px solid #a07a3c}
.limits{margin:12px 0;padding:8px 14px;border-left:4px solid #c9962b;border-radius:6px;background:#fffaf0}
.limits h2{margin:0 0 4px;font-size:1rem}
.missing,.unk-box{margin:8px 0;padding:8px 12px;border:1px dashed #9aa1ab;border-radius:8px;background:#f1f2f4;color:#4d545e}
.blockers{margin:4px 0;padding:6px 10px 6px 28px;border-left:4px solid #c25454;border-radius:6px;background:#fdf1f1}
.toc{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:12px}
.map{overflow-x:auto;padding:6px;border:1px solid var(--line);border-radius:8px;background:#fafbfc}
.map svg{display:block;max-width:100%;height:auto}
.map .en{font-size:14px;font-weight:600;fill:#1d2433}.map .ep{font-size:11px}
.tbl{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.88rem}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{background:#f0f2f5;font-weight:600}
table.wide{min-width:960px}
.quote{margin:6px 0;padding:4px 8px;border-left:3px solid #9aa1ab;border-radius:4px;background:#f6f7f9}
.quote figcaption{color:var(--muted);font-size:.78rem}
.q-ok{border-left-color:#3d8c5a}.q-bad{border-left-color:#c25454}
summary{color:#1f4f8a;cursor:pointer}
details.ev{margin-top:6px}
details.evidence{margin:8px 0;padding:8px 12px;border:1px solid var(--line);border-radius:8px}
.where{margin:8px 0;padding:8px 10px;border-radius:6px;background:#f3f6fa}
.plan{margin:8px 0;padding:6px 10px;border-left:4px solid #4f8a6b;border-radius:6px;background:#eef6f0}
.num{font-size:1.4rem;font-weight:700}
.hero{border-top:5px solid #315f89!important;background:linear-gradient(120deg,#fff,#eef4fa)!important}
.overview{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:16px 0}
.stat{padding:12px;border:1px solid #c7d5e4;border-radius:8px;background:#fff}
.stat .count{display:block;color:#264f77;font-size:1.8rem}.stat p{margin:0;color:var(--muted);font-size:.86rem}
.technical{margin:10px 0}.technical>summary{padding:6px 0}
.signal{padding:10px 14px;border-left:4px solid #315f89;background:#edf3f9;border-radius:6px}
@media(max-width:640px){.overview{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:640px){main{padding:8px}header.top,section{padding:12px}.kv{grid-template-columns:minmax(0,1fr)}.kv dt{margin-top:6px}}
"""


def build_html(bundle):
    """Compact offline passport with a fixed CSP-hashed interface script."""
    from .compact_report import build_html as compact_html
    return compact_html(bundle)


def build_markdown(bundle):
    """Markdown passport with the same values, labels and unknowns as the HTML."""
    run = _Run(bundle)
    renderers = {"map": _mmap, "coverage": _mcoverage, "instrumentation": _mplan, "metrics": _mmetrics}
    lines = _mheader(run)
    for key, _, title in _SECTIONS:
        lines += _msection(run, key, title, renderers[key])
    lines += _minsights(run) + _mevidence(run)
    return "\n".join(lines) + "\n"


# Shared view of the bundle


class _Run:
    """Read-only view of a bundle with locally generated fragment anchors."""

    def __init__(self, bundle):
        bundle = bundle if isinstance(bundle, dict) else {}
        self.bundle = bundle
        self.manifest = _mapping(bundle.get("manifest"))
        self.sections = {key: _mapping(bundle.get(name)) for key, name, _ in _SECTIONS}
        self.scope = _mapping(bundle.get("scope"))
        self.review_flags=_records(bundle.get("review_flags")) or []
        self.insights = bundle.get("insights") if isinstance(bundle.get("insights"), list) else None
        self.evidence = _records(bundle.get("evidence"))
        self.run_id = _known(_get(self.manifest, "run_id"))
        self.stages = _records(_get(self.sections["map"], "stages")) or []
        self.facts = _records(_get(self.sections["coverage"], "facts")) or []
        self.metrics = _records(_get(self.sections["metrics"], "metrics")) or []
        self.evidence_index = _index(self.evidence)
        self.stage_index = _index(self.stages)
        self.fact_index = _index(self.facts)
        self.metric_index = _index(self.metrics)
        self.card_index = _index(_records(_get(self.sections["instrumentation"], "cards")) or [])
        self.has_telemetry = any(_known(item.get("kind")) == "telemetry" for item in self.evidence or [])

    def stage_state(self, key):
        return _mapping(_get(_get(self.manifest, "stages"), key)) or {}

    def measured(self, ids):
        """Whether a reference resolves to text- and meaning-verified telemetry in this bundle."""
        for ref in _items(ids):
            _, item = self.evidence_index.get(_known(ref), (None, {}))
            if (_known(item.get("kind")) == "telemetry" and _known(item.get("text_validation")) == "verified"
                    and _known(item.get("semantic_validation")) == "verified"):
                return True
        return False

    def unmeasured_observation(self, key, assessment):
        return key == "observed" and _known(assessment.get("state")) == "observed" and not self.measured(assessment.get("evidence_ids"))


def _mapping(value):
    return value if isinstance(value, dict) else None


def _records(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else None


def _get(mapping, key):
    return mapping.get(key) if isinstance(mapping, dict) else None


def _items(value):
    return value if isinstance(value, list) else []


def _index(items):
    """First record per known ID with its 1-based position, used for local anchors."""
    index = {}
    for number, item in enumerate(items or [], 1):
        key = _known(item.get("id"))
        if key is not None:
            index.setdefault(key, (number, item))
    return index


def _scalar(value):
    """Display text shared by both formats; None means the value is unknown."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, str):
        return _CONTROL.sub(_REPLACEMENT,value)
    if isinstance(value, (int, float)):
        return str(value)
    return _CONTROL.sub(_REPLACEMENT,json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _known(value):
    text = _scalar(value)
    return text if text is not None and text.strip() else None


def _state(value, labels=None):
    key = _known(value)
    if key is None:
        return _UNKNOWN, "unknown"
    label, tone = _STATES.get(key, (key, "unknown"))
    return (labels or {}).get(key, label), tone


def _short(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _lines(anchor):
    start, end = _known(_get(anchor, "start_line")), _known(_get(anchor, "end_line"))
    return None if start is None and end is None else f"{start or '?'}–{end or '?'}"


def _stage_title(run, value):
    _, stage = run.stage_index.get(_known(value), (None, {}))
    return _known(stage.get("name")) or _known(value) or _UNKNOWN


def _mapped(run, stage):
    key = _known(stage.get("id"))
    mappings = _records(_get(run.sections["map"], "event_mappings")) or []
    return 0 if key is None else sum(_known(item.get("stage_id")) == key for item in mappings)


def _opportunity_rows(opportunities):
    rows = [("количество", opportunities.get("count"))] if opportunities.get("count") is not None else []
    return rows + [("окно", opportunities.get("window")), ("область полна", opportunities.get("scope_complete"))]


def _limitations(run):
    origin, mode = _known(_get(run.manifest, "data_origin")), _known(_get(run.manifest, "model_mode"))
    items = []
    if origin == "synthetic":
        items.append("Синтетические входные данные: это демонстрация работы инструмента, а не проверка реального приложения.")
    elif origin != "real":
        items.append("Происхождение данных неизвестно: результаты нельзя считать проверкой реального приложения.")
    if mode == "fake":
        items.append("Семантические суждения получены от сценарной (fake) модели, а не от живой LLM.")
    elif mode == "live":
        items.append("Семантические суждения сделаны живой LLM и требуют проверки человеком.")
    else:
        items.append("Режим модели неизвестен.")
    if run.evidence is None:
        items.append("Реестр доказательств не передан: наличие телеметрии неизвестно, наблюдаемое поведение не подтверждено.")
    elif not run.has_telemetry and (_get(run.scope, "telemetry") is not None or _get(run.scope, "telemetry_profile") is not None):
        items.append("Логи предоставлены и профилированы; их бизнес-смысл, полнота и пригодность для расчётов ещё не проверены. "
                     "Наличие ключа события в записях не подтверждает исполнение всего этапа.")
    elif not run.has_telemetry:
        items.append("Телеметрия в пакете отсутствует (анализ только по коду и документам): "
                     "столбец «Наблюдается» и значения метрик не измерялись.")
    if run.scope is None:
        items.append(_NO_SCOPE)
    return items + [
        _QUOTE_LIMIT,
        "Количество вызовов логгера в коде не считается покрытием процесса: "
        "покрытие относится только к реестру требуемых фактов.",
        "Серый цвет и «неизвестно» означают отсутствие данных — не ноль и не успех.",
        "Отчёт только отображает артефакты запуска: он не проверяет источники, не рассчитывает оценки и не добавляет выводов.",
    ]


# HTML


def _h(value, unknown=_UNKNOWN):
    text = _known(value)
    return html.escape(text) if text is not None else f'<span class="unk">{unknown}</span>'


def _hcode(value):
    text = _known(value)
    return f"<code>{html.escape(text)}</code>" if text is not None else _h(None)


def _hlink(value, index, prefix):
    number, _ = index.get(_known(value), (None, None))
    return f'<a href="#{prefix}-{number}">{_hcode(value)}</a>' if number else _hcode(value)


def _hlinker(index, prefix):
    return lambda value: _hlink(value, index, prefix)


def _hmetric_link(run, identifier):
    number, metric = run.metric_index.get(_known(identifier), (None, {}))
    if number is None:
        return _h(identifier)
    title = (f"Определение метрики №{number} ещё не подготовлено" if _metric_pending(metric)
             else _metric_title(metric))
    return f'<a href="#m-{number}">{_h(title or _known(identifier))}</a>'


def _hdependency(run, identifier):
    number, fact = run.fact_index.get(_known(identifier), (None, {}))
    if number is not None:
        return f'<a href="#fact-{number}">{_h(fact.get("meaning") or _known(identifier))}</a>'
    number, card = run.card_index.get(_known(identifier), (None, {}))
    if number is not None:
        _, fact = run.fact_index.get(_known(card.get("fact_id")), (None, {}))
        return f'<a href="#card-{number}">{_h(fact.get("meaning") or card.get("semantics") or _known(identifier))}</a>'
    return _h(identifier)


def _hbadge(value, labels=None):
    label, tone = _state(value, labels)
    raw = _known(value)
    title = f' title="{html.escape(raw)}"' if raw is not None else ""
    return f'<span class="b t-{tone}"{title}>{html.escape(label)}</span>'


def _hjoin(values, render=_h, empty="нет"):
    if not isinstance(values, list):
        return _h(None)
    if not values:
        return f'<span class="muted">{empty}</span>'
    return ", ".join(render(value) for value in values)


def _hdl(rows):
    return '<dl class="kv">' + "".join(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in rows) + "</dl>"


def _hmissing(text):
    return f'<p class="missing">{html.escape(text)}</p>'


def _hblockers(values):
    if not isinstance(values, list):
        return '<p class="unk">Блокеры неизвестны.</p>'
    if not values:
        return '<p class="muted">Блокеров нет.</p>'
    return '<ul class="blockers">' + "".join(f"<li>{_h(value)}</li>" for value in values) + "</ul>"


def _htable(headers, rows, wide=False):
    head = "".join(f"<th>{header}</th>" for header in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    table_class = ' class="wide"' if wide else ""
    return f'<div class="tbl"><table{table_class}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _hvalue(value, depth=0):
    """Generic plain data such as scope or insights; every key and value is escaped."""
    if isinstance(value, (dict, list)) and not value:
        return '<span class="muted">пусто</span>'
    if isinstance(value, dict) and depth < 4:
        return _hdl([(_h(key), _hvalue(item, depth + 1)) for key, item in value.items()])
    if isinstance(value, list) and depth < 4:
        return "<ul>" + "".join(f"<li>{_hvalue(item, depth + 1)}</li>" for item in value) + "</ul>"
    return _h(value)


def _hlocation(item):
    anchor = _mapping(item.get("source_anchor"))
    if anchor is None:
        return _hcode(item.get("locator"))
    return f'{_hcode(anchor.get("path"))} · {_hcode(anchor.get("symbol"))} · строки {_h(_lines(anchor))}'


def _hquote(item, limit=None):
    quote = _scalar(item.get("safe_quote"))
    if quote is None or not quote.strip():
        return '<p class="unk">Цитата отсутствует.</p>'
    caption, tone = _QUOTES.get(_known(item.get("text_validation")), _UNCHECKED_QUOTE)
    text = _short(quote, limit) if limit else quote
    return f'<figure class="quote q-{tone}"><figcaption>{caption}</figcaption><pre>{html.escape(text)}</pre></figure>'


def _hrefs(run, ids):
    """Evidence references expandable next to the result they support."""
    ids = _items(ids)
    if not ids:
        return ''
    rows = []
    for ref in ids:
        number, item = run.evidence_index.get(_known(ref), (None, None))
        if number is None:
            missing = ('<span class="b t-unknown">реестр не передан</span>' if run.evidence is None
                       else '<span class="b t-bad">нет в реестре доказательств</span>')
            rows.append(f"<li>{_hcode(ref)} {missing}</li>")
        else:
            rows.append(f'<li><a href="#ev-{number}">{_hcode(ref)}</a> {_hbadge(item.get("kind"))}'
                        f'{_hbadge(item.get("text_validation"), _TEXT)} {_hlocation(item)}{_hquote(item, 240)}</li>')
    return f'<details class="ev"><summary>Источники · {len(ids)}</summary><ul>{"".join(rows)}</ul></details>'


def _hheader(run):
    manifest = run.manifest
    provenance = _get(manifest, "provenance")
    parts = [
        '<header class="top hero" id="top">',
        '<p class="eyebrow">Паспорт наблюдаемости процесса · офлайн-отчёт</p>',
        f"<h1>{_h(_get(run.sections['map'], 'purpose'), 'Описание процесса пока не получено')}</h1>",
        f'<p>Запуск {_hcode(run.run_id)} · {_hbadge(_get(manifest, "data_origin"))} {_hbadge(_get(manifest, "model_mode"))}</p>',
    ]
    if manifest is None:
        parts.append(_hmissing(_NO_MANIFEST))
    counts = [(len(run.stages), "этапов в черновике", "map"), (len(run.facts), "кандидатов фактов", "coverage"),
              (len(_records(_get(run.sections["instrumentation"], "cards")) or []), "предложений по логам", "instrumentation"),
              (len(run.metrics), "определений метрик", "metrics")]
    parts.append('<div class="overview">' + ''.join(
        f'<a class="stat" href="#{key}"><span class="count">{count}</span><p>{label}</p></a>'
        for count, label, key in counts) + '</div>')
    parts.append('<p class="signal">Карта и рекомендации — черновик для проверки. Серое «неизвестно» показывает, '
                 'каких данных ещё не хватает. Детали и цитаты раскрываются рядом с результатами.</p>')
    parts.append('<details class="technical"><summary>Технические сведения о запуске</summary>' + _hdl([
        ("Происхождение данных", _hbadge(_get(manifest, "data_origin"))),
        ("Режим модели", _hbadge(_get(manifest, "model_mode"))),
        ("Версия инструмента", _h(_get(provenance, "tool_version"))),
        ("Версия Python", _h(_get(provenance, "python_version"))),
        ("Хеш конфигурации", _hcode(_get(provenance, "config_sha256"))),
        ("Схема манифеста", _h(_get(manifest, "schema_version"))),
    ]) + '</details>')
    limits = "".join(f"<li>{html.escape(text)}</li>" for text in _limitations(run))
    parts.append(f'<details class="limits"><summary>Ограничения и смысл статусов</summary><ul>{limits}</ul></details>')
    parts.append('<h2 id="scope">Область анализа</h2>')
    source_scope = _mapping(_get(run.scope, 'source')) or {}
    parts.append(_hdl([('Выбрано файлов', _h(source_scope.get('selected_files'))),
                       ('Неполный вход', _h(_get(run.scope, 'partial_input')))]))
    parts.append('<details class="technical"><summary>Файлы, лимиты и границы</summary>' +
                 (_hvalue(run.scope) if run.scope is not None else _hmissing(_NO_SCOPE)) + '</details>')
    profile = _mapping(_get(run.scope, 'telemetry_profile'))
    if profile is not None:
        parts.append(_hprofile(profile))
    inputs = _records(_get(manifest, "input_fingerprints"))
    if inputs is None:
        parts.append(f'<p class="unk">{_NO_INPUTS}</p>')
    else:
        rows = [(_hcode(item.get("path")), _hcode(item.get("sha256"))) for item in inputs]
        parts.append(f"<details><summary>Входные файлы: {len(inputs)}</summary>{_htable(('Путь', 'SHA-256'), rows)}</details>")
    tiles = []
    for key, _, title in _SECTIONS:
        state = run.stage_state(key)
        tiles.append(f'<div class="tile"><h3><a href="#{key}">{title}</a></h3>'
                     f'<p>{_hbadge(state.get("status"), _MANIFEST)}</p>{_hblockers(state.get("blockers"))}</div>')
    parts += [
        "<h2>Стадии запуска</h2>",
        f'<p class="note">{_STAGE_NOTE}</p>',
        f'<div class="tiles">{"".join(tiles)}</div>',
        '<nav class="toc">' + "".join(f'<a href="#{anchor}">{label}</a>' for anchor, label in _TOC) + "</nav>",
        "</header>",
    ]
    return "\n".join(parts)


def _hprofile(profile):
    labels = (("record_count", "Записей в файле"), ("identified_case_count", "Различных переданных case ID"),
              ("logical_event_count", "Различных переданных event ID"), ("duplicate_record_count", "Повторов event ID"),
              ("invalid_time_count", "Непригодных временных меток"), ("missing_case_count", "Записей без case ID"))
    return ('<details class="technical"><summary>Профиль логов: ' + _h(profile.get('record_count')) + ' записей</summary>'
            + _hdl([(label, _h(profile.get(key))) for key, label in labels])
            + '<p class="note">Это технический профиль файла. Case ID ещё не проверен как бизнес-экземпляр; '
              'повтор event ID не равен доказанной повторной доставке. Полнота поставки неизвестна.</p>'
            + _htable(('Ключ события', 'Записей'), [(_hcode(key), _h(value))
                      for key, value in (profile.get('event_counts') or {}).items()]) + '</details>')


def _hsection(run, key, title, body):
    artifact = run.sections[key]
    parts = [f'<section id="{key}"><h2>{title}</h2>']
    if artifact is None:
        parts.append(_hmissing(_NO_ARTIFACT))
    else:
        artifact_run = _hcode(artifact.get("run_id"))
        if run.run_id is not None and _known(artifact.get("run_id")) != run.run_id:
            artifact_run += ' <span class="b t-bad">не совпадает с манифестом</span>'
        parts += [
            '<details class="section-meta"><summary>Данные раздела</summary>' + _hdl([("Стадия в манифесте", _hbadge(run.stage_state(key).get("status"), _MANIFEST)), ("Статус артефакта", _hbadge(artifact.get("status"))), ("Запуск", artifact_run),
                  ("Доказательства артефакта", _hrefs(run, artifact.get("evidence_ids")))]) + "</details>",
            _hblockers(artifact.get("blockers")) if artifact.get("blockers") else "",
            body(run, artifact),
        ]
    parts.append("</section>")
    return "\n".join(parts)


def _hstage_ref(run, value):
    number, stage = run.stage_index.get(_known(value), (None, None))
    if number is None:
        return f'{_hcode(value)} <span class="b t-bad">этап не найден</span>'
    return f'<a href="#st-{number}">{_h(stage.get("name"), "без названия")}</a> {_hcode(value)}'


def _hmap(run, process_map):
    if _records(process_map.get("operations")):
        from .scenario_graph import map_body
        return map_body(run,process_map)
    transitions = _records(process_map.get("transitions")) or []
    mappings = _records(process_map.get("event_mappings")) or []
    unknowns = _items(process_map.get("unknowns"))
    parts = [
        _hdl([("Назначение", _h(process_map.get("purpose"))), ("Границы", _h(process_map.get("boundaries"))),
              ("Что считается случаем", _h(process_map.get("case_definition")))]),
        '<div class="map-layout"><div class="map-main">',
        "<h3>Схема этапов и переходов</h3>",
        _hsvg(run, transitions, unknowns) + _LEGEND if run.stages else _hmissing(_NO_STAGES),
        '<details><summary>Неизвестные ветви и вопросы</summary><h3 id="unknowns">Неизвестные ветви и пробелы</h3>',
    ]
    if unknowns:
        parts.append('<ul class="blockers">' + "".join(f"<li>{_h(item)}</li>" for item in unknowns) + "</ul>")
    else:
        parts.append(f'<p class="unk">{_NO_UNKNOWNS}</p>')
    parts.append('</details></div><aside class="stage-inspector"><div class="inspector-overview"><h3>О процессе</h3>' +
                 _hdl([("Границы",_h(process_map.get("boundaries"))), ("Единица учёта",_h(process_map.get("case_definition"))),
                       ("Этапов",str(len(run.stages))), ("Сопоставлений событий",str(len(mappings)))]) +
                 '<p class="note">Выберите этап на карте, чтобы посмотреть основания и логирование.</p></div>')
    if run.stages:
        parts.append('<div class="stages">' + "".join(_hstage(run, number, stage) for number, stage in enumerate(run.stages, 1)) + "</div>")
    else:
        parts.append(_hmissing(_NO_STAGES))
    parts.append('</aside></div><details class="drawer-list"><summary>Переходы и словарь событий</summary><h3>Переходы</h3>')
    rows = [(str(number), _hstage_ref(run, item.get("from_stage")), _hstage_ref(run, item.get("to_stage")),
             _h(item.get("condition"), "условие не указано"), _hbadge(item.get("state")), _hrefs(run, item.get("evidence_ids")))
            for number, item in enumerate(transitions, 1)]
    parts.append(_htable(("№", "Из", "В", "Условие", "Состояние", "Доказательства"), rows) if rows else _hmissing(_NO_TRANSITIONS))
    parts += ["<h3>Сопоставление событий с этапами</h3>", f'<p class="note">{_MAPPING_NOTE}</p>']
    rows = [(_hcode(item.get("event_key")), _h(item.get("business_action")), _hcode(item.get("operation")),
             _hstage_ref(run, item.get("stage_id")), _hbadge(item.get("lifecycle_role")), _h(item.get("occurrence_rule")),
             _hbadge(item.get("state")), _hrefs(run, item.get("evidence_ids"))) for item in mappings]
    headers = ("Событие", "Бизнес-действие", "Операция", "Этап", "Роль в жизненном цикле", "Правило вхождения", "Состояние", "Доказательства")
    parts.append(_htable(headers, rows, wide=True) if rows else _hmissing(_NO_MAPPINGS))
    parts.append("</details>")
    return "\n".join(parts)


def _hsvg(run, transitions, unknowns):
    from .compact_report import graph
    return graph(run, transitions, unknowns)


def _hstage(run, number, stage):
    views = []
    for key, title, _ in _VIEWS:
        assessment = _mapping(stage.get(key)) or {}
        extra = f'<p><span class="b t-bad">{_UNMEASURED}</span></p>' if run.unmeasured_observation(key, assessment) else ""
        if _known(assessment.get("decision_id")):
            extra += f'<p class="muted">Решение: {_hcode(assessment.get("decision_id"))}</p>'
        views.append(f'<div class="view v-{key}"><h4>{title}</h4>{_hbadge(assessment.get("state"))}{extra}'
                     f'<details><summary>Основание</summary><p>{_h(assessment.get("reason"))}</p>{_hrefs(run, assessment.get("evidence_ids"))}</details></div>')
    alternatives = "полны" if stage.get("alternatives_complete") is True else '<span class="unk">не подтверждены как полные</span>'
    facts=[(index,fact) for index,fact in enumerate(run.facts,1) if fact.get("stage_id")==stage.get("id")]
    logging='<h3>Логирование этапа</h3>'+''.join(f'<div class="inspector-fact"><a href="#fact-{index}">{_h(fact.get("meaning"))}</a><p>{_hbadge(fact.get("instrumentation"),_INSTRUMENTED)}</p></div>' for index,fact in facts)
    return (f'<article class="stage" id="st-{number}"><h4>{_h(stage.get("name"), "без названия")} {_hcode(stage.get("id"))}</h4>'
            + _hdl([("Роли", _hjoin(stage.get("roles"), empty="не указаны")),
                    ("Системы", _hjoin(stage.get("systems"), empty="не указаны")),
                    ("Альтернативные ветви", alternatives), ("Сопоставлено событий", str(_mapped(run, stage)))])
            + f'<details><summary>Состояние этапа</summary><div class="views">{"".join(views)}</div></details>' + logging + '</article>')


def _hsource_link(run, anchor):
    if not isinstance(anchor, dict) or not anchor.get("path"):
        return _h(None, "Место в коде не определено")
    label = _hcode(anchor.get("path")) + ":" + _h(_lines(anchor))
    source_sets = [("code", _records(_get(run.bundle.get("report_sources"), "excerpts")) or []),
                   ("ev", run.evidence or [])]
    for prefix, sources in source_sets:
        for number, source in enumerate(sources, 1):
            found = _mapping(source.get("source_anchor")) or {}
            if all(found.get(key) == anchor.get(key) for key in ("path", "start_line", "end_line")):
                return f'<a href="#{prefix}-{number}" class="code-link">{label}</a>'
    return label + '<div class="muted">Фрагмент кода не приложен к этому запуску</div>'


def _hreport_sources(run):
    sources = _records(_get(run.bundle.get("report_sources"), "excerpts")) or []
    if not sources:
        return ""
    items = []
    for number, source in enumerate(sources, 1):
        anchor = _mapping(source.get("source_anchor")) or {}
        items.append(f'<article id="code-{number}" class="source-excerpt"><h3>'
                     + _hcode(anchor.get("path")) + ":" + _h(_lines(anchor)) + '</h3>'
                     + '<p>' + _hcode(anchor.get("symbol")) + '</p><pre>' + _h(source.get("safe_quote"), "Пустой фрагмент") + '</pre>'
                     + '<details><summary>Версия источника</summary>'
                     + _hdl([("Снимок", _hcode(source.get("snapshot_id"))), ("Хеш фрагмента", _hcode(source.get("raw_span_hash")))])
                     + '</details></article>')
    return '<details class="source-excerpts"><summary>Код записей и предлагаемых изменений</summary>' + ''.join(items) + '</details>'


def _hexact_sites(run, atomic, sites):
    """Atomic map: each emit site with only its own accepted bindings; one key at two sites lends nothing."""
    bindings = {}
    for item in _records(atomic.get("event_mappings")) or []:
        bindings.setdefault((item.get("path"), item.get("start_line"), item.get("end_line"), item.get("event_key")),
                            []).append(item)
    found = []
    for site in sites:
        first, last = site.get("start_line", site.get("line")), site.get("end_line", site.get("line"))
        matches = bindings.get((site.get("path"), first, last, site.get("event_key")))
        if matches:
            found.append((site, matches))
    return found


def _hexact_row(run, site, matches):
    from .scenario_graph import _fact_link, _op_link
    operations = list(dict.fromkeys(item.get("operation_id") for item in matches if _known(item.get("operation_id"))))
    stages = list(dict.fromkeys(_known(item.get("stage_id")) for item in matches if _known(item.get("stage_id"))))
    facts = list(dict.fromkeys(fact for item in matches for fact in _items(item.get("fact_ids")) if _known(fact)))

    def stage_link(identifier):
        number, stage = run.stage_index.get(identifier, (None, {}))
        return f'<a href="#st-{number}">{_h(stage.get("name") or identifier)}</a>' if number else _h(identifier)
    moments = []
    for item in matches:
        role = _known(item.get("lifecycle_role"))
        label = _STATES.get(role, (role,))[0] if role and role != "unknown" else "роль не установлена"
        support = item.get("role_support")
        note = ""
        if support == "no_static_emit":
            note = " · прямая запись в журнал здесь не подтверждена"
        elif support == "emit_outside_operation_sources":
            note = (" · связь с результатом выбранного действия не подтверждена" if role == "result"
                    else " · связь записи с выбранным действием не подтверждена")
        elif role == "result" and support != "emit_within_operation_sources":
            note = " · основание связи с результатом выбранного действия не проверено"
        moments.append(f'<span class="b t-info">{_h(label)}</span>{note}<p>{_h(item.get("occurrence_rule"))}</p>')
    return ('<strong>' + ", ".join(_op_link(run, identifier) for identifier in operations) + '</strong>'
            + ('<p>' + _hjoin(stages, stage_link) + '</p>' if stages else '')
            + ('<p>Связанные факты: ' + ", ".join(_fact_link(run, fact) for fact in facts) + '</p>' if facts else ''),
            _hcode(site.get("event_key")) + ('<details><summary>Данные в записи</summary>'
                                             + _hjoin(site.get("fields"), _hcode, empty="Именованные поля не найдены")
                                             + '</details>'),
            "".join(moments), _hsource_link(run, site))


def _hcoverage(run, coverage):
    inventory = _mapping(run.bundle.get("logging_inventory")) or {}
    sites = _records(inventory.get("sites")) or []
    atomic = _mapping(run.bundle.get("atomic_map"))
    if atomic is not None:
        selected = _hexact_sites(run, atomic, sites)
        parts = ['<h3>Существующие записи, связанные с процессом</h3>',
                 '<p class="note">Запись связана с действием по точному месту вызова; одинаковый ключ в другом месте '
                 'не переносит её смысл.</p>',
                 '<details class="logging-moment-help"><summary>Как читать момент записи</summary>'
                 + _hdl([(_STATES[role][0].capitalize() if role != "unknown" else "Роль не установлена", explanation)
                         for role, explanation in (
                     ("request", "Обращение к системе или намерение выполнить действие."),
                     ("start", "Начало выполнения действия."),
                     ("result", "Локальный итог действия, например ответ сервиса. Финансовый или иной конечный "
                      "бизнес-результат требует своего основания."),
                     ("error", "Ошибка при выполнении действия."),
                     ("refusal", "Решение отклонить действие по условию."),
                     ("retry", "Новая повторная попытка выполнения."),
                     ("duplicate", "Запрос или сообщение распознаны как повтор уже прошедшего обработку события."),
                     ("no_op", "Обработка без изменения состояния."),
                     ("ui", "Действие в пользовательском интерфейсе."),
                     ("unknown", "Момент записи относительно действия ещё не определён."),
                 )])
                 + '<p>«Одна запись на вызов» — предложенное правило группировки записей в одно выполнение, '
                 'не измеренное количество выполнений. Таблица описывает места записи в исходном коде.</p></details>']
        if not selected:
            parts.append('<p class="muted">В этом запуске не сопоставлены существующие записи и действия процесса.</p>')
        else:
            parts.append('<div class="logging-coverage">' + _htable(
                ("Какое действие помогает увидеть", "Что записывается", "В какой момент", "Код записи"),
                [_hexact_row(run, site, matches) for site, matches in selected]) + '</div>')
        unresolved = len(sites) - len(selected)
        if unresolved:
            parts.append(f'<p><a href="#logging-registry">Записи без сопоставления с действиями: {unresolved}</a></p>')
        return "\n".join(parts)
    mappings = _records(_get(run.sections["map"], "event_mappings")) or []
    meanings = {}
    for item in mappings:
        key = _known(item.get("event_key"))
        if key:
            meanings.setdefault(key, []).append(item)
    for fact in run.facts:
        for key in _items(fact.get("event_keys")):
            key = _known(key)
            if key and key not in meanings:
                meanings[key] = [{"business_action": fact.get("meaning"), "stage_id": fact.get("stage_id")}]
    selected = [(site, meanings[_known(site.get("event_key"))]) for site in sites if _known(site.get("event_key")) in meanings]
    parts = ['<h3>Существующие записи, связанные с процессом</h3>']
    if not selected:
        parts.append('<p class="muted">В этом запуске не сопоставлены существующие записи и действия процесса.</p>')
    else:
        rows = []
        for site, matches in selected:
            purposes = list(dict.fromkeys(_known(item.get("business_action")) for item in matches if _known(item.get("business_action"))))
            stages = list(dict.fromkeys(_known(item.get("stage_id")) for item in matches if _known(item.get("stage_id"))))
            def stage_link(identifier):
                number, stage = run.stage_index.get(identifier, (None, {}))
                return f'<a href="#st-{number}">{_h(stage.get("name") or identifier)}</a>' if number else _h(identifier)
            roles = list(dict.fromkeys(_known(item.get("lifecycle_role")) for item in matches if _known(item.get("lifecycle_role"))))
            rules = list(dict.fromkeys(_known(item.get("occurrence_rule")) for item in matches if _known(item.get("occurrence_rule"))))
            lifecycle = _hjoin(roles, lambda role: _hbadge(role)) if roles else ''
            rows.append((
                '<strong>' + _hjoin(purposes) + '</strong>' + ('<p>' + _hjoin(stages, stage_link) + '</p>' if stages else ''),
                _hcode(site.get("event_key")) + ('<details><summary>Данные в записи</summary>' + _hjoin(site.get("fields"), _hcode, empty="Именованные поля не найдены") + '</details>'),
                lifecycle + ('<p>' + _hjoin(rules) + '</p>' if rules else ''),
                _hsource_link(run, site),
            ))
        parts.append('<div class="logging-coverage">' + _htable(
            ("Какое действие помогает увидеть", "Что записывается", "В какой момент", "Код записи"), rows) + '</div>')
    unresolved = len(sites) - len(selected)
    if unresolved:
        parts.append(f'<p><a href="#logging-registry">Записи без сопоставления с действиями: {unresolved}</a></p>')
    return "\n".join(parts)


def _hfact_targets(run):
    if not run.facts:
        return ''
    opened = ' open' if (_mapping(run.bundle.get("instrumentation_accounting")) or {}).get("unaddressed_fact_ids") else ''
    return (f'<details class="fact-targets"{opened}><summary>Для каких действий нужны записи</summary><ul>'
            + ''.join(f'<li id="fact-{number}"><strong>{_h(fact.get("meaning"))}</strong><p>{_h(fact.get("rationale"))}</p>'
                      + '<details><summary>Источники и идентификатор</summary>' + _hcode(fact.get("id"))
                      + _hrefs(run, fact.get("evidence_ids")) + '</details></li>' for number, fact in enumerate(run.facts, 1))
            + '</ul></details>')


_LOG_ACTIONS = {
    "map_existing": "Использовать найденную запись", "join": "Связать существующие данные",
    "enrich": "Дополнить данные при обработке", "add_field": "Добавить данные в запись",
    "add_event": "Добавить запись в лог", "move_emit": "Изменить момент записи",
    "fix_delivery": "Исправить передачу записи",
}


def _hplan_pending(run):
    record = _mapping(run.bundle.get("instrumentation_accounting")) or {}
    missing = _items(record.get("unaddressed_fact_ids"))
    rows = []
    for key in missing:
        number, fact = run.fact_index.get(_known(key), (None, None))
        if number is not None:
            rows.append(f'<li><a href="#fact-{number}">{_h(fact.get("meaning"))}</a></li>')
    if not rows:
        return ''
    return ('<aside class="note"><h3>Доработки по этим фактам ещё не определены</h3>'
            '<p>Для них не установлена подходящая запись в коде и не предложено изменение. '
            'Это открытые вопросы; отсутствие записи не доказывает ошибку операции.</p><ul>'
            + ''.join(rows) + '</ul></aside>')


def _hplan(run, plan):
    cards = _records(plan.get("cards")) or []
    if not cards:
        message = (_NO_CARDS if _known(run.stage_state("instrumentation").get("status")) == "completed"
                   else "Предложения по доработкам ещё не подготовлены.")
        return _hplan_pending(run) + f'<p class="muted">{message}</p>' + _hfact_targets(run)
    return (_hplan_pending(run) + '<div class="cards logging-cards">'
            + "".join(_hcard(run, number, card) for number, card in enumerate(cards, 1)) + "</div>" + _hfact_targets(run))


_GAP_STATES = {"found": "существующая запись найдена в коде; доработка уточняет её",
               "missing": "подходящая запись в коде не найдена"}


def _hcard_gap(fact_number, fact):
    if not fact:
        return _h(None, "факт не найден")
    title = _h(fact.get("meaning") or fact.get("id"))
    link = f'<a href="#fact-{fact_number}">{title}</a>' if fact_number else title
    state = _GAP_STATES.get(_known(fact.get("instrumentation")), "подходящая запись не установлена")
    return f'{link} — {html.escape(state)}'


def _hcard_operations(run, card, fact):
    """Exact owners through the change's fact; otherwise labelled candidates, never a single false owner."""
    from .scenario_graph import _inspection, _op_link
    link = _inspection(run)["links"].get(_known(card.get("id")))
    if link and link["operation_ids"]:
        return ", ".join(_op_link(run, identifier) for identifier in link["operation_ids"])
    stage = _stage_title(run, fact.get("stage_id")) if fact and _known(fact.get("stage_id")) else None
    text = ('<span class="unk">Операция не установлена: доработка остаётся на уровне факта'
            + (f' и этапа «{html.escape(stage)}»' if stage else '') + '</span>')
    candidates = (link or {}).get("candidates") or []
    if candidates:
        text += ('<p>Кандидаты по пересечению строк кода или ключу события (принадлежность не установлена): '
                 + ", ".join(_op_link(run, item["operation_id"]) for item in candidates) + '</p>')
    return text


def _hcard(run, number, card):
    anchor = _mapping(card.get("source_anchor"))
    fact_number, fact = run.fact_index.get(_known(card.get("fact_id")), (None, {}))
    title = fact.get("meaning") or card.get("semantics")
    if _known(card.get("location")) == "anchored" and anchor is not None:
        where = ('<div class="where"><h4>Место в коде</h4>' + _hsource_link(run, anchor) + '<details><summary>Сведения об участке</summary>'
                 + _hdl([("Путь", _hcode(anchor.get("path"))), ("Символ", _hcode(anchor.get("symbol"))),
                         ("Строки", _h(_lines(anchor))), ("Снимок", _hcode(anchor.get("snapshot_id")))]) + "</details></div>")
    else:
        where = f'<div class="unk-box"><h4>Место в коде не определено</h4><p>{_UNRESOLVED}</p></div>'
    fields = card.get("fields")
    if isinstance(fields, dict) and fields:
        field_html = _htable(("Для чего нужны данные", "Имя в записи"), [(_h(value), _hcode(key)) for key, value in fields.items()])
    else:
        field_html = '<span class="muted">Состав данных не указан.</span>' if isinstance(fields, dict) else _h(None)
    dependencies = _items(card.get("dependencies"))
    metrics = _items(card.get("metric_ids"))
    technical = _hdl([("Предложение", _hcode(card.get("id"))), ("Факт", _hlink(card.get("fact_id"), run.fact_index, "fact")),
                      ("Идентификаторы зависимостей", _hjoin(dependencies, _hcode)),
                      ("Идентификаторы метрик", _hjoin(metrics, _hcode))])
    rows = [("Что должна означать запись", _h(card.get("semantics"))), ("Зачем это нужно", _h(card.get("rationale"))),
            ("Какой пробел закрывает", _hcard_gap(fact_number, fact)), ("Для каких операций", _hcard_operations(run, card, fact)),
            ("Какие данные нужны", field_html), ("Когда должна появиться запись", _h(card.get("transaction_timing"), "Момент записи не указан"))]
    if dependencies:
        rows.append(("Что нужно сделать раньше", _hjoin(dependencies, lambda value: _hdependency(run, value))))
    if metrics:
        rows.append(("Что сможем измерять", _hjoin(metrics, lambda value: _hmetric_link(run, value))))
    flags = [flag.get("message") for flag in run.review_flags if flag.get("card_id") == card.get("id")]
    return (f'<article class="card logging-card" id="card-{number}"><h3>{_h(title, "Предложение по логированию")}</h3>'
            + _hbadge(card.get("action"), _LOG_ACTIONS)
            + (f'<p><a href="#fact-{fact_number}">К оценке текущих записей</a></p>' if fact_number else '')
            + _hdl(rows) + where
            + f'<div class="plan"><h4>Как проверить результат</h4><p>{_h(card.get("validation_plan"))}</p></div>'
            + ('<details><summary>Проверить перед доработкой</summary>' + _hblockers(flags) + '</details>' if flags else '')
            + _hrefs(run, card.get("evidence_ids"))
            + '<details class="technical"><summary>Служебные идентификаторы</summary>' + technical + '</details></article>')


def _hmetrics(run, catalog):
    if not run.metrics:
        return f'<p class="muted">{_NO_METRICS}</p>'
    numbered = list(enumerate(run.metrics, 1))
    pending = [(number, metric) for number, metric in numbered if _metric_pending(metric)]
    defined = [(number, metric) for number, metric in numbered if not _metric_pending(metric)]
    return ((_hmetric_status(run, pending) if pending else '')
            + ('<div class="cards">' + "".join(_hmetric(run, number, metric) for number, metric in defined) + "</div>"
               if defined else ''))


_METRIC_PLACEHOLDER = "Referenced by proposed facts or instrumentation; definition not established"
# Exact host messages shown in Russian; any other text stays verbatim under technical details.
_HOST_MESSAGES = {
    "metrics stage not run: instrumentation stage did not complete":
        "Стадия метрик не запускалась: доработки логирования не завершены.",
}
_METRIC_STAGE = {"not_started": "не запускалась", "incomplete": "не завершена", "blocked": "заблокирована",
                 "completed": "завершена"}


def _metric_title(metric):
    purpose = _known(metric.get("purpose"))
    if purpose == _METRIC_PLACEHOLDER:
        return "Метрика " + str(metric.get("id", ""))
    return purpose


def _metric_pending(metric):
    """Host placeholder: facts reference the metric, but no definition was produced."""
    return _known(metric.get("purpose")) == _METRIC_PLACEHOLDER and _known(metric.get("formula_text")) is None


def _metric_stage(run):
    """Russian stage status, translated host messages and the remaining exact messages."""
    state = run.stage_state("metrics")
    status = _METRIC_STAGE.get(_known(state.get("status")), "состояние неизвестно")
    shown, technical = [], []
    for value in _items(state.get("blockers")):
        text = _known(value)
        if text in _HOST_MESSAGES:
            shown.append(_HOST_MESSAGES[text])
        elif text is not None:
            technical.append(text)
    return status, shown, technical


def _hmetric_status(run, pending):
    """One status block instead of a card per undefined metric; formulas are never derived from names."""
    status, shown, technical = _metric_stage(run)
    items = ''.join(f'<li id="m-{number}"><strong>{_h(_metric_title(metric))}</strong>'
                    + (_metric_dependencies(run, metric) or '<p class="muted">Связанные факты не указаны</p>') + '</li>'
                    for number, metric in pending)
    rows = [("Стадия метрик", _hcode(run.stage_state("metrics").get("status")))]
    if technical:
        rows.append(("Причины", "<ul>" + "".join(f"<li>{_h(text)}</li>" for text in technical) + "</ul>"))
    for _, metric in pending:
        readiness = _mapping(metric.get("readiness")) or {}
        rows.append((_hcode(metric.get("id")), _hbadge(readiness.get("primary")) + _hjoin(readiness.get("blockers"))))
    return ('<div class="metric-status"><h3>Определения метрик ещё не сформированы</h3>'
            f'<p>Стадия метрик: {status}.</p>' + ''.join(f'<p>{html.escape(text)}</p>' for text in shown)
            + '<p class="note">Формулы, единицы счёта и состав случаев не выводятся из названий метрик. '
              'Ниже перечислены метрики, на которые ссылаются факты и доработки, и данные, которые для них понадобятся.</p>'
            + '<ul class="metric-pending">' + items + '</ul>'
            + '<details class="technical"><summary>Технические сведения</summary>' + _hdl(rows) + '</details></div>')


def _metric_dependencies(run, metric):
    """Human descriptions of required facts; stable IDs stay in the JSON."""
    rows=[]
    for identifier in _items(metric.get("dependencies")):
        number,fact=run.fact_index.get(_known(identifier),(None,None))
        if number is None:
            rows.append("<li>"+_hcode(identifier)+"</li>")
            continue
        keys=_hjoin(fact.get("event_keys"),_hcode) if fact.get("event_keys") else ''
        rows.append(f'<li><a href="#fact-{number}">'+_h(fact.get("meaning"),str(identifier))+'</a>'
                    + ('<p>'+keys+'</p>' if keys else '')+'</li>')
    return '<h4>Что нужно записывать</h4><ul class="metric-inputs">'+''.join(rows)+'</ul>' if rows else ''


def _hmetric(run, number, metric):
    formula=_known(metric.get("formula_text"))
    formula_html=('<pre class="metric-formula">'+html.escape(formula)+'</pre>' if formula is not None
                  else '<p class="muted">Формула не сформирована</p>')
    rules=[(title,_h(metric.get(key))) for key,title in (
        ('counting_unit','Единица счёта'),('cohort','Какие случаи учитываются'),
        ('time_semantics','Начало и конец'),('open_case_policy','Незавершённые случаи'),
        ('late_data_policy','Поздние события')) if _known(metric.get(key)) is not None]
    details='<details class="metric-rules"><summary>Правила расчёта</summary>'+_hdl(rules)+'</details>' if rules else ''
    return (f'<article class="metric" id="m-{number}"><h3>'+_h(_metric_title(metric))+'</h3>'
            +'<p class="metric-formula-label">Формула</p>'+formula_html
            +_metric_dependencies(run,metric)+details+'</article>')


def _hinsights(run):
    parts = ['<section id="insights"><h2>Выводы</h2>']
    if run.insights is None:
        parts.append(_hmissing(_NO_INSIGHTS))
    elif not run.insights:
        parts.append('<p class="muted">Список выводов пуст.</p>')
    else:
        parts.append(f'<p class="note">{_INSIGHT_NOTE}</p><div class="cards">')
        for number, insight in enumerate(run.insights, 1):
            fields = {key: value for key, value in insight.items() if key != "evidence_ids"} if isinstance(insight, dict) else insight
            refs = _hrefs(run, insight.get("evidence_ids")) if isinstance(insight, dict) and "evidence_ids" in insight else ""
            parts.append(f'<article class="insight"><h4>Вывод {number}</h4>{_hvalue(fields)}{refs}</article>')
        parts.append("</div>")
    parts.append("</section>")
    return "\n".join(parts)


def _hevidence(run):
    parts = ['<section id="evidence"><h2>Источники</h2>']
    if run.evidence is None:
        parts.append(_hmissing(_NO_EVIDENCE))
    elif not run.evidence:
        parts.append('<p class="muted">Реестр доказательств пуст.</p>')
    for number, item in enumerate(run.evidence or [], 1):
        anchor = _mapping(item.get("source_anchor"))
        rows = [("Снимок", _hcode(item.get("snapshot_id"))), ("Локатор", _hcode(item.get("locator")))]
        if anchor is not None:
            rows += [("Путь", _hcode(anchor.get("path"))), ("Символ", _hcode(anchor.get("symbol"))), ("Строки", _h(_lines(anchor)))]
        rows += [("Хеш фрагмента", _hcode(item.get("raw_span_hash"))),
                 ("Проверка текста", _hbadge(item.get("text_validation"), _TEXT)),
                 ("Проверка смысла", _hbadge(item.get("semantic_validation"), _MEANING)),
                 ("Статус утверждения", _hbadge(item.get("claim_state")))]
        parts.append(f'<details class="evidence" id="ev-{number}"><summary>{_hcode(item.get("id"))} {_hbadge(item.get("kind"))}'
                     f'{_hlocation(item)}</summary>'
                     f"{_hquote(item)}<details><summary>Сведения об источнике</summary>{_hdl(rows)}</details></details>")
    parts.append("</section>")
    return "\n".join(parts)


# Markdown


def _m(value, unknown=_UNKNOWN):
    text = _known(value)
    if text is None:
        return f"_{unknown}_"
    return html.escape(" ".join(_MD_SPECIAL.sub(r"\\\1", text).split()), quote=False)


def _mbadge(value, labels=None):
    label, tone = _state(value, labels)
    return f"_{_m(label)}_" if tone == "unknown" else _m(label)


def _mjoin(values, empty="нет"):
    if not isinstance(values, list):
        return _m(None)
    return ", ".join(_m(value) for value in values) if values else f"_{empty}_"


def _mrefs(run, ids):
    ids = _items(ids)
    if not ids:
        return "_нет_"
    marks = []
    for ref in ids:
        if _known(ref) in run.evidence_index:
            marks.append(_m(ref))
        else:
            marks.append(_m(ref) + (" (реестр не передан)" if run.evidence is None else " (нет в реестре доказательств)"))
    return ", ".join(marks)


def _mblockers(values, label="Блокеры", indent=""):
    if not isinstance(values, list):
        return [f"{indent}- {label}: _неизвестны_"]
    if not values:
        return [f"{indent}- {label}: нет"]
    return [f"{indent}- {label}:"] + [f"{indent}  - {_m(value)}" for value in values]


def _mtable(headers, rows):
    return (["| " + " | ".join(headers) + " |", "|" + " --- |" * len(headers)]
            + ["| " + " | ".join(row) + " |" for row in rows])


def _mplain(value):
    return "_пусто_" if isinstance(value, (dict, list)) and not value else _m(value)


def _mvalue(value, indent="", depth=0):
    """Generic plain data as nested bullets, mirroring _hvalue."""
    if not isinstance(value, (dict, list)) or not value or depth >= 4:
        return [f"{indent}- {_mplain(value)}"]
    pairs = value.items() if isinstance(value, dict) else ((None, item) for item in value)
    lines = []
    for key, item in pairs:
        prefix = f"{indent}- {_m(key)}:" if isinstance(value, dict) else f"{indent}-"
        if isinstance(item, (dict, list)) and item and depth + 1 < 4:
            lines.append(prefix)
            lines += _mvalue(item, indent + "  ", depth + 1)
        else:
            lines.append(f"{prefix} {_mplain(item)}")
    return lines


def _mquote(item):
    quote = _scalar(item.get("safe_quote"))
    if quote is None or not quote.strip():
        return ["", "_Цитата отсутствует._"]
    caption, _ = _QUOTES.get(_known(item.get("text_validation")), _UNCHECKED_QUOTE)
    longest = max((len(run) for run in _BACKTICKS.findall(quote)), default=0)
    fence = "`" * max(3, longest + 1)
    return ["", f"{caption}:", "", fence + "text", quote, fence]


def _mheader(run):
    manifest = run.manifest
    provenance = _get(manifest, "provenance")
    lines = ["# Паспорт наблюдаемости процесса", "", f"Запуск: {_m(run.run_id)}", ""]
    if manifest is None:
        lines += [f"> {_NO_MANIFEST}", ""]
    lines += [
        f"- Происхождение данных: {_mbadge(_get(manifest, 'data_origin'))}",
        f"- Режим модели: {_mbadge(_get(manifest, 'model_mode'))}",
        f"- Версия инструмента: {_m(_get(provenance, 'tool_version'))}",
        f"- Версия Python: {_m(_get(provenance, 'python_version'))}",
        f"- Хеш конфигурации: {_m(_get(provenance, 'config_sha256'))}",
        f"- Схема манифеста: {_m(_get(manifest, 'schema_version'))}",
    ]
    lines += ["", "## Область анализа", ""]
    lines += _mvalue(run.scope) if run.scope is not None else [f"> {_NO_SCOPE}"]
    lines += ["", "### Входные файлы", ""]
    inputs = _records(_get(manifest, "input_fingerprints"))
    if inputs is None:
        lines.append(f"_{_NO_INPUTS}_")
    else:
        lines += _mtable(("Путь", "SHA-256"), [(_m(item.get("path")), _m(item.get("sha256"))) for item in inputs])
    lines += ["", "## Стадии запуска", "", f"_{_STAGE_NOTE}_", ""]
    for key, _, title in _SECTIONS:
        state = run.stage_state(key)
        lines.append(f"- {title}: {_mbadge(state.get('status'), _MANIFEST)}")
        lines += _mblockers(state.get("blockers"), indent="  ")
    return lines


def _msection(run, key, title, body):
    artifact = run.sections[key]
    lines = ["", f"## {title}", "", f"- Стадия в манифесте: {_mbadge(run.stage_state(key).get('status'), _MANIFEST)}"]
    if artifact is None:
        return lines + ["", f"> {_NO_ARTIFACT}"]
    artifact_run = _m(artifact.get("run_id"))
    if run.run_id is not None and _known(artifact.get("run_id")) != run.run_id:
        artifact_run += " (не совпадает с манифестом)"
    lines += [f"- Статус артефакта: {_mbadge(artifact.get('status'))}", f"- Запуск: {artifact_run}",
              f"- Доказательства артефакта: {_mrefs(run, artifact.get('evidence_ids'))}"]
    return lines + _mblockers(artifact.get("blockers"), "Блокеры артефакта") + body(run, artifact)


def _mstage_ref(run, value):
    if _known(value) not in run.stage_index:
        return f"{_m(value)} (этап не найден)"
    return f"{_m(_stage_title(run, value))} ({_m(value)})"


def _mmap(run, process_map):
    transitions = _records(process_map.get("transitions")) or []
    mappings = _records(process_map.get("event_mappings")) or []
    unknowns = _items(process_map.get("unknowns"))
    lines = [f"- Назначение: {_m(process_map.get('purpose'))}", f"- Границы: {_m(process_map.get('boundaries'))}",
             f"- Что считается случаем: {_m(process_map.get('case_definition'))}",
             "", "### Неизвестные ветви и пробелы", ""]
    lines += [f"- {_m(item)}" for item in unknowns] or [f"_{_NO_UNKNOWNS}_"]
    lines += ["", "### Этапы: требуется, реализовано, наблюдается", ""]
    if not run.stages:
        lines.append(f"> {_NO_STAGES}")
    for number, stage in enumerate(run.stages, 1):
        alternatives = "полны" if stage.get("alternatives_complete") is True else "_не подтверждены как полные_"
        lines += [f"#### {number}. {_m(stage.get('name'), 'без названия')} ({_m(stage.get('id'))})", "",
                  f"- Роли: {_mjoin(stage.get('roles'), 'не указаны')}; системы: {_mjoin(stage.get('systems'), 'не указаны')}; "
                  f"альтернативные ветви: {alternatives}; сопоставлено событий: {_mapped(run, stage)}"]
        for key, title, _ in _VIEWS:
            assessment = _mapping(stage.get(key)) or {}
            text = f"- {title}: {_mbadge(assessment.get('state'))} — {_m(assessment.get('reason'))}"
            if run.unmeasured_observation(key, assessment):
                text += f"; {_UNMEASURED}"
            if _known(assessment.get("decision_id")):
                text += f"; решение: {_m(assessment.get('decision_id'))}"
            lines.append(text + f"; доказательства: {_mrefs(run, assessment.get('evidence_ids'))}")
        lines.append("")
    lines += ["### Переходы", ""]
    rows = [(str(number), _mstage_ref(run, item.get("from_stage")), _mstage_ref(run, item.get("to_stage")),
             _m(item.get("condition"), "условие не указано"), _mbadge(item.get("state")), _mrefs(run, item.get("evidence_ids")))
            for number, item in enumerate(transitions, 1)]
    lines += _mtable(("№", "Из", "В", "Условие", "Состояние", "Доказательства"), rows) if rows else [f"> {_NO_TRANSITIONS}"]
    lines += ["", "### Сопоставление событий с этапами", "", f"_{_MAPPING_NOTE}_", ""]
    rows = [(_m(item.get("event_key")), _m(item.get("business_action")), _m(item.get("operation")),
             _mstage_ref(run, item.get("stage_id")), _mbadge(item.get("lifecycle_role")), _m(item.get("occurrence_rule")),
             _mbadge(item.get("state")), _mrefs(run, item.get("evidence_ids"))) for item in mappings]
    headers = ("Событие", "Бизнес-действие", "Операция", "Этап", "Роль в жизненном цикле", "Правило вхождения", "Состояние", "Доказательства")
    lines += _mtable(headers, rows) if rows else [f"> {_NO_MAPPINGS}"]
    operations=_records(process_map.get('operations')) or []
    if operations:
        lines += ['', '### Операции внутри этапов', '']
        lines += _mtable(('Операция','Этап','Тип','Описание','Исполнитель','Источники'),[
            (_m(op.get('name')),_mstage_ref(run,op.get('stage_id')),_m(op.get('kind')),
             _m(op.get('description')),_m(op.get('actor')),_mrefs(run,op.get('evidence_ids'))) for op in operations])
        names={op.get('id'):op.get('name') for op in operations}
        edges=_records(process_map.get('operation_transitions')) or []
        lines += ['', '### Переходы между операциями', '']
        lines += _mtable(('Из','В','Условие','Источники'),[
            (_m(names.get(edge.get('from_operation'))),_m(names.get(edge.get('to_operation'))),
             _m(edge.get('condition'),'следующий шаг'),_mrefs(run,edge.get('evidence_ids'))) for edge in edges])
        lines += ['', '### Сценарии', '']
        for item in _records(process_map.get('scenarios')) or []:
            lines += ['- '+_m(item.get('name'))+': '+_m(item.get('description')),
                      '  Операции: '+_mjoin([names.get(key,key) for key in item.get('operation_ids',[])])]
    return lines


def _mcoverage(run, coverage):
    summary = _mapping(coverage.get("summary")) or {}
    provisional = summary.get("scope_provisional")
    scope = "предварительная" if provisional is True else "зафиксирована" if provisional is False else _m(None)
    lines = ["", "### Сводка по реестру требуемых фактов", "",
             f"- Область требований: {scope}",
             f"- Знаменатель (принятые требования применимой или неизвестной области): {_m(summary.get('denominator'))}",
             f"- Числитель (пригодные факты принятой области): {_m(summary.get('numerator'))}",
             f"- Неизвестно: {_m(summary.get('unknown'))}",
             f"- Кандидаты, ещё не принятые требования: {_mjoin(summary.get('candidate_fact_ids'))}",
             f"- Исключены утверждённым решением: {_mjoin(summary.get('excluded_fact_ids'))}",
             "", f"_{_COUNT_NOTE}_"]
    if _known(summary.get("denominator")) == "0":
        lines += ["", f"> {_ZERO_DENOMINATOR}"]
    lines += ["", "### Что нужно фиксировать", ""]
    if not run.facts:
        return lines + [f"> {_NO_FACTS}"]
    rows, details = [], []
    for fact in run.facts:
        requirement = _mapping(fact.get("requirement")) or {}
        opportunities = _mapping(fact.get("opportunities")) or {}
        counted = "".join(f"; {label}: {_m(value)}" for label, value in _opportunity_rows(opportunities))
        if _items(opportunities.get("independent_evidence_ids")):
            counted += f"; независимые доказательства: {_mrefs(run, opportunities.get('independent_evidence_ids'))}"
        rows.append((_m(fact.get("id")), _m(fact.get("meaning")), _mbadge(requirement.get("state")),
                     _mbadge(fact.get("applicability")), _mbadge(fact.get("instrumentation"), _INSTRUMENTED),
                     _mbadge(opportunities.get("state")) + counted, _mbadge(fact.get("delivery"), _DELIVERY),
                     _mbadge(fact.get("quality")), _mjoin(fact.get("metric_ids")), _mrefs(run, fact.get("evidence_ids"))))
        details.append(f"- {_m(fact.get('id'))}: обоснование: {_m(fact.get('rationale'))}; область: {_m(fact.get('scope'))}; "
                       f"этап: {_m(fact.get('stage_id'))}; ключи событий: {_mjoin(fact.get('event_keys'))}; "
                       f"условие: {_m(fact.get('condition'), 'не указано')}; "
                       f"альтернативные доказательства: {_mjoin(fact.get('evidence_alternatives'))}; "
                       f"основание требования: {_m(requirement.get('reason'))}; "
                       f"решение по требованию: {_m(requirement.get('decision_id'), 'нет')}; "
                       f"решение о применимости: {_m(fact.get('decision_id'), 'нет')}")
    headers = ("Факт", "Смысл", "Требование", "Применимость", "Логирование", "Возможности", "Доставка", "Качество", "Метрики", "Доказательства")
    if not _get(run.scope,"telemetry") and not _get(run.scope,"telemetry_profile") and not run.has_telemetry:
        headers = tuple(value for index,value in enumerate(headers) if index not in {5,6})
        rows = [tuple(value for index,value in enumerate(row) if index not in {5,6}) for row in rows]
    return lines + _mtable(headers, rows) + ["", "#### Подробности фактов", ""] + details


def _mplan(run, plan):
    cards = _records(plan.get("cards")) or []
    missing = (_mapping(run.bundle.get("instrumentation_accounting")) or {}).get("unaddressed_fact_ids") or []
    pending = ["", "Доработки по этим фактам ещё не определены:", ""] if missing else []
    for key in missing:
        _, fact = run.fact_index.get(_known(key), (None, {}))
        pending.append("- " + _m(fact.get("meaning") or key))
    if not cards:
        return [*pending, "", f"_{_NO_CARDS}_"]
    lines = [*pending, "", f"_{_PLAN_NOTE}_"]
    for number, card in enumerate(cards, 1):
        anchor = _mapping(card.get("source_anchor"))
        if _known(card.get("location")) == "anchored" and anchor is not None:
            where = (f"путь {_m(anchor.get('path'))}; символ {_m(anchor.get('symbol'))}; "
                     f"строки {_m(_lines(anchor))}; снимок {_m(anchor.get('snapshot_id'))}")
        else:
            where = f"_не определено_ ({_m(_UNRESOLVED)})"
        fields = card.get("fields")
        if isinstance(fields, dict):
            field_text = "; ".join(f"{_m(key)} — {_m(value)}" for key, value in fields.items()) or "_новых полей нет_"
        else:
            field_text = _m(None)
        lines += ["", f"### {number}. {_mbadge(card.get('action'))}: {_m(card.get('id'))}", "",
                  f"- Факт: {_m(card.get('fact_id'))}",
                  f"- Место в коде: {where}",
                  f"- Обоснование: {_m(card.get('rationale'))}",
                  f"- Семантика: {_m(card.get('semantics'))}",
                  f"- Поля: {field_text}",
                  f"- Момент относительно транзакции: {_m(card.get('transaction_timing'), 'не указан')}",
                  f"- Зависимости: {_mjoin(card.get('dependencies'))}",
                  f"- Метрики: {_mjoin(card.get('metric_ids'))}",
                  f"- **План проверки:** {_m(card.get('validation_plan'))}",
                  f"- Доказательства: {_mrefs(run, card.get('evidence_ids'))}"]
    return lines


def _mmetrics(run, catalog):
    if not run.metrics:
        return ["", f"_{_NO_METRICS}_"]
    lines=[]
    pending=[metric for metric in run.metrics if _metric_pending(metric)]
    if pending:
        status, shown, technical = _metric_stage(run)
        lines += ["", "### Определения метрик ещё не сформированы", "", f"- Стадия метрик: {status}"]
        lines += [f"- {_m(text)}" for text in shown]
        lines.append("- Формулы, единицы счёта и состав случаев не выводятся из названий метрик.")
        for metric in pending:
            needed=[]
            for identifier in _items(metric.get('dependencies')):
                _,fact=run.fact_index.get(_known(identifier),(None,{}))
                keys=_mjoin(fact.get('event_keys')) if fact.get('event_keys') else ''
                needed.append(_m(fact.get('meaning'),str(identifier))+(' — '+keys if keys else ''))
            lines.append(f"- {_m(_metric_title(metric))}: нужно записывать — "
                         + ("; ".join(needed) if needed else "_связанные факты не указаны_"))
        if technical:
            lines += ["- Технические причины:"] + [f"  - {_m(text)}" for text in technical]
    for metric in run.metrics:
        if _metric_pending(metric):
            continue
        lines += ["", f"### {_m(_metric_title(metric))}", "",
                  f"- Формула: {_m(metric.get('formula_text'), 'нужно уточнить формулу')}"]
        for key,title in (('counting_unit','Единица счёта'),('cohort','Какие случаи учитываются'),
                          ('time_semantics','Начало и конец'),('open_case_policy','Незавершённые случаи'),
                          ('late_data_policy','Поздние события')):
            if _known(metric.get(key)) is not None:
                lines.append(f'- {title}: {_m(metric[key])}')
        for identifier in _items(metric.get('dependencies')):
            _,fact=run.fact_index.get(_known(identifier),(None,{}))
            meaning=_m(fact.get('meaning'),str(identifier))
            keys=_mjoin(fact.get('event_keys')) if fact.get('event_keys') else ''
            lines.append('- Что нужно записывать: '+meaning+(' — '+keys if keys else ''))
    return lines


def _minsights(run):
    lines = ["", "## Выводы", ""]
    if run.insights is None:
        return lines + [f"> {_NO_INSIGHTS}"]
    if not run.insights:
        return lines + ["_Список выводов пуст._"]
    lines.append(f"_{_INSIGHT_NOTE}_")
    for number, insight in enumerate(run.insights, 1):
        fields = {key: value for key, value in insight.items() if key != "evidence_ids"} if isinstance(insight, dict) else insight
        lines += ["", f"### Вывод {number}", ""] + _mvalue(fields)
        if isinstance(insight, dict) and "evidence_ids" in insight:
            lines.append(f"- Доказательства: {_mrefs(run, insight.get('evidence_ids'))}")
    return lines


def _mevidence(run):
    lines = ["", "## Доказательства", "", f"> {_QUOTE_LIMIT}"]
    if run.evidence is None:
        return lines + ["", f"> {_NO_EVIDENCE}"]
    if not run.evidence:
        return lines + ["", "_Реестр доказательств пуст._"]
    for item in run.evidence:
        anchor = _mapping(item.get("source_anchor"))
        lines += ["", f"### {_m(item.get('id'))}", "",
                  f"- Вид: {_mbadge(item.get('kind'))}",
                  f"- Снимок: {_m(item.get('snapshot_id'))}",
                  f"- Локатор: {_m(item.get('locator'))}"]
        if anchor is not None:
            lines += [f"- Путь: {_m(anchor.get('path'))}", f"- Символ: {_m(anchor.get('symbol'))}",
                      f"- Строки: {_m(_lines(anchor))}"]
        lines += [f"- Хеш фрагмента: {_m(item.get('raw_span_hash'))}",
                  f"- Проверка текста: {_mbadge(item.get('text_validation'), _TEXT)}",
                  f"- Проверка смысла: {_mbadge(item.get('semantic_validation'), _MEANING)}",
                  f"- Статус утверждения: {_mbadge(item.get('claim_state'))}"]
        lines += _mquote(item)
    return lines
