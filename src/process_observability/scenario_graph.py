"""Business operations inside stage regions. Only explicit semantic edges are drawn.

All geometry comes from scenario_layout and is precomputed for every scenario
view; the page script only switches between finished views.
"""

import html
import json

from . import report as r
from . import scenario_layout as L
from .graph_font import FONT_FAMILY


KINDS = {"start":"Начало", "operation":"Действие", "decision":"Проверка", "end":"Результат"}
ROUTES = (("#7f99ad", "#eef3f7"), ("#a58eb6", "#f4eff7"),
          ("#ad8d9d", "#f8f0f4"), ("#7eaaa9", "#eef6f5"),
          ("#9a9bb7", "#f1f1f8"), ("#af9d85", "#f7f3ed"))
STATUS = {
    "code_logs": ("Код и события найдены", "#edf6ef", "#a6c9af"),
    "code_only": ("Код найден, события не подтверждены", "#fff3e7", "#d9ba98"),
    "unlinked": ("Без привязки к коду", "#f5f7f8", "#bcc6cc"),
    "unknown": ("Покрытие не оценено", "#ffffff", "#d8e0df"),
}
NEUTRAL, CONTEXT = "#8f9da9", "#bbc2c8"


def operation_status(run, operation):
    """Project existing source/fact assessments; never infer executed delivery.

    Green means related events were found, not that the complete requirement set
    or runtime delivery is established. Orange includes an unresolved mapping.
    """
    refs = [ref for ref in r._items(operation.get('evidence_ids')) if isinstance(ref,str)]
    code = any(run.evidence_index.get(ref, (None, {}))[1].get('kind') == 'code'
               and run.evidence_index[ref][1].get('text_validation') == 'verified' for ref in refs)
    if not code:
        scope=run.scope or {}
        partial = scope.get('partial_input') or r._get(scope.get('source'), 'partial_input')
        return 'unknown' if refs or partial else 'unlinked'
    keys = {key for key in r._items(operation.get('event_keys')) if isinstance(key,str)}
    view = _inspection(run)["views"].get(operation.get('id'))
    if view is not None and view["linkage"] == "exact":
        # Colour follows this operation's compatible emit mappings, not shared fact ownership alone.
        own = {identifier for log in view["logs"] if log["linkage"] == "exact"
               and log["role_support"] == "emit_within_operation_sources"
               and log["site_status"] not in {"contradicted", "unsupported"}
               and log["reconciliation"] not in {"contradicted", "unsupported"}
               for identifier in log["fact_ids"]}
        facts = [fact for fact in run.facts if fact.get('id') in own]
    else:
        facts = [fact for fact in run.facts if fact.get('stage_id') == operation.get('stage_id')
                 and keys.intersection(fact.get('event_keys') or [])]
    found = {key for fact in facts if fact.get('instrumentation') == 'found'
             for key in fact.get('event_keys') or []}
    return 'code_logs' if keys and facts and keys <= found and all(
        fact.get('instrumentation') == 'found' for fact in facts) else 'code_only'


def _memberships(scenarios, key, item_id):
    return ' '.join(str(i) for i,scenario in enumerate(scenarios,1) if item_id in r._items(scenario.get(key)))


def _a(value):
    return html.escape(str(value), quote=True)


def _n(value):
    value = round(float(value), 1)
    return str(int(value)) if value.is_integer() else str(value)


def _path(points):
    return 'M ' + ' L '.join(f'{_n(x)} {_n(y)}' for x, y in points)


def _tspans(text, x, top):
    baseline = L.BASELINE[text.size]
    return ''.join(f'<tspan x="{_n(x)}" y="{_n(top + baseline + i * text.line_height)}">{html.escape(line)}</tspan>'
                   for i, line in enumerate(text.lines))


def _display_edges(process):
    """Collapse only byte-equivalent semantic records except their IDs, for display.

    Keep every original record/position and union scenario membership. Different
    evidence, conditions or any other supplied field always produce separate arrows.
    This never edits the map, its accounting, or its scenario definitions.
    """
    groups = {}
    for number, item in enumerate(r._records(process.get('operation_transitions')) or [], 1):
        key = json.dumps({k: v for k, v in item.items() if k != 'id'}, sort_keys=True, ensure_ascii=False)
        groups.setdefault(key, []).append((number, item))
    return list(groups.values())


def _model(run):
    process = run.sections["map"] or {}
    operations = r._records(process.get("operations")) or []
    scenarios = r._records(process.get("scenarios")) or []
    columns = [r._known(stage.get("name")) or "Этап" for stage in run.stages]
    places = [run.stage_index.get(r._known(op.get("stage_id")), (None, None))[0] for op in operations]
    if any(place is None for place in places):
        columns.append("Этап не указан")
    places = [len(run.stages) if place is None else place - 1 for place in places]

    def member(key, item):
        return frozenset(i for i, scenario in enumerate(scenarios, 1) if item.get("id") in r._items(scenario.get(key)))

    nodes = [{"column": place, "kind": op.get("kind"), "name": r._known(op.get("name")) or "Без названия",
              "scenarios": member("operation_ids", op)} for op, place in zip(operations, places)]
    first = {}
    for index, op in enumerate(operations):
        key = r._known(op.get("id"))
        if key is not None:
            first.setdefault(key, index)
    edges = []
    for records in _display_edges(process):
        number, edge = records[0]
        source, target = first.get(r._known(edge.get("from_operation"))), first.get(r._known(edge.get("to_operation")))
        if source is None or target is None:
            continue
        condition = r._known(edge.get('condition'))
        edges.append({"number": number, "source": source, "target": target, "item": edge,
                      "records": records,
                      "condition": condition,
                      "scenarios": frozenset().union(*(member('transition_ids', record) for _, record in records))})
    views = ["all"] + [str(i) for i in range(1, len(scenarios) + 1)]
    if scenarios and any(not item["scenarios"] for item in nodes):
        views.append("unassigned")
    return operations, scenarios, columns, nodes, edges, views


def build_layout(run):
    """The scenario map layout exactly as graph() renders it, for checks and tests."""
    _, _, columns, nodes, edges, views = _model(run)
    return L.layout(columns, nodes, edges, views)


