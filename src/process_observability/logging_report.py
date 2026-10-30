"""Deterministic three-section report and portable export of the logging layer.

The input HTML is never read or executed. The Карта section is the first agent's trusted
renderer (vendored in ``map_presentation``) with a C/P/U/X overlay bound to the exact map
revision; it builds no second map. Логирование and Доработки are escaped server-side text.
"""
import base64
import csv
from copy import deepcopy
import hashlib
import html
import io
from pathlib import Path

from .contracts import strict_json_loads
from .logging_contracts import LABELS, SCHEMA, LoggingReviewError, canonical, task_is_blocking
from .map_input import MapView, read_regular
from .map_presentation import EMBED_HEADER_SLOT, EMBED_PANELS_SLOT, scenario_map_parts, validate_hierarchy
from .operation_coverage import summarize, validate_saved_rows
from .redaction import redact_text

ROLE_LABELS = {'request': 'Запрос', 'start': 'Начало', 'result': 'Результат', 'receiver_response': 'Ответ получателя',
               'error': 'Ошибка', 'repeat': 'Повтор', 'no_op': 'Без изменения'}
MOMENT_LABELS = {'before_action': 'до действия', 'after_action': 'после действия',
                 'after_confirmation': 'после подтверждённого ответа', 'on_failure': 'при сбое',
                 'unknown': 'момент не установлен'}
SITE_STATUS_LABELS = {'mapped': 'Есть привязка', 'pending': 'Ожидает проверки', 'unmapped': 'Без привязки',
                      'not_log': 'Не запись журнала', 'supporting': 'Техническая запись',
                      'other_operation': 'Запись другой операции'}
DISPOSITION_LABELS = {'not_log': 'Не запись журнала', 'technical_log': 'Техническая запись',
                      'unresolved': 'Назначение не установлено', 'other_operation_log': 'Запись другой операции'}
CHANGE_LABELS = {'map_existing': 'Сопоставить существующую запись', 'join': 'Связать записи',
                 'enrich': 'Дополнить существующую запись', 'add_field': 'Добавить поле', 'add_event': 'Добавить запись',
                 'move_emit': 'Перенести запись'}
SCOPE_LABELS = {'source': 'исходный код', 'map': 'карта сценария', 'runtime': 'исполнение, уровень журнала и доставка'}
# These host-generated scope notes belong to the report as a whole. Keep every
# other limitation visible, including model text and unresolved source details.
REPORT_WIDE_LIMITATIONS = frozenset({
    'Статический разбор не доказывает включённость уровня журналирования, фактическое выполнение и доставку записи',
    'Назначение интерфейса и адаптера как журналирования оценивает модель по исходникам; '
    'семантическая правильность этой оценки, внешний SDK, выполнение и доставка не проверены автоматически',
})


def _specific_limitations(items):
    return list(dict.fromkeys(str(item) for item in items
                             if str(item) not in REPORT_WIDE_LIMITATIONS))


def validate_report(data):
    try:
        return _validate_report(data)
    except LoggingReviewError:
        raise
    except (KeyError, TypeError, AttributeError, IndexError):
        raise LoggingReviewError('Malformed saved logging report') from None


def _validate_report(data):
    """Structural checks here; per-row semantics and witnesses belong to operation_coverage."""
    if type(data) is not dict or data.get('schema_version') != SCHEMA:
        raise LoggingReviewError('Expected logging-review/1')
    view = MapView.from_dict(data['map'])
    if data.get('map_content_sha256') != view.sha256:
        raise LoggingReviewError('The logging report map content has changed')
    if type(data.get('operations')) is not list or any(type(row) is not dict for row in data['operations']):
        raise LoggingReviewError('Invalid operation review list')
    summary = summarize(view.operation_ids, data['operations'])
    if summary != data.get('summary') or len(data['operations']) != len(view.operation_ids):
        raise LoggingReviewError('Logging summary does not match the complete map-owned accounting')
    sites = {site['id']: site for site in data['inventory']['sites']}
    if len(sites) != len(data['inventory']['sites']): raise LoggingReviewError('Duplicate inventory site IDs')
    validate_saved_rows(view, data)
    if data.get('business_metrics') != 'frozen': raise LoggingReviewError('Business metrics are outside this report')
    return view