def _style(view, scenarios):
    """Stroke colour, marker, width, dash and active flag of an edge in one view."""
    if view == "all":
        if len(scenarios) == 1:
            (unique,) = scenarios
            return ROUTES[(unique-1)%len(ROUTES)][0], f'scenario-arrow-{unique}', '1.6', '6,4' if unique != 1 else '-', '1'
        return NEUTRAL, 'scenario-arrow', '1.6', '-', '1'
    if not L.member(view, scenarios):
        return CONTEXT, 'scenario-arrow-context', '1.4', '-', '0'
    if view == "unassigned":
        return NEUTRAL, 'scenario-arrow', '2', '-', '1'
    return ROUTES[(int(view)-1)%len(ROUTES)][0], f'scenario-arrow-{view}', '2.2', '-', '1'


def graph(run):
    operations, scenarios, columns, model_nodes, model_edges, views = _model(run)
    if not operations:
        return ''
    lay = L.layout(columns, model_nodes, model_edges, views)
    default = "1" if scenarios else "all"
    index = views.index(default)
    view = lay.views[index]
    names = [item["name"] for item in model_nodes]
    items = {edge["number"]: edge for edge in model_edges}

    regions = []
    for c, column in enumerate(lay.columns):
        title = f'<text class="region-title" data-max-w="{column.title.limit}">{_tspans(column.title, column.x + L.TITLE_INSET, lay.title_top)}</text>'
        if c < len(run.stages):
            title = f'<a href="#st-{c+1}">{title}</a>'
        stage_id = _a(run.stages[c].get("id")) if c < len(run.stages) else ''
        empty = (f'<text class="region-empty" x="{column.x + L.TITLE_INSET}" y="{lay.empty_top + L.BASELINE[11]}">{L.EMPTY_TEXT}</text>'
                 if column.empty else '')
        regions.append(f'<g class="stage-region" data-stage-id="{stage_id}" data-hs="{" ".join(_n(v.height - 2*L.MARGIN) for v in lay.views)}"'
                       f' data-counts="{_a("|".join(L.count_text(lay, v, c) for v in lay.views))}">'
                       f'<rect x="{column.x}" y="{L.MARGIN}" width="{column.w}" height="{_n(view.height - 2*L.MARGIN)}" rx="18" fill="#ffffff" fill-opacity=".58" stroke="#d9e5df"/>'
                       + title + f'<text class="region-count" x="{column.x + L.TITLE_INSET}" y="{lay.count_top + L.BASELINE[11]}">{L.count_text(lay, view, c)}</text>'
                       + empty + '</g>')

    separators = []
    for item in lay.views:
        if item.separator is None:
            separators.append('-')
        else:
            line, box = item.separator
            separators.append(f'{_n(line)} {_n(box.x)} {_n(box.y + 12)} {L.MARGIN} {lay.width - L.MARGIN}')
    if view.separator:
        line, box = view.separator
        zone = (f'<g class="context-zone" data-sep="{"|".join(separators)}"><path d="M {L.MARGIN} {_n(line)} H {lay.width - L.MARGIN}"/>'
                f'<text x="{_n(box.x)}" y="{_n(box.y + 12)}">{L.SEPARATOR_TEXT}</text></g>')
    else:
        zone = (f'<g class="context-zone" data-sep="{"|".join(separators)}" hidden display="none"><path d="M 0 0"/>'
                f'<text x="0" y="0">{L.SEPARATOR_TEXT}</text></g>')

    edges = []
    for edge in sorted(lay.edges, key=lambda e: (e.number in view.active_edges, e.number)):
        model = items[edge.number]
        item = model['item']
        styles = [_style(v.key, edge.scenarios) for v in lay.views]
        color, marker, width, dash, active = styles[index]
        condition = edge.label.source if edge.label else None
        title = f'Из «{names[edge.source.key]}» в «{names[edge.target.key]}»' + (f': {condition}' if condition else '')
        d = _path(view.routes[edge.number])
        dash_attr = f' stroke-dasharray="{dash.replace(",", " ")}"' if dash != '-' else ''
        body = (f'<title>{html.escape(title)}</title><path class="edge-halo" d="{d}"/>'
                f'<path class="edge-line" d="{d}" fill="none" stroke="{color}" stroke-width="{width}"{dash_attr} marker-end="url(#{marker})"/>')
        attrs = (f'data-scenarios="{" ".join(str(n) for n in sorted(model["scenarios"]))}"'
                 f' data-from-node="{_a(item.get("from_operation"))}" data-to-node="{_a(item.get("to_operation"))}"'
                 f' data-edge-index="{edge.number}" data-active="{active}"'
                 f' data-d="{"|".join(_path(v.routes[edge.number]) for v in lay.views)}"'
                 f' data-style="{"|".join(" ".join(style) for style in styles)}"')
        if edge.label:
            box = view.labels[edge.number]
            attrs += ' data-l="' + '|'.join(f'{_n(v.labels[edge.number].x)} {_n(v.labels[edge.number].y)}' for v in lay.views) + '"'
            body += (f'<g class="edge-caption" transform="translate({_n(box.x)} {_n(box.y)})">'
                     f'<rect class="edge-label-bg" x="0" y="0" width="{edge.label_w}" height="{edge.label_h}" rx="4"/>'
                     f'<text class="edge-kind" x="{edge.label_w/2}" y="11" text-anchor="middle">Условие</text>'
                     f'<text class="edge-label" text-anchor="middle" data-max-w="{edge.label.limit}">'
                     f'{_tspans(edge.label, edge.label_w / 2, L.LABEL_PAD_Y + L.TYPE_HEIGHT)}</text></g>')
        body = (f'<a class="transition-link" href="#transition-{edge.number}" aria-label="{_a(title)}">'
                + body + '</a>')
        edges.append(f'<g class="scenario-edge" {attrs}>{body}</g>')

    orders = [{key: position for position, key in enumerate(v.order)} for v in lay.views]
    inspected = _inspection(run)["views"]
    nodes = []
    for key in view.order:
        node, op, number = lay.nodes[key], operations[key], key + 1
        top, x, w, h = view.node_y[key], node.x, L.NODE_W, node.h
        state = operation_status(run, op)
        status, fill, color = STATUS[state]
        dash = ' stroke-dasharray="5 4"' if state == 'unlinked' else ''
        if node.kind == "decision":
            tip = L.HEX_TIP
            outline = (f'M {_n(x+tip)} {_n(top)} L {_n(x+w-tip)} {_n(top)} L {_n(x+w)} {_n(top+h/2)} '
                       f'L {_n(x+w-tip)} {_n(top+h)} L {_n(x+tip)} {_n(top+h)} L {_n(x)} {_n(top+h/2)} Z')
            shape = f'<path class="op-shape" d="{outline}" fill="{fill}" stroke="{color}"{dash}/>'
        else:
            shape = f'<rect class="op-shape" x="{_n(x)}" y="{_n(top)}" width="{w}" height="{h}" rx="{_n(L.radius(node))}" fill="{fill}" stroke="{color}"{dash}/>'
        text_top = top + L.TYPE_HEIGHT + (h - L.TYPE_HEIGHT - node.text.height) / 2
        cx = x + w / 2
        text = (f'<text class="node-kind" text-anchor="middle" x="{_n(cx)}" y="{_n(top+14)}">'
                + KINDS.get(op.get('kind'), 'Узел') + '</text>'
                + f'<text class="op-name" text-anchor="middle" data-max-w="{node.text.limit}">{_tspans(node.text, cx, text_top)}</text>')
        safe = f'{_n(cx - node.text.limit/2 - 2)} {_n(text_top - 4)} {node.text.limit + 4} {node.text.height + 8}'
        attrs = (f'data-op="op-{number}" data-scenarios="{_memberships(scenarios,"operation_ids",op.get("id"))}"'
                 f' data-node-id="{_a(op.get("id"))}" data-stage-id="{_a(op.get("stage_id"))}" data-x="{_n(x)}" data-y="{_n(top)}"'
                 f' data-col="{node.column}" data-row="{view.rows[key]}" data-coverage="{state}"'
                 f' data-active="{"1" if key in view.active_nodes else "0"}"'
                 f' data-ys="{" ".join(_n(v.node_y[key]) for v in lay.views)}"'
                 f' data-as="{" ".join("1" if key in v.active_nodes else "0" for v in lay.views)}"'
                 f' data-order="{" ".join(str(order[key]) for order in orders)}"'
                 f' data-box="{_n(x)} {_n(top)} {w} {h}" data-tb="{safe}"')
        asked = len(inspected.get(op.get("id"), {}).get("questions", []))
        attrs += f' data-questions="{asked}"'
        asked_text = f' · вопросов: {asked}' if asked else ''
        label = f'{names[key]} · {lay.columns[node.column].name} · {status}{asked_text}'
        nodes.append(f'<a href="#op-{number}" aria-label="{_a(label)}"><g class="node operation-node" {attrs}>'
                     f'<title>{html.escape(names[key] + " · " + status + asked_text)}</title>{shape}{text}</g></a>')

    chips='<div class="scenario-chips" role="group" aria-label="Сценарии"><button class="scenario-filter'+(' active' if default=='all' else '')+f'" data-scenario="all" type="button" aria-pressed="{"true" if default=="all" else "false"}">Все сценарии</button>'+''.join(
        f'<button class="scenario-filter'+(' active' if number==1 else '')+f'" data-scenario="{number}" data-route-color="{ROUTES[(number-1)%len(ROUTES)][0]}" type="button" aria-pressed="{"true" if number==1 else "false"}"><span class="route-dot" aria-hidden="true"></span>{r._h(item.get("name"))}</button>' for number,item in enumerate(scenarios,1))
    if "unassigned" in views:
        count=sum(not item["scenarios"] for item in model_nodes)
        chips+=f'<button class="scenario-filter" data-scenario="unassigned" type="button" aria-pressed="false">Вне сценариев · {count}</button>'
    chips+='</div>'
    tools='<div class="map-tools"><input id="node-search" type="search" placeholder="Найти операцию" aria-label="Поиск операции"><div class="zoom-controls"><button id="zoom-out" type="button" aria-label="Уменьшить">−</button><output id="zoom-label">100%</output><button id="zoom-in" type="button" aria-label="Увеличить">+</button><button id="fit-map" type="button">Вписать</button><button id="expand-map" type="button" aria-label="Развернуть карту">⤢</button></div></div>'
    markers=[('scenario-arrow',NEUTRAL),('scenario-arrow-context',CONTEXT)]
    markers += [('scenario-arrow-'+str(number),ROUTES[(number-1)%len(ROUTES)][0]) for number in range(1,len(scenarios)+1)]
    defs='<defs>'+''.join(f'<marker id="{name}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M1 1 L9 5 L1 9" fill="none" stroke="{color}" stroke-width="1.6"/></marker>' for name,color in markers)+'</defs>'
    labels = [('code_logs','Код и события'), ('code_only','События не подтверждены'),
              ('unlinked','Без привязки к коду'), ('unknown','Не оценено')]
    legend = '<div class="coverage-legend" aria-label="Покрытие по исходникам">' + ''.join(
        f'<span><span class="coverage-swatch" data-coverage="{state}" aria-hidden="true"></span>{label}</span>'
        for state,label in labels) + '</div>'
    svg = (f'<svg data-views="{" ".join(views)}" data-default="{default}" data-hs="{" ".join(_n(v.height) for v in lay.views)}"'
           f' data-font="{_a(FONT_FAMILY)}" viewBox="0 0 {lay.width} {_n(view.height)}" width="{lay.width}" height="{_n(view.height)}"'
           ' role="group" aria-label="Сценарии процесса: операции внутри этапов">'
           + defs + ''.join(regions) + zone + '<g class="edge-layer">' + ''.join(edges) + '</g>'
           + '<g class="node-layer">' + ''.join(nodes) + '</g></svg>')
    scenario_names = [r._known(item.get("name")) or f"Сценарий {number}" for number, item in enumerate(scenarios, 1)]
    route_items = []
    for edge in lay.edges:
        text = f'{names[edge.source.key]} → {names[edge.target.key]}'
        if edge.label:
            text += f' · условие: {edge.label.source}'
        if edge.scenarios:
            text += ' · сценарии: ' + ', '.join(scenario_names[number-1] for number in sorted(edge.scenarios))
        route_items.append('<li>' + html.escape(text) + '</li>')
    routes = (f'<details class="route-list"><summary>Переходы на схеме · {len(route_items)}</summary><ul>' + ''.join(route_items) + '</ul></details>'
              if route_items else '')
    from .hierarchy_graph import mount
    hierarchy = mount(run)
    return (hierarchy + ('<div class="static-operation-graph">' if hierarchy else '')
            + chips+tools+legend+'<div class="map">'+svg+'</div><p class="graph-caption"><span id="scenario-readout" aria-live="polite"></span></p>'
            + '<p class="graph-caption">Действие — работа; проверка — выбор ветви; результат — исход. '
              'Подпись «Условие» относится к стрелке. Нажмите на узел или стрелку, чтобы открыть пояснение.</p>'
            + f'<p class="graph-caption path-note">{PATH_NOTE}</p>'
            + routes + '<pre id="layout-check-result" class="layout-check" hidden></pre>'
            + ('</div>' if hierarchy else ''))


PATH_NOTE = ("Выбор операции подсвечивает связанные пути до и после неё по переходам карты, включая циклы. "
             "Это структурные связи карты, а не трасса исполнения конкретного случая.")
INITIATIONS = {"user_interaction": "Действие пользователя: обработчик взаимодействия процитирован в коде",
               "api_call": "Вызов API: команда сервиса; действие пользователя за ним не установлено",
               "automatic": "Автоматически: по расписанию или событию системы",
               "external_message": "Входящее сообщение или обратный вызов внешней системы",
               "unknown": "Способ запуска не установлен"}
EFFECTS = {"decision": "Решение: операция сама принимает решение (по реализации)",
           "recommendation": "Рекомендация: операция только предлагает действие (по реализации)",
           "command": "Команда другой системе или компоненту; исполнение у получателя по этому коду не подтверждено",
           "result": "Результат по реализации: операция сама фиксирует эффект; наблюдением не проверено"}
OUTCOMES = {"success": "успех", "refusal": "отказ по бизнес-правилу", "failure": "сбой", "cancelled": "отмена",
            "deferred": "перенос на потом"}
CONTINUATIONS = {"choice": "выбирается одна из ветвей (основание в коде)",
                 "all": "выполняются все подходящие продолжения (основание в коде)"}
JOINS = {"all": "ожидает завершения всех входящих ветвей (основание в коде)",
         "any": "достаточно одной входящей ветви (основание в коде)"}