def _csv(headers, rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n'); writer.writerow(headers)
    for row in rows:
        # Prevent spreadsheet formula execution while keeping raw values in JSON.
        writer.writerow(["'" + str(v) if str(v).lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else str(v)
                         for v in row])
    return output.getvalue()


def _missing_cell(row):
    """An unassessed row publishes the model's missing items as hypotheses, never as established absence."""
    items = '; '.join(str(item) for item in row.get('missing') or [])
    if not items: return ''
    if row['state'] == 'X': return 'Не проверено; предположение модели: ' + items
    if row.get('missing_status') == 'hypothesis': return 'Предположение модели: ' + items
    return items


def _percent(summary):
    uncovered = summary['uncovered']
    if uncovered['lower'] is None: return 'В карте нет операций'
    if uncovered['exact']: return uncovered['display_lower'] + '%'
    return uncovered['display_lower'] + '–' + uncovered['display_upper'] + '%'


def _task(task):
    if type(task) is not dict: return {'description': str(task), 'kind': None, 'blocking': True, 'scope': None}
    return {'description': str(task.get('description') or ''), 'kind': task.get('kind'),
            'blocking': task_is_blocking(task), 'scope': task.get('scope')}


def _ref(ref):
    if type(ref) is not dict: return {}
    return {key: ref.get(key) for key in ('path', 'start_line', 'end_line', 'quote') if ref.get(key) is not None}


def _proposal(change):
    return {'id': change.get('id'), 'kind': change.get('kind'), 'what': change.get('what')} if type(change) is dict else {}


def _overlay_binding(binding, site):
    site = site or {}
    chain = binding.get('chain') if type(binding.get('chain')) is dict else None
    route = binding.get('route') if type(binding.get('route')) is dict else {}
    return {'site_id': binding.get('site_id'), 'record': site.get('event_key') or site.get('callee') or binding.get('site_id'),
            'callee': site.get('callee'), 'role': binding.get('role'), 'moment': binding.get('moment'),
            'meaning': binding.get('meaning'), 'conditions': binding.get('conditions'),
            'correlation': binding.get('correlation'), 'evidence': [_ref(r) for r in binding.get('evidence') or []],
            'chain': None if chain is None else {'basis': chain.get('basis'), 'terminal': chain.get('terminal'),
                                                 'hops': chain.get('hops') or [],
                                                 'limitations': _specific_limitations(chain.get('limitations') or []),
                                                 'source_contract': chain.get('source_contract')},
            'route_limitations': _specific_limitations(route.get('limitations') or []),
            'contextual_use': _context_summary(binding), 'shared_with': binding.get('shared_with'),
            'shared_meaning': binding.get('shared_meaning')}


def _context_summary(binding):
    basis=binding.get('context_basis')
    if type(basis) is not dict:return None
    return {'status':basis.get('status'),'meaning_authority':basis.get('meaning_authority'),
            'invocation':_ref(binding.get('invocation_ref')),
            'emit':_ref((basis.get('emit') or {}).get('statement')),
            'guards':[{'condition':_ref(g.get('condition')),'branch':g.get('branch')}
                      for g in (basis.get('caller') or {}).get('path_conditions',[])]}


def logging_overlay(data, view):
    """The C/P/U/X layer for the renderer, keyed by the immutable operation IDs of ``view``.

    Fields added by policy 2 are optional: legacy rows have no moment, task typing or missing_status.
    """
    sites = {site['id']: site for site in data['inventory']['sites']}
    operations = {}
    for row in data['operations']:
        state = row['state']
        operations[row['operation_id']] = {
            'state': state,
            'bindings': [_overlay_binding(b, sites.get(b.get('site_id'))) for b in row['bindings'] if type(b) is dict],
            'missing': [str(item) for item in row.get('missing') or []],
            'missing_status': 'hypothesis' if state == 'X' else row.get('missing_status') or 'established',
            'tasks': [_task(task) for task in row.get('review_tasks') or []],
            'limitations': _specific_limitations(row.get('limitations') or []),
            'changes': [_proposal(c) for c in row.get('changes') or []],
            'rejected_changes': [_proposal(c) for c in row.get('rejected_changes') or []],
            'change_diagnostics': [{'local_id': d.get('local_id'), 'error': d.get('error')}
                                   for d in row.get('change_diagnostics') or [] if type(d) is dict]}
    summary = data['summary']
    return {'map_revision': view.revision, 'operations': operations,
            'summary': {'denominator': summary['denominator'], 'counts': summary['counts'],
                        'display': _percent(summary), 'scope': summary['scope']}}


def _presentation_hierarchy(raw, model):
    """The saved grouping for the map, or None with a visible note; operations never disappear."""
    if raw is None: return None, []
    try:
        if type(raw) is not dict: raise ValueError('ожидался объект')
        hierarchy = deepcopy(raw)
        listed = {stage['id']: stage['operation_ids'] for stage in hierarchy.get('stages') or []
                  if type(stage) is dict and type(stage.get('operation_ids')) is list}
        membership = hierarchy.get('membership')
        if not membership:
            # Earlier saved groupings may list members only per stage.
            membership = {}
            for stage_id, members in listed.items():
                for identifier in members:
                    if identifier in membership: raise ValueError('операция повторяется в двух этапах')
                    membership[identifier] = stage_id
            hierarchy['membership'] = membership
        elif type(membership) is dict and any(sorted(map(str, members)) != sorted(k for k, v in membership.items() if v == stage_id)
                                              for stage_id, members in listed.items()):
            raise ValueError('состав этапов противоречит принадлежности операций')
        validate_hierarchy(hierarchy, model)
        return hierarchy, []
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return None, ['Бизнес-этапы не показаны на карте: ' + redact_text(str(exc))[:300]]


def _e(value):
    return html.escape('' if value is None else str(value), quote=True)


def _note(value):
    return value if isinstance(value, str) else canonical(value)


def _op_button(identifier, names):
    return ('<button type="button" class="op-link" data-report-operation="' + _e(identifier) + '">'
            + _e(names.get(identifier, identifier)) + '</button>')


def _code_ref(ref):
    if type(ref) is not dict: return ''
    start = ref.get('start_line') or ref.get('line_start')
    end = ref.get('end_line') or ref.get('line_end') or start
    where = str(ref.get('path') or '') + ((':' + str(start) + ('–' + str(end) if end != start else '')) if start else '')
    quote = ref.get('quote') or ref.get('safe_quote') or 'Текст не приложен; путь указан для проверки в исходной репозитории.'
    return '<details class="code-ref"><summary>' + _e(where) + '</summary><pre>' + _e(quote) + '</pre></details>'


def _header_html(data):
    summary = data['summary']
    limit = ('<p class="coverage-scope" id="map-limit">Исходная карта частичная: процент относится только к показанным '
             'операциям.</p>' if (data['map'].get('run') or {}).get('status') == 'partial' else '')
    chips = ''.join('<span class="coverage-chip state-' + key + '">' + _e(LABELS[key] + ': ' + str(summary['counts'][key]))
                    + '</span>' for key in ('C', 'P', 'U', 'X'))
    tabs = ''.join('<button type="button" role="tab" id="tab-' + view + '" data-view="' + view + '" aria-controls="' + panel
                   + '" aria-selected="' + ('true' if view == 'map' else 'false') + '"'
                   + ('' if view == 'map' else ' tabindex="-1"') + '>' + label + '</button>'
                   for view, panel, label in (('map', 'panel-map', 'Карта'), ('logging', 'panel-logging', 'Логирование'),
                                              ('improvements', 'panel-improvements', 'Доработки')))
    return ('<div class="coverage-header"><p class="coverage-headline" id="coverage-headline">'
            + _e('Без достаточного покрытия логами: ' + _percent(summary)) + '</p>'
            '<p class="coverage-scope" id="coverage-scope">'
            + _e(summary['scope'] + '. Всего операций: ' + str(summary['denominator']) + '.') + '</p>' + limit
            + '<div class="coverage-totals" id="coverage-counts">' + chips + '</div></div>'
            '<div class="report-tabs" id="report-tabs" role="tablist" aria-label="Разделы отчёта">' + tabs + '</div>')


def _logging_panel(data, names, notes):
    sites = {site['id']: site for site in data['inventory']['sites']}
    rows = []
    for row in data['operations']:
        if row['state'] == 'X': continue  # bindings of an unfinished assessment are not published as existing records
        for binding in row['bindings']:
            site = sites.get(binding.get('site_id')) or {}
            record = site.get('event_key') or site.get('callee') or binding.get('site_id')
            role = ROLE_LABELS.get(binding.get('role'), str(binding.get('role') or 'Роль не указана'))
            moment = MOMENT_LABELS.get(binding.get('moment'), MOMENT_LABELS['unknown'])
            chain = binding.get('chain') if type(binding.get('chain')) is dict else {}
            route = binding.get('route') if type(binding.get('route')) is dict else {}
            limits = _specific_limitations((chain.get('limitations') or []) + (route.get('limitations') or []))
            contextual=_context_summary(binding)
            explanation=''
            if contextual is not None:
                explanation='<details><summary>Связь через соседний вызов</summary>'+_code_ref(contextual['invocation'])
                for guard in contextual['guards']:
                    explanation+=_code_ref(guard['condition'])+'<p>При проверке: '+('истина' if guard['branch']=='true' else 'ложь')+'</p>'
                explanation+='<p>'+_e('Путь вызова подтверждён кодом.'
                    if contextual['status']=='supported' else 'Синтаксическое основание связи не полностью установлено; запись не подтверждает достаточность покрытия.')+'</p></details>'
            if binding.get('shared_with'):
                explanation+='<p>Общая запись с '+_op_button(binding['shared_with'],names)+': '+_e(binding.get('shared_meaning'))+'</p>'
            rows.append('<tr><td>' + _op_button(row['operation_id'], names) + '</td><td><code>' + _e(record) + '</code><p>'
                        + _e(role + ' · ' + moment) + '</p></td><td>' + _e(binding.get('meaning')) + '</td><td><p>'
                        + _e(binding.get('conditions')) + '</p></td><td>'
                        + _e(binding.get('correlation') or 'Способ связи не указан в оценке') + '</td><td>'
                        + ''.join(_code_ref(ref) for ref in binding.get('evidence') or [])
                        + explanation
                        + (('<p class="report-note">' + _e('Ограничения: ' + '; '.join(limits)) + '</p>') if limits else '')
                        + '</td></tr>')
    empty = ('' if rows else '<p id="logs-empty" class="empty">Подтверждённых привязок бизнес-записей пока нет. '
             'Это не означает, что в коде нет логов.</p>')
    registry = []
    for site in data['inventory']['sites']:
        status = SITE_STATUS_LABELS.get(site.get('review_status'), str(site.get('review_status') or 'Без привязки'))
        detail = ''
        for item in site.get('dispositions') or []:
            if type(item) is not dict: continue
            related = item.get('related_operation_id')
            detail += ('<p class="report-note">' + _e(DISPOSITION_LABELS.get(item.get('classification'), item.get('classification')))
                       + (': ' + _op_button(related, names) if related else '') + '</p>')
        registry.append('<tr><td>' + _e(site.get('event_key') or 'Динамическая запись') + '</td><td>' + _e(site.get('callee'))
                        + '</td><td>' + _e(status) + detail + '</td><td>' + _e(str(site.get('path')) + ':' + str(site.get('start_line')))
                        + '</td></tr>')
    unbound = sum(site.get('review_status') != 'mapped' for site in data['inventory']['sites'])
    context_rows=[]
    use_labels={'bound':'Учтено в привязке','prior_fact_not_bound':'Подтверждает предшествующий факт; не включено в привязку',
                'not_relevant':'Другой смысл','unresolved':'Связь не установлена'}
    for operation in data['operations']:
        for entry in (operation.get('context_assessment') or {}).get('entries',[]):
            context_rows.append('<li>'+_op_button(operation['operation_id'],names)+': '
                +_e(use_labels[entry['use']])+'. '+_e(entry['reason'])+_code_ref(entry['invocation'])+'</li>')
    context_details=('<details class="registry"><summary>Оценка соседних записей</summary><ul>'
                     +''.join(context_rows)+'</ul></details>') if context_rows else ''
    notes = [_note(v) for v in [*(data['map'].get('limitations') or []), *(data.get('presentation_warnings') or []),
                                *((data.get('artifact_reuse') or {}).get('notes') or []), *notes]]
    return ('<section id="panel-logging" class="report-panel" role="tabpanel" aria-labelledby="tab-logging" hidden inert>'
            '<h2>Существующие бизнес-записи</h2><div class="table-wrap"><table id="logs"><thead><tr><th>Операция</th>'
            '<th>Что записывается</th><th>Что позволяет установить</th><th>Момент записи</th>'
            '<th>Идентификаторы и связь записей</th><th>Код</th>'
            '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>' + empty
            + '<details class="registry"><summary id="registry-title">'
            + _e('Полный реестр: ' + str(len(data['inventory']['sites'])) + ' мест, ' + str(unbound) + ' без принятой привязки')
            + '</summary><div class="table-wrap"><table id="registry"><thead><tr><th>Запись</th><th>Вызов</th>'
            '<th>Привязка</th><th>Место</th></tr></thead><tbody>' + ''.join(registry) + '</tbody></table></div></details>'
            + context_details + '<details class="registry"><summary>Область и ограничения</summary><ul id="limitations">'
            + ''.join('<li>' + _e(note) + '</li>' for note in notes) + '</ul>'
            '<p>Назначение и смысл записей оценены моделью по коду; выполнение, настройки и доставка логов '
            'не проверялись.</p></details></section>')


def _placement_description(change):
    position=change.get('placement')
    if position is None:return ''
    words={'before':'Перед указанным оператором','after':'После указанного оператора','at':'Изменить указанную запись'}
    info=change.get('placement_context') or {}
    parts=[words.get(position,str(position))]
    point=info.get('point')
    if point:parts.append('Точка: строка '+str(point.get('line'))+', столбец '+str(point.get('column'))+' (с нуля)')
    if info.get('status')=='supported':parts.append('Синтаксический путь согласован; бизнес-смысл отдельно проверяется')
    else:parts.append('Согласованность точки с управлением не установлена')
    if info.get('status')!='supported':
        for guard in change.get('guards') or []:
            condition=guard.get('condition') or {}
            parts.append('Заявлено моделью: '+str(condition.get('path'))+':'+str(condition.get('start_line'))+' — '
                         +str(condition.get('quote'))+' → '+('истина' if guard.get('branch')=='true' else 'ложь'))
    for guard in info.get('path_conditions') or []:
        condition=guard.get('condition') or {}
        parts.append('Найдено синтаксически, при проверке '+str(condition.get('path'))+':'+str(condition.get('start_line'))+' — '
                     +str(condition.get('quote'))+' → '+('истина' if guard.get('branch')=='true' else 'ложь'))
    if info.get('repetition_lines'):parts.append('Внутри цикла; число повторов по коду не установлено')
    return '\n'.join(parts)


def _improvements_panel(data, names):
    from .logging_change_groups import proposal_groups
    groups=proposal_groups(data['changes'])
    grouped=''
    if groups:
        grouped='<h3>Предложения в одной точке кода</h3><p>Это указатель пересечений. Исходные формулировки и проверки каждого предложения сохранены ниже.</p><ul>'
        for group in groups:
            grouped+='<li>'+ ' '.join(_op_button(i,names) for i in group['operation_ids'])+' · '+', '.join(
                '<a href="#change-'+_e(i)+'">'+_e(i)+'</a>' for i in group['change_ids'])
            if group['structured_conflict']:grouped+=' · Различаются логгер или поля: требуется согласование'
            if group['unverified_grouping']:grouped+=' · Синтаксическая точка не подтверждена'
            grouped+='</li>'
        grouped+='</ul>'
    changes = []
    for change in data['changes']:
        proposal = ('<p>' + _e(change.get('what')) + '</p><small>'
                    + _e('Предложение · ' + CHANGE_LABELS.get(change.get('kind'), str(change.get('kind')))) + '</small><p>'
                    + _e('Проблема: ' + str(change.get('problem') or '')) + '</p>'
                    + ''.join('<p class="report-note">' + _e(note) + '</p>' for note in change.get('requirements') or [])
                    + (('<p class="report-note">' + _e('Зависит от: ' + ', '.join(map(str, change['dependencies']))) + '</p>')
                       if change.get('dependencies') else ''))
        fields = ''.join('<p>' + _e(str(field.get('name')) + ' = ' + str(field.get('expression')) + ' ('
                                   + ('имеется в коде' if field.get('origin') == 'existing' else 'предлагается') + ')') + '</p>'
                         for field in change.get('fields') or [] if type(field) is dict)
        changes.append('<tr id="change-'+_e(change['id'])+'"><td>' + ' '.join(_op_button(i, names) for i in change.get('operation_ids') or []) + '</td><td>'
                       + proposal + '</td><td>' + _e(change.get('why')) + '</td><td>' + fields + '</td><td>'
                       + _code_ref(change.get('location'))
                       + ('<p class="report-note">'+_e(_placement_description(change)).replace('\n','<br>')+'</p>'
                          if change.get('placement') is not None else '')
                       + '<p>' + _e(change.get('timing')) + '</p><p>'
                       + _e('Проверка: ' + str(change.get('verification') or '')) + '</p></td></tr>')
    blocking, limits, rejected = [], [], []
    for row in data['operations']:
        for task in map(_task, row.get('review_tasks') or []):
            scope = SCOPE_LABELS.get(task['scope'])
            item = '<li>' + _op_button(row['operation_id'], names) + ' ' + _e(task['description'] + (' · ' + scope if scope else '')) + '</li>'
            (blocking if task['blocking'] else limits).append(item)
        for change in row.get('rejected_changes') or []:
            if type(change) is dict:
                rejected.append('<li>' + _op_button(row['operation_id'], names) + ' '
                                + _e('Отклонено: ' + str(change.get('what') or change.get('id')) + ' · '
                                     + CHANGE_LABELS.get(change.get('kind'), str(change.get('kind')))) + '</li>')
        for item in row.get('change_diagnostics') or []:
            if type(item) is dict:
                rejected.append('<li>' + _op_button(row['operation_id'], names) + ' '
                                + _e('Ошибка необязательного предложения' + (' ' + str(item['local_id']) if item.get('local_id') else '')
                                     + ': ' + str(item.get('error') or '')) + '</li>')
    empty = '' if changes else ('<p id="changes-empty" class="empty">Адресных доработок пока нет. Незавершённые проверки '
                                'перечислены ниже.</p>')
    task_block = ('<h3>Блокирующие проверки</h3><ul id="review-tasks" class="task-list">' + ''.join(blocking) + '</ul>'
                  if blocking else '<p id="review-tasks" class="report-note">Блокирующих проверок нет.</p>')
    limit_block = ('<h3>Ограничения статической проверки · не блокируют оценку</h3><ul class="task-list">'
                   + ''.join(limits) + '</ul>' if limits else '')
    rejected_block = ('<h3>Отклонённые и некорректные предложения</h3><ul class="task-list">' + ''.join(rejected) + '</ul>'
                      if rejected else '')
    return ('<section id="panel-improvements" class="report-panel" role="tabpanel" aria-labelledby="tab-improvements" hidden inert>'
            '<h2>Что изменить в логировании</h2><p>Предлагаемые изменения ещё не внедрены и не повышают текущее покрытие.</p>'
            +grouped+'<div class="table-wrap"><table id="changes"><thead><tr><th>Операция</th><th>Что изменить</th><th>Зачем</th>'
            '<th>Данные записи</th><th>Код, момент и проверка</th></tr></thead><tbody>' + ''.join(changes)
            + '</tbody></table></div>' + empty + '<h2>Что ещё проверить</h2>' + task_block + limit_block + rejected_block
            + '</section>')


REPORT_CSS = '''
.coverage-header{display:flex;flex-direction:column;gap:4px;margin:6px 0 0}
.coverage-headline{margin:0;font-size:16px;font-weight:500;color:var(--ink);overflow-wrap:anywhere}
.coverage-scope{margin:0;font-size:12px;color:var(--ink-secondary);overflow-wrap:anywhere}
.coverage-totals{display:flex;flex-wrap:wrap;gap:6px;margin:2px 0 0}
.report-tabs{display:flex;flex-wrap:wrap;gap:4px;width:fit-content;max-width:100%;margin:10px 0 0;padding:4px;border-radius:var(--radius-pill);background:var(--bg-subtle)}
.report-tabs button{min-height:36px;padding:6px 16px;border:0;border-radius:var(--radius-pill);background:transparent;color:var(--ink-secondary);font-size:13px;white-space:normal}
.report-tabs button:hover{background:var(--bg-muted);color:var(--ink)}
.report-tabs button[aria-selected=true]{background:var(--bg-inverse);color:var(--ink-inverse);font-weight:500}
.main-panels>.report-panel{padding:20px 24px 32px;overflow:auto}
.report-panel h2{font-size:18px;margin:8px 0 12px}.report-panel h3{font-size:15px;margin:18px 0 8px}
.report-panel>p{max-width:1100px}
.report-panel table{table-layout:auto}.report-panel td{white-space:normal;max-width:none}.report-panel td p{margin:0 0 6px}
.report-panel code{font:12px/1.5 var(--font-mono);overflow-wrap:anywhere}
.op-link{min-height:36px;padding:0;border:0;background:transparent;color:var(--accent-ink);font-weight:600;text-align:left;white-space:normal;overflow-wrap:anywhere;cursor:pointer}
.op-link:hover{text-decoration:underline;text-underline-offset:3px}
.code-ref summary{cursor:pointer;color:var(--accent-ink);font-size:12px;min-height:28px;overflow-wrap:anywhere}
.code-ref pre{margin:6px 0;padding:8px 10px;border-radius:var(--radius-md);background:var(--bg-subtle);font:11px/1.5 var(--font-mono);white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}
.registry{margin-top:20px}.registry summary{cursor:pointer;color:var(--accent-ink);min-height:32px}
.registry ul{padding-left:20px;overflow-wrap:anywhere}
.task-list{margin:0 0 12px;padding-left:20px}.task-list li{margin:6px 0;overflow-wrap:anywhere}
.report-note{color:var(--ink-secondary);font-size:13px;overflow-wrap:anywhere}
.report-panel .empty{padding:16px 0;margin:0}
@media(max-width:700px){.main-panels>.report-panel{padding:16px 12px 24px}.report-tabs button{padding:6px 12px;font-size:12px}}
'''

# The report shell owns the three tabs and the table buttons; the map stays the renderer's.
SHELL_JS = r'''(()=>{
'use strict';
const map=window.scenarioMapView||null;
const tabs=[...document.querySelectorAll('#report-tabs [role=tab]')];
const panelOf=tab=>document.getElementById(tab.getAttribute('aria-controls'));
function activate(view,focus){
 const target=tabs.find(tab=>tab.dataset.view===view)||tabs[0];if(!target)return;
 for(const tab of tabs){const chosen=tab===target;tab.setAttribute('aria-selected',String(chosen));tab.tabIndex=chosen?0:-1;
  if(tab.dataset.view==='map')continue;
  const panel=panelOf(tab);if(!panel)continue;panel.hidden=!chosen;if(chosen)panel.removeAttribute('inert');else panel.setAttribute('inert','');}
 // The renderer hides its own panel and inspector, keeping selection, view, colour, filter and zoom.
 if(map){if(target.dataset.view==='map')map.showPanel();else map.hidePanel();}
 if(focus)target.focus();
}
tabs.forEach((tab,index)=>{
 tab.addEventListener('click',()=>activate(tab.dataset.view,false));
 tab.addEventListener('keydown',event=>{
  const step=event.key==='ArrowRight'?1:event.key==='ArrowLeft'?-1:0;
  const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:step?(index+step+tabs.length)%tabs.length:-1;
  if(next<0)return;event.preventDefault();activate(tabs[next].dataset.view,true);});
});
const mapPanel=document.getElementById('panel-map');
if(mapPanel){mapPanel.setAttribute('role','tabpanel');mapPanel.setAttribute('aria-labelledby','tab-map');}
if(map)map.onRequestMap(()=>activate('map',false));
document.addEventListener('click',event=>{
 const button=event.target&&event.target.closest?event.target.closest('[data-report-operation]'):null;
 if(!button||!map)return;
 event.preventDefault();map.selectNode(button.dataset.reportOperation);
});
})();'''


def _csp(scripts):
    hashes = ' '.join("'sha256-" + base64.b64encode(hashlib.sha256(script.encode('utf-8')).digest()).decode('ascii') + "'"
                      for script in scripts)
    return ("default-src 'none'; script-src " + hashes + "; style-src 'unsafe-inline'; font-src data:; img-src data:; "
            "base-uri 'none'; form-action 'none'")


def build_html(data):
    view = validate_report(data)
    model = data['map']
    hierarchy, notes = _presentation_hierarchy(data.get('hierarchy'), model)
    parts = scenario_map_parts(model, hierarchy, overlay=logging_overlay(data, view), embedded=True)
    names = {node['id']: node.get('name') or node['id'] for node in model['nodes']}
    body = (parts['body'].replace(EMBED_HEADER_SLOT, _header_html(data), 1)
            .replace(EMBED_PANELS_SLOT, _logging_panel(data, names, notes) + _improvements_panel(data, names), 1))
    scripts = (parts['script'], SHELL_JS)  # each hash below is computed from the exact final inline text
    return ('<!doctype html>\n<html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta http-equiv="Content-Security-Policy" content="' + html.escape(_csp(scripts), quote=True) + '">\n'
            '<meta name="color-scheme" content="light"><title>Логирование операций · ' + parts['title'] + '</title>\n'
            + parts['fonts'] + '<style>' + parts['style'] + REPORT_CSS + '</style></head>\n<body>' + body + parts['data']
            + ''.join('<script>' + script + '</script>' for script in scripts) + '</body></html>\n')


def write_report(data, output, *, source_artifacts=None, owned_output=False):
    validate_report(data)
    output = Path(output).absolute()
    if ((output.exists() and not owned_output) or any(p.is_symlink() for p in (output, *output.parents))
            or (owned_output and (not output.is_dir() or any(p.name != 'logging_review.checkpoint.json' for p in output.iterdir())))):
        raise LoggingReviewError('Report output must be a new directory without symbolic links')
    names = {n['id']: n['name'] for n in data['map']['nodes']}
    operation_csv = _csv(['operation_id', 'Операция', 'Состояние', 'Что не установлено'],
                         [[r['operation_id'], names[r['operation_id']], LABELS[r['state']], _missing_cell(r)] for r in data['operations']])
    has_placement=any(c.get('placement') is not None for c in data['changes'])
    change_csv = _csv(['Операции', 'Изменение', 'Зачем', 'Файл', 'Строка', 'Момент', 'Проверка']
                     + (['Точка и ветви'] if has_placement else []),
                     [[', '.join(c['operation_ids']), c['what'], c['why'], c['location']['path'],
                       c['location']['start_line'], c['timing'], c['verification']]
                       + ([_placement_description(c)] if has_placement else []) for c in data['changes']])
    files = {'logging_review.json': (canonical(data) + '\n').encode(), 'report.html': build_html(data).encode(),
             'operations.csv': operation_csv.encode('utf-8-sig'), 'logging_changes.csv': change_csv.encode('utf-8-sig'),
             'SOURCE-ARTIFACTS.json': (canonical(source_artifacts or {}) + '\n').encode()}
    files['SHA256SUMS'] = ''.join(hashlib.sha256(raw).hexdigest() + '  ' + name + '\n' for name, raw in sorted(files.items())).encode()
    if not owned_output: output.mkdir(parents=True, exist_ok=False)
    for name, raw in files.items():
        with (output / name).open('xb') as stream: stream.write(raw)
    verify_export(output)
    checkpoint = output/'logging_review.checkpoint.json'
    if owned_output and checkpoint.exists(): checkpoint.unlink()
    return {'status': data['status'], 'report': str(output/'report.html'), 'result': str(output/'logging_review.json'),
            'files': len(files), 'summary': data['summary'], 'interrupted': data.get('interrupted', False)}


def verify_export(folder):
    folder = Path(folder)
    lines = read_regular(folder/'SHA256SUMS').splitlines()
    expected = {'logging_review.json', 'report.html', 'operations.csv', 'logging_changes.csv', 'SOURCE-ARTIFACTS.json'}
    seen = set()
    for line in lines:
        sha, name = line.split('  ', 1)
        if name not in expected or name in seen: raise LoggingReviewError('Unexpected export checksum entry')
        seen.add(name)
        value = read_regular(folder/name)
        if hashlib.sha256(value.encode()).hexdigest() != sha: raise LoggingReviewError('Export checksum mismatch: ' + name)
    if seen != expected: raise LoggingReviewError('Incomplete export checksums')
    return True


def export_logging(run, output):
    source = Path(run)
    if source.is_dir(): source /= 'logging_review.json'
    raw = read_regular(source); data = strict_json_loads(raw)
    return write_report(data, output, source_artifacts={source.name: hashlib.sha256(raw.encode()).hexdigest()})