DIRECTIONS = {"request": "запрос к внешней системе", "send": "отправка данных", "receive": "получение данных"}
BASES = {"source_condition": "по условию в коде", "source_call": "по вызову в коде",
         "source_handoff": "по передаче управления или данных в коде", "business_text": "по бизнес-описанию"}
CITED_BY = {"model": "указано моделью", "host_unit": "из единицы команды (учёт хоста)",
            "unknown": "источник ссылки не указан"}
SUBJECTS = {"operation": "Операция", "transition": "Переход", "unit": "Единица кода", "fact": "Факт",
            "process": "Процесс"}
LEVEL_NAMES = (("process", "Процесс"), ("stages", "Крупные блоки"), ("operations", "Подробно"))


def _inspection(run):
    """Operation views, questions, detail levels and source anchors of this bundle, computed once per report."""
    cached = getattr(run, "_r08_inspection", None)
    if cached is None:
        from . import operation_view as V
        bundle = run.bundle
        sources = {}
        for prefix, items in (("code", r._records(r._get(bundle.get("report_sources"), "excerpts")) or []),
                              ("ev", run.evidence or [])):
            for number, item in enumerate(items, 1):
                anchor = r._mapping(item.get("source_anchor")) or {}
                sources.setdefault((anchor.get("path"), anchor.get("start_line"), anchor.get("end_line")),
                                   f"#{prefix}-{number}")
        numbers = {}
        for number, op in enumerate(r._records(r._get(run.sections["map"], "operations")) or [], 1):
            numbers.setdefault(op.get("id"), number)
        cached = {"views": V.operation_views(bundle), "questions": V.question_index(bundle),
                  "levels": {level: V.detail_view(bundle, level) for level in V.LEVELS},
                  "links": V.card_links(bundle), "sources": sources, "numbers": numbers,
                  "transitions": r._records(r._get(run.sections["map"], "operation_transitions")) or []}
        run._r08_inspection = cached
    return cached


def _code_link(run, path, symbol, first, last):
    """File, symbol and lines first; the link opens the complete cited fragment already in the report."""
    if not isinstance(path, str):
        return '<span class="unk">Место в коде не определено</span>'
    label = html.escape(path) + (f' · {html.escape(symbol)}' if isinstance(symbol, str) and symbol != "<module>" else '')
    label += f' · строки {r._h(first)}–{r._h(last)}'
    href = _inspection(run)["sources"].get((path, first, last))
    if href:
        return f'<a class="code-link" href="{href}">{label}</a>'
    return label + ' <span class="muted">фрагмент кода не приложен к этому запуску</span>'


def _evidence_links(run, ids):
    links = []
    for key in r._items(ids):
        _, item = run.evidence_index.get(r._known(key), (None, None))
        anchor = r._mapping((item or {}).get("source_anchor"))
        links.append(_code_link(run, anchor.get("path"), anchor.get("symbol"), anchor.get("start_line"),
                                anchor.get("end_line")) if anchor else r._hcode(key))
    return ', '.join(links) if links else '<span class="unk">основание не указано</span>'


def _op_link(run, operation_id):
    inspection = _inspection(run)
    number = inspection["numbers"].get(operation_id)
    name = (inspection["views"].get(operation_id) or {}).get("name") or operation_id
    return f'<a href="#op-{number}">{r._h(name)}</a>' if number else r._h(name)


def _fact_link(run, fact_id):
    number, fact = run.fact_index.get(r._known(fact_id), (None, {}))
    text = r._h(fact.get("meaning") or fact_id)
    # #fact-N anchors are rendered with the logging changes; without that section the name stays plain text.
    return f'<a href="#fact-{number}">{text}</a>' if number and run.sections["instrumentation"] is not None else text


def _card_link(run, card_id, text):
    number, _ = run.card_index.get(r._known(card_id), (None, None))
    return f'<a href="#card-{number}">{text}</a>' if number else text


def _initiation(view):
    if view["initiation"] is None:
        return '<span class="unk">Способ запуска не установлен: у карты нет атомарного учёта</span>'
    text = r._h(INITIATIONS.get(view["initiation"], view["initiation"]))
    if view["initiation_basis"]:
        text += '<p class="muted">Основание: ' + r._h(view["initiation_basis"]) + '</p>'
    return text


def _details_section(run, view):
    details = view["details"]
    parts = ['<div class="op-section" data-section="details"><h4>По реализации</h4>']
    if details is None:
        parts.append('<p class="unk">Вход, ожидаемый результат и условия по реализации не установлены: атомарный ответ '
                     'не указал их с основанием в коде.</p></div>')
        return ''.join(parts)
    rows, unknown = [], []
    for key, label in (("input", "Вход по реализации"), ("expected_result", "Ожидаемый результат по реализации"),
                       ("precondition", "Предусловие"), ("completion_condition", "Условие завершения процесса")):
        if details.get(key):
            rows.append((label, r._h(details[key])))
        else:
            unknown.append(label.lower())
    if details.get("effect_kind") in EFFECTS:
        rows.append(("Тип эффекта", r._h(EFFECTS[details["effect_kind"]])))
    else:
        unknown.append("тип эффекта")
    if details.get("outcomes"):
        rows.append(("Возможные исходы по реализации",
                     r._h(", ".join(OUTCOMES.get(value, value) for value in details["outcomes"]))))
    if details.get("continuation") in CONTINUATIONS:
        rows.append(("Продолжение", r._h(CONTINUATIONS[details["continuation"]])))
    if details.get("join") in JOINS:
        rows.append(("Входящие ветви", r._h(JOINS[details["join"]])))
    if details.get("evidence_ids"):
        rows.append(("Основание в коде", _evidence_links(run, details["evidence_ids"])))
    if rows:
        parts.append(r._hdl(rows))
    if unknown:
        parts.append('<p class="unk">Не установлено: ' + html.escape(", ".join(unknown)) + '.</p>')
    for item in r._records(details.get("external_interactions")) or []:
        direction = item.get("direction")
        note = ("Результат на стороне получателя по этому коду не подтверждён." if direction in ("request", "send")
                else "Данные получены; их источник и полнота по этому коду не проверены.")
        parts.append('<div class="op-external"><p><strong>' + r._h(item.get("system")) + '</strong> · '
                     + r._h(DIRECTIONS.get(direction, direction)) + '</p>'
                     + (f'<p>Назначение: {r._h(item.get("purpose"))}</p>' if item.get("purpose") else '')
                     + (f'<p>Точка вызова: {r._hcode(item.get("endpoint"))}</p>' if item.get("endpoint") else '')
                     + (f'<p>Контракт: {r._h(item.get("contract"))}</p>' if item.get("contract") else '')
                     + f'<p class="note">{note}</p><p>Основание: {_evidence_links(run, item.get("evidence_ids"))}</p></div>')
    parts.append('</div>')
    return ''.join(parts)


def _logs_section(run, view):
    parts = ['<div class="op-section" data-section="logs"><h4>Что уже видно в логах</h4>']
    if view["logs"] and view["linkage"] == "exact" and all(item["linkage"] == "exact" for item in view["logs"]):
        parts.append('<p class="note">Связь по точному месту вызова, принятому для этой операции.</p>')
    elif view["logs"]:
        parts.append('<p class="note">Есть связи по ключу события и этапу: такие записи помечены как кандидаты; '
                     'точное место вызова для них не установлено.</p>')
    if not view["logs"]:
        parts.append('<p class="muted">Связанных записей среди найденных мест логирования нет.</p>')
    rows = []
    for item in view["logs"]:
        role = item["lifecycle_role"]
        label = "роль не установлена" if role in (None, "unknown") else r._STATES.get(role, (role,))[0]
        candidate = item["linkage"] != "exact"
        if candidate:
            meaning = "Кандидат по ключу события: принадлежность записи этой операции и её результат не подтверждены."
        elif item["shows_new_result"]:
            meaning = "Запись о результате этой операции в коде; доставка записи и исполнение не проверены."
        elif role == "result":
            meaning = ("Роль «результат», но запись не подтверждает новый результат: место вызова или смысл "
                       "привязки к операции не подтверждены.")
        else:
            meaning = "Запись не подтверждает новый результат операции."
        facts = ', '.join(_fact_link(run, fact) for fact in item["fact_ids"])
        rows.append('<li>' + r._hcode(item["event_key"]) + f' <span class="b t-info">{html.escape(label)}</span>'
                    + f'<p>{meaning}</p><p>Когда появляется: {r._h(item["occurrence_rule"], "правило не указано")}</p>'
                    + f'<p>Где: {_code_link(run, item["path"], None, item["start_line"], item["end_line"])}</p>'
                    + (f'<p>{"Возможные связи с фактами (кандидаты)" if candidate else "Связанные факты"}: {facts}</p>'
                       if facts else '')
                    + (f'<p class="unk">Неоднозначность: {r._h(item["ambiguity"])}</p>' if item["ambiguity"] else '')
                    + '</li>')
    if rows:
        parts.append('<ul class="op-logs">' + ''.join(rows) + '</ul>')
    if view["facts"]:
        label = "" if view["linkage"] == "exact" else " (по ключу события, кандидаты)"
        parts.append(f'<p>Факты операции{label}: ' + ', '.join(
            _fact_link(run, fact["id"]) + ' ' + r._hbadge(fact["instrumentation"], r._INSTRUMENTED)
            for fact in view["facts"]) + '</p>')
    parts.append('</div>')
    return ''.join(parts)


def _change(run, item):
    action = r._LOG_ACTIONS.get(item["action"], item["action"])
    fields = ', '.join(r._hcode(name) + (f' — {r._h(meaning)}' if meaning else '')
                       for name, meaning in item["fields"].items())
    anchor = item["anchor"] or {}
    where = (_code_link(run, anchor.get("path"), anchor.get("symbol"), anchor.get("start_line"),
                        anchor.get("end_line")) if item["location"] == "anchored" and anchor
             else '<span class="unk">Место в коде не определено</span>')
    return (f'<strong>{r._h(item["semantics"])}</strong> <span class="b t-info">{r._h(action)}</span>'
            + f'<p>Зачем: {r._h(item["rationale"])}</p>'
            + (f'<p>Данные: {fields}</p>' if fields else '')
            + f'<p>Когда записывать: {r._h(item["transaction_timing"], "момент записи не указан")}</p>'
            + f'<p>Где: {where}</p><p>Закрывает: {_fact_link(run, item["fact_id"])}</p>')


def _improvements_section(run, view):
    parts = ['<div class="op-section" data-section="improvements"><h4>Чего не хватает в логах</h4>']
    if view["improvements"]:
        parts.append('<ul>' + ''.join('<li>' + _change(run, item) + '<p>'
                                      + _card_link(run, item["card_id"], "Открыть доработку в плане") + '</p></li>'
                                      for item in view["improvements"]) + '</ul>')
    if view["improvement_candidates"]:
        parts.append('<p class="note">Кандидаты: доработка связана с операцией только по пересечению строк кода или по '
                     'ключу события. Принадлежность не установлена; в плане доработка указана один раз.</p><ul>')
        for item in view["improvement_candidates"]:
            basis = "кандидат по пересечению строк кода" if item["linkage"] == "source_overlap" else "кандидат по ключу события"
            others = ', '.join(_op_link(run, other) for other in item["shared_with"])
            parts.append('<li>' + _card_link(run, item["card_id"], r._h(item["semantics"]))
                         + f' <span class="b t-warn">{basis}</span>'
                         + (f'<p>Также пересекается с: {others}</p>' if others else '') + '</li>')
        parts.append('</ul>')
    if not view["improvements"] and not view["improvement_candidates"]:
        parts.append('<p class="muted">Доработок, связанных с этой операцией через её факты, нет.</p>')
    parts.append('</div>')
    return ''.join(parts)


def _routes_section(run, view, key):
    items = view[key]
    title = "Раньше" if key == "previous" else "Дальше"
    parts = [f'<div class="op-section" data-section="{key}"><h4>{title}</h4>']
    if key == "next" and len(items) > 1:
        mode = CONTINUATIONS.get(view["continuation"])
        parts.append(f'<p>Продолжение: {r._h(mode)}</p>' if mode else
                     '<p class="unk">Способ выбора продолжения не установлен: ни выбор одной ветви, ни выполнение '
                     'всех ветвей кодом не подтверждены.</p>')
    if key == "previous" and len(items) > 1:
        mode = JOINS.get(view["join"])
        parts.append(f'<p>Входящие ветви: {r._h(mode)}</p>' if mode else
                     '<p class="unk">Ожидание входящих ветвей не установлено.</p>')
    rows = []
    for item in items:
        basis = BASES.get(item["basis"])
        basis = (r._h(basis) if basis else '<span class="unk">основание порядка не указано</span>')
        if item["basis_note"]:
            basis += ': ' + r._h(item["basis_note"])
        refs = (' · ' + _evidence_links(run, item["evidence_ids"])) if item["evidence_ids"] else ''
        rows.append('<li>' + _op_link(run, item["operation_id"])
                    + (f'<p>Условие: {r._h(item["condition"])}</p>' if item["condition"]
                       else '<p class="unk">Условие не установлено</p>')
                    + f'<p>Основание: {basis}{refs}</p></li>')
    if rows:
        parts.append('<ul>' + ''.join(rows) + '</ul>')
    elif key == "previous":
        parts.append('<p class="unk">Предшествующие действия по переходам карты не установлены.</p>')
    else:
        parts.append('<p class="unk">Порядок после этой операции не установлен: в карте нет перехода дальше, и это не '
                     'означает остановку процесса.</p>')
    parts.append('</div>')
    return ''.join(parts)


def _paths_section(run, view):
    from .operation_view import related_operations
    found = related_operations(_inspection(run)["transitions"], view["id"])
    return ('<div class="op-section" data-section="paths"><p class="note">Связано по переходам карты: до операции — '
            f'{len(found["upstream"])}, после — {len(found["downstream"])}. {PATH_NOTE}</p></div>')


def _questions_section(run, view):
    if not view["questions"]:
        return ''
    rows = []
    for item in view["questions"]:
        others = [_op_link(run, other) for other in item["operation_ids"] if other != view["id"]]
        rows.append(f'<li><span class="b t-unknown">{SUBJECTS.get(item["subject_kind"], "Вопрос")}</span> '
                    + r._h(item["text"]) + (' · ' + ', '.join(others) if others else '') + '</li>')
    return ('<div class="op-section" data-section="questions"><h4>Что осталось выяснить · '
            + str(len(rows)) + '</h4><ul>' + ''.join(rows) + '</ul></div>')


def _hints_section(view):
    if not view["hints"]:
        return ''
    return ('<div class="op-section" data-section="hints"><h4>Эвристическая подсказка</h4><ul>'
            + ''.join('<li>' + r._h(item["text"]) + '</li>' for item in view["hints"])
            + '</ul><p class="note">Это эвристика по форме карты, а не проверка атомарности: решение остаётся за '
              'человеком, статус анализа от неё не меняется.</p></div>')


def _code_section(run, view):
    entries = view["code"]
    explanations = r._records((view["details"] or {}).get("code_explanations")) or []
    parts = [f'<details class="op-section op-code" data-section="code"><summary>Код и основания · {len(entries)}</summary>']
    if entries:
        parts.append('<ul class="code-refs">' + ''.join(
            '<li><p>' + _code_link(run, item["path"], item["symbol"], item["start_line"], item["end_line"])
            + f' <span class="b t-unknown">{CITED_BY.get(item["cited_by"], item["cited_by"])}</span></p>'
            + '<pre class="code-preview">' + html.escape(item["preview"]) + '</pre></li>' for item in entries) + '</ul>')
    else:
        parts.append('<p class="unk">Ссылок на код у операции нет: реализация не установлена.</p>')
    for item in explanations:
        parts.append('<div class="code-explanation"><p>' + r._hcode(item.get("symbol")) + ' · '
                     + _evidence_links(run, item.get("evidence_ids")) + '</p><p class="interpretation">Пояснение '
                     'модели — интерпретация, не проверка кода: ' + r._h(item.get("explanation")) + '</p></div>')
    parts.append('<p class="note">Ссылка открывает полный процитированный фрагмент. Цитата подтверждает только текст '
                 'исходника, не исполнение и не доставку записей.</p></details>')
    return ''.join(parts)


def _empty_view(op):
    return {"id": op.get("id"), "name": op.get("name"), "description": op.get("description"), "initiation": None,
            "initiation_basis": None, "linkage": "event_key", "logs": [], "facts": [], "improvements": [],
            "improvement_candidates": [], "previous": [], "next": [], "continuation": "unknown", "join": "unknown",
            "questions": [], "code": [], "details": None, "hints": [], "unit_ids": [], "request_id": None,
            "event_keys": []}


def operation_card(run, number, op):
    """One inspector card: business meaning first, then logs, changes, routes, questions and code."""
    view = _inspection(run)["views"].get(op.get("id")) or _empty_view(op)
    stage_number, _ = run.stage_index.get(r._known(op.get("stage_id")), (None, None))
    stage = r._h(r._stage_title(run, op.get("stage_id")))
    stage_link = f'<a href="#st-{stage_number}">{stage}</a>' if stage_number else stage
    meta = r._hdl([("Этап", stage_link), ("Исполнитель", r._h(op.get("actor"), "не указан")),
                   ("Способ запуска", _initiation(view))])
    technical = r._hdl([("Идентификатор", r._hcode(op.get("id"))),
                        ("Основание учёта", r._h("атомарная запись операции" if view["linkage"] == "exact"
                                       else "по ключу события (кандидаты)")),
                        ("Запрос", r._hcode(view["request_id"])), ("Единицы кода", r._hjoin(view["unit_ids"], r._hcode)),
                        ("Ключи событий", r._hjoin(view["event_keys"], r._hcode))])
    return (f'<article class="stage operation-card" id="op-{number}"><p class="inspector-kicker">'
            + f'{KINDS.get(op.get("kind"), "Операция")} · {stage}</p><h3>{r._h(op.get("name"))}</h3>'
            + f'<p class="operation-description">{r._h(op.get("description"), "Описание пока не составлено")}</p>' + meta
            + _details_section(run, view) + _logs_section(run, view) + _improvements_section(run, view)
            + _routes_section(run, view, "previous") + _routes_section(run, view, "next") + _paths_section(run, view)
            + _questions_section(run, view) + _hints_section(view) + _code_section(run, view)
            + '<details class="technical"><summary>Служебные сведения</summary>' + technical + '</details></article>')


def operation_cards(run):
    process = run.sections["map"] or {}
    return ''.join(operation_card(run, index, op)
                   for index, op in enumerate(r._records(process.get("operations")) or [], 1))


def _questions_block(run):
    items = _inspection(run)["questions"]
    if not items:
        return ''
    rows = []
    for item in items:
        links = [_op_link(run, identifier) for identifier in item["operation_ids"]]
        if item["subject_kind"] == "fact":
            links.append(_fact_link(run, item["subject_id"]))
        rows.append(f'<li><span class="b t-unknown">{SUBJECTS.get(item["subject_kind"], "Вопрос")}</span> '
                    + r._h(item["text"]) + (' · ' + ', '.join(links) if links else '') + '</li>')
    return (f'<details class="map-questions"><summary>Открытые вопросы · {len(rows)}</summary><ul>'
            + ''.join(rows) + '</ul></details>')


def _level_counts(item):
    return (f'Операций: {len(item["operation_ids"])} · записей логов: {item["event_mapping_count"]} · фактов: '
            f'{len(item["fact_ids"])} · доработок: {len(item["card_ids"])} · метрик: {len(item["metric_ids"])} · '
            f'вопросов: {item["question_count"]}')


def transition_cards(run):
    """A real destination for each arrow, retaining aliases and original evidence."""
    cards = []
    for records in _display_edges(run.sections['map'] or {}):
        number, edge = records[0]
        condition = r._known(edge.get('condition'))
        raw = ''.join('<li>' + r._hcode(item.get('id')) + ' · ' + _evidence_links(run, item.get('evidence_ids'))
                      + '</li>' for _, item in records)
        aliases = ''.join(f'<span id="transition-{n}"></span>' for n, _ in records[1:])
        cards.append(f'<article class="stage transition-card" id="transition-{number}">{aliases}'
                     '<p class="inspector-kicker">Переход между операциями</p><h3>'
                     + _op_link(run, edge.get('from_operation')) + ' → ' + _op_link(run, edge.get('to_operation'))
                     + '</h3><p>' + ('Условие: ' + r._h(condition) if condition else
                                                   'Условие перехода не установлено') + '</p>'
                     + '<p class="note">Связь из сохранённой карты; не подтверждение исполнения или результата.</p>'
                     + f'<details><summary>Исходных записей: {len(records)}</summary><ul>{raw}</ul></details></article>')
    return ''.join(cards)


def group_cards(run):
    """Groups inherit their members' sources; they do not cite a new action themselves."""
    from .operation_hierarchy import hierarchy_view
    groups = hierarchy_view(run.bundle)['groups']
    cards = []
    operations = {op.get('id'): op for op in (r._records((run.sections['map'] or {}).get('operations')) or [])}
    for number, group in enumerate(groups, 1):
        members = set(group['operation_ids'])
        sources = list(dict.fromkeys(e for identifier in group['operation_ids']
                                      for e in operations[identifier].get('evidence_ids', [])))
        inside = [(records[0][0], records) for records in _display_edges(run.sections['map'] or {})
                  if records[0][1].get('from_operation') in members and records[0][1].get('to_operation') in members]
        links = ''.join('<li>' + _op_link(run, identifier) + '</li>' for identifier in group['operation_ids'])
        edges = ''.join('<li>' + _op_link(run, records[0][1].get('from_operation')) + ' → '
                        + _op_link(run, records[0][1].get('to_operation')) + f' · <a href="#transition-{n}">'
                        + r._h(records[0][1].get('condition'), 'Условие не установлено')
                        + '</a> · исходных записей: ' + str(len(records)) + '</li>' for n, records in inside)
        cards.append(f'<article class="stage group-card" id="group-{number}"><p class="inspector-kicker">Бизнес-блок</p>'
                     '<h3>' + r._h(group['name']) + '</h3><p>Навигационная группа, предложенная моделью при композиции. '
                     'Это агрегат дочерних действий, а не дополнительное выполнение операции.</p><h4>Операции</h4><ul>'
                     + links + '</ul><h4>Внутренние переходы</h4>' + ('<ul>' + edges + '</ul>' if edges else
                     '<p>Связи между дочерними операциями не установлены. Порядок списка не задаёт порядок выполнения.</p>')
                     + '<details class="technical"><summary>Основания группы</summary><p>Источники унаследованы от операций; '
                     'группа не имеет отдельного кодового основания.</p>' + _evidence_links(run, sources)
                     + '<p>Запрос композиции: ' + r._hcode(group.get('request_id')) + '</p></details></article>')
    return ''.join(cards)


def _levels(run, process):
    """Process -> stages -> operations: one switch over the same records; the operations view holds the graph."""
    levels = _inspection(run)["levels"]
    totals = levels["process"]["totals"]
    switch = ('<div class="detail-levels" role="group" aria-label="Уровень детализации">' + ''.join(
        f'<button class="detail-level{" active" if key == "stages" else ""}" type="button" data-detail-level="{key}"'
        f' aria-pressed="{"true" if key == "stages" else "false"}">{label}</button>' for key, label in LEVEL_NAMES)
        + '</div><p class="detail-note">Уровень меняет только представление: все действия, записи логов, источники, '
          'вопросы, метрики и ограничения остаются в отчёте. Этапы служат навигацией и не задают порядок исполнения.</p>')
    counts = [("Этапы", len(run.stages)), ("Операции", totals["operations"]),
              ("Сопоставленные записи логов", totals["event_mappings"]), ("Факты", totals["facts"]),
              ("Доработки логирования", totals["cards"]), ("Метрики", totals["metrics"]),
              ("Адресные вопросы", totals["questions"]), ("Открытые вопросы и ограничения карты", totals["unknowns"]),
              ("Неучтённые единицы кода", totals["open_units"]), ("Источники", totals["evidence"])]
    stage_list = []
    for item in levels["stages"]["items"]:
        number, _ = run.stage_index.get(r._known(item["id"]), (None, None))
        title = (f'<a href="#st-{number}">{r._h(item["name"], "без названия")}</a>' if number
                 else r._h(item["name"], "Вне известных этапов"))
        stage_list.append(f'<li>{title} · операций: {len(item["operation_ids"])}</li>')
    order = [f'{r._h(r._stage_title(run, item.get("from_stage")))} → {r._h(r._stage_title(run, item.get("to_stage")))}'
             + (f' ({r._h(item.get("condition"))})' if item.get("condition") else '')
             for item in levels["stages"]["transitions"]]
    stage_order = ('<p class="note">Переходы каркаса этапов: ' + '; '.join(order) + '. Это не порядок исполнения '
                   'операций.</p>' if order else '<p class="note">Порядок этапов не установлен.</p>')
    process_view = ('<div class="level-view level-process" data-level-view="process" hidden><h3>'
                    + r._h(process.get("purpose"), "Процесс") + '</h3>' + r._hdl([(label, str(value)) for label, value in counts])
                    + ('<ul class="level-stage-list">' + ''.join(stage_list) + '</ul>' if stage_list else '')
                    + stage_order + '</div>')
    return switch + process_view


def map_body(run,process):
    scenarios=r._records(process.get("scenarios")) or []
    overview='<div class="inspector-overview"><p class="inspector-kicker">Карта процесса</p><h3>Сценарии и операции</h3><p>'+r._h(process.get("boundaries"))+'</p><div class="process-unit"><span>Один экземпляр процесса</span><strong>'+r._h(process.get("case_definition"))+'</strong></div>'
    overview+='<div class="scenario-list">'+''.join('<div><strong>'+r._h(item.get("name"))+'</strong><p>'+r._h(item.get("description"),"")+'</p></div>' for item in scenarios)+'</div><p class="inspector-hint">Выберите операцию на схеме</p></div>'
    questions=_questions_block(run)
    stages=[]
    operations=r._records(process.get('operations')) or []
    grouped={item["id"]: item for item in _inspection(run)["levels"]["stages"]["items"] if item["kind"]=="stage"}
    for index,stage in enumerate(run.stages,1):
        members=[(i,op) for i,op in enumerate(operations,1) if op.get('stage_id')==stage.get('id')]
        links='<ul class="next-steps">'+''.join(f'<li><a href="#op-{i}">'+r._h(op.get('name'))+'</a></li>' for i,op in members)+'</ul>'
        counts=f'<p class="level-counts">{_level_counts(grouped[stage.get("id")])}</p>' if stage.get("id") in grouped else ''
        states=r._hdl([(title,r._hbadge(r._get(stage.get(key),'state'))) for key,title,_ in r._VIEWS])
        sources=r._hrefs(run,r._get(stage.get('implemented'),'evidence_ids'))
        stages.append(f'<article class="stage" id="st-{index}"><p class="inspector-kicker">Этап</p><h3>'+r._h(stage.get('name'))+'</h3>'+counts+links+'<details class="technical"><summary>Код и данные</summary>'+states+sources+'</details></article>')
    stages=''.join(stages)
    return ('<div class="map-layout"><div class="map-main">' + _levels(run, process)
            + '<div class="level-view level-operations" data-level-view="stages operations">' + graph(run) + '</div>'
            + questions + '</div><aside class="stage-inspector">' + overview + operation_cards(run)
            + transition_cards(run) + group_cards(run) + stages
            + '</aside></div>')
