"""One offline product document: map, logging, proposals and metric definitions."""
from copy import deepcopy
import hashlib
import html
from pathlib import Path

from .logging_contracts import LoggingReviewError, canonical
from . import logging_report as logging
from .map_presentation import EMBED_HEADER_SLOT, EMBED_PANELS_SLOT, scenario_map_parts
from .metrics import POLICIES, validate_metrics

LABELS = {'counting_unit': 'Единица счёта', 'cohort': 'Популяция', 'window': 'Окно',
          'time_semantics': 'Временные границы', 'repeat_policy': 'Повторы и дубликаты',
          'open_case_policy': 'Незавершённые случаи', 'late_data_policy': 'Поздние события'}
STATES = {'pending': 'ожидает запуска', 'running': 'выполняется', 'completed': 'завершён',
          'partial': 'частичный результат', 'failed': 'ошибка', 'skipped': 'пропущен',
          'cancelled': 'остановлен пользователем', 'not_requested': 'не запускался'}
STAGES = {'map': 'Карта', 'belka': 'Карта', 'logging': 'Логирование', 'improvements': 'Доработки',
          'metrics': 'Метрики', 'report': 'HTML'}
CSS = '''
.product-status{margin:8px 0;padding:10px 14px;background:var(--bg-subtle);border-radius:var(--radius-md);font-size:13px}
.product-status p{margin:3px 0;overflow-wrap:anywhere}.product-status ul{display:flex;flex-wrap:wrap;gap:8px 24px;margin:6px 0;padding-left:18px}
.metrics-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),1fr));gap:16px}
.metric-card{padding:18px;border:1px solid var(--border);border-radius:var(--radius-lg);background:var(--bg)}
.metric-card h3{margin:0 0 10px;font-size:17px}.metric-card p{overflow-wrap:anywhere}.metric-card dl{margin:12px 0}
.metric-card dt{font-size:12px;color:var(--ink-secondary);margin-top:9px}.metric-card dd{margin:3px 0;font-size:14px;overflow-wrap:anywhere}
.metric-card ul{padding-left:18px}.metric-links{display:flex;gap:6px 12px;flex-wrap:wrap}.metric-card button{min-height:36px}
.metric-state{font-size:12px;color:var(--ink-secondary)}.metric-formula{background:var(--bg-subtle);padding:10px;border-radius:var(--radius-md)}
.report-tabs button{min-height:44px}.report-tabs button:focus-visible,.metric-card button:focus-visible{outline:2px solid var(--accent-ink);outline-offset:2px}
@media(max-width:700px){.page-header{max-height:48dvh;overflow:auto;flex-shrink:0}.product-status{padding:8px 10px}.product-status ul{gap:3px 16px}.metric-card{padding:14px}.report-tabs{margin-bottom:4px}}
'''
EXTRA_JS = r'''(()=>{'use strict';document.addEventListener('click',event=>{
const link=event.target.closest&&event.target.closest('[data-product-change]');if(!link)return;
const tab=document.getElementById('tab-improvements');if(tab)tab.click();
const row=document.getElementById('change-'+link.dataset.productChange);if(row){row.tabIndex=-1;row.focus();row.scrollIntoView({block:'center',behavior:'auto'});}
});
const inspector=document.getElementById('inspector-content');
if(inspector)new MutationObserver(()=>{
 if(inspector.querySelector('.product-metric-links'))return;
 const selection=window.scenarioMapView?.captureState().selected;
 if(!selection||selection.type!=='node')return;
 const matching=[...document.querySelectorAll('[data-product-operations]')].filter(card=>{
  try{return JSON.parse(card.dataset.productOperations).includes(selection.id);}catch{return false;}
 });
 if(!matching.length)return;
 const section=document.createElement('section');section.className='details product-metric-links';
 const heading=document.createElement('h3');heading.textContent='Метрики этого действия';section.append(heading);
 for(const card of matching){const button=document.createElement('button');button.type='button';button.className='op-link';
  button.textContent=card.querySelector('h3').textContent;button.addEventListener('click',()=>{
   document.getElementById('tab-metrics').click();card.tabIndex=-1;card.focus();card.scrollIntoView({block:'start',behavior:'auto'});
  });section.append(button);}
 inspector.append(section);
}).observe(inspector,{childList:true,subtree:true});
})();'''


def _e(value):
    return html.escape('' if value is None else str(value), quote=True)


def _status(manifest, map_data):
    manifest = manifest or {}; scenario = (map_data or {}).get('scenario') or {}
    stages = manifest.get('stages') or {}
    if type(stages) is not dict: stages = {}
    parts = []
    for key, value in stages.items():
        value = value if type(value) is dict else {'status': value}
        state = value.get('status', 'pending')
        parts.append('<li><b>' + _e(STAGES.get(key, key)) + ':</b> ' + _e(STATES.get(state, state))
                     + (': ' + _e(value.get('reason') or value.get('error')) if value.get('reason') or value.get('error') else '') + '</li>')
    reason = manifest.get('reason') or manifest.get('stop_reason') or ''
    if type(reason) is dict: reason = reason.get('description') or reason.get('message') or ''
    return ('<section class="product-status" aria-label="Состояние полного запуска"><p><b>Постановка сценария на учёт</b>'
            + (' · ' + _e(STATES.get(manifest.get('status'), manifest.get('status'))) if manifest.get('status') else '') + '</p>'
            '<p>Единица экземпляра: ' + _e(scenario.get('instance_unit') or 'не установлена') + '</p>'
            + ('<ul>' + ''.join(parts) + '</ul>' if parts else '')
            + ('<p>' + _e(reason) + '</p>' if reason else '') + '</section>')


def _metric_tab():
    return '<button type="button" role="tab" id="tab-metrics" data-view="metrics" aria-controls="panel-metrics" aria-selected="false" tabindex="-1">Метрики</button>'


def _metrics_panel(catalog, names):
    cards = []
    facts = {f['id']: f for f in catalog['facts']}
    for metric in catalog['metrics']:
        deps = [facts[i] for i in metric['dependencies']]
        changes = list(dict.fromkeys(c for f in deps for c in f['change_ids']))
        cards.append('<article class="metric-card" data-product-operations="' + _e(canonical(metric['operation_ids'])) + '" id="metric-' + _e(metric['id']) + '"><h3>' + _e(metric['purpose']) + '</h3>'
                     '<p class="metric-state">Предложение определения · расчёт требует данных и согласованной методики</p>'
                     '<p class="metric-formula">' + _e(metric['formula_text']) + '</p><dl>'
                     + ''.join('<dt>' + label + '</dt><dd>' + _e(metric[key] or 'Не определено') + '</dd>' for key, label in LABELS.items())
                     + '<dt>Фактическое значение</dt><dd>' + _e(metric['value_reason']) + '</dd></dl>'
                     '<div class="metric-links">' + ''.join(logging._op_button(i, names) for i in metric['operation_ids']) + '</div>'
                     '<details><summary>Необходимые факты и ограничения</summary><ul>'
                     + ''.join('<li>' + _e(f['purpose']) + ' · ' + _e(logging.LABELS[f['logging_state']]) + '</li>' for f in deps)
                     + ''.join('<li>' + _e(v) + '</li>' for v in metric['readiness']['blockers']) + '</ul></details>'
                     + ('<p>Связанные предложения</p><div class="metric-links">' + ''.join(
                         '<button type="button" class="op-link" data-product-change="' + _e(i) + '">' + _e(i) + '</button>' for i in changes) + '</div>' if changes else '') + '</article>')
    errors = ''.join('<p class="report-note">' + _e(e) + '</p>' for e in catalog.get('errors', []))
    return ('<section id="panel-metrics" class="report-panel" role="tabpanel" aria-labelledby="tab-metrics" hidden inert>'
            '<h2>Определения метрик сценария</h2><p>Формулы предложены по карте и анализу логирования. Значения по исходному коду не вычисляются.</p>'
            + errors + '<div class="metrics-grid">' + ''.join(cards) + '</div>'
            + ('' if cards else '<p>Для определения метрик пока недостаточно действий и границ сценария.</p>') + '</section>')


def _document(parts, body, scripts):
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="' + _e(logging._csp(scripts)) + '">'
            '<meta name="color-scheme" content="light"><title>Сценарий на учёт · ' + parts['title'] + '</title>'
            + parts['fonts'] + '<style>' + parts['style'] + logging.REPORT_CSS + CSS + '</style></head><body>'
            + body + parts['data'] + ''.join('<script>' + s + '</script>' for s in scripts) + '</body></html>\n')


def build_product_html(logging_data, metrics, manifest=None):
    view = logging.validate_report(logging_data)
    validate_metrics(metrics, logging_data)
    model = logging_data['map']
    hierarchy, notes = logging._presentation_hierarchy(logging_data.get('hierarchy'), model)
    parts = scenario_map_parts(model, hierarchy, overlay=logging.logging_overlay(logging_data, view), embedded=True)
    names = {node['id']: node.get('name') or node['id'] for node in model['nodes']}
    header = logging._header_html(logging_data)
    # _header_html ends at the tablist close; insert only a static, trusted control.
    header = header[:-6] + _metric_tab() + '</div>'
    body = parts['body'].replace(EMBED_HEADER_SLOT, _status(manifest, model) + header, 1).replace(
        EMBED_PANELS_SLOT, logging._logging_panel(logging_data, names, notes) + logging._improvements_panel(logging_data, names)
        + _metrics_panel(metrics, names), 1)
    return _document(parts, body, (parts['script'], logging.SHELL_JS, EXTRA_JS))


def build_pending_html(map_data=None, hierarchy=None, manifest=None):
    """Publish all tabs after interruption, even before a map has been accepted."""
    model = deepcopy(map_data) if map_data is not None else {'schema_version': '2.0', 'nodes': [], 'edges': [],
            'scenario': {'title': 'Сценарий процесса'}, 'run': {'status': 'partial', 'reason': 'Карта ещё не опубликована'}}
    hierarchy, notes = logging._presentation_hierarchy(hierarchy, model)
    parts = scenario_map_parts(model, hierarchy, embedded=True)
    tabs = ''.join('<button type="button" role="tab" id="tab-' + key + '" data-view="' + key + '" aria-controls="panel-' + key
                   + '" aria-selected="' + ('true' if key == 'map' else 'false') + '"' + ('' if key == 'map' else ' tabindex="-1"')
                   + '>' + label + '</button>' for key, label in [('map', 'Карта'), ('logging', 'Логирование'), ('improvements', 'Доработки'), ('metrics', 'Метрики')])
    panels = ''.join('<section id="panel-' + key + '" class="report-panel" role="tabpanel" aria-labelledby="tab-' + key
                     + '" hidden inert><h2>' + label + '</h2><p>Результат этапа ещё не опубликован. Причина остановки указана в состоянии запуска.</p></section>'
                     for key, label in [('logging', 'Логирование'), ('improvements', 'Доработки'), ('metrics', 'Метрики')])
    body = parts['body'].replace(EMBED_HEADER_SLOT, _status(manifest, model) + '<div class="report-tabs" id="report-tabs" role="tablist" aria-label="Разделы отчёта">' + tabs + '</div>', 1).replace(EMBED_PANELS_SLOT, panels, 1)
    return _document(parts, body, (parts['script'], logging.SHELL_JS))


def write_product_report(logging_data, metrics, output, manifest=None):
    """Export to a new portable directory; all input objects remain unchanged."""
    page = build_product_html(logging_data, metrics, manifest)
    output = Path(output).absolute()
    if output.exists() or any(p.is_symlink() for p in (output, *output.parents)):
        raise LoggingReviewError('Product export requires a new directory without symlinks')
    files = {'report.html': page.encode('utf-8'), 'logging_review.json': canonical(logging_data).encode('utf-8'),
             'metric_catalog.json': canonical(metrics).encode('utf-8'), 'product_run.json': canonical(manifest or {}).encode('utf-8')}
    files['SHA256SUMS'] = ''.join(hashlib.sha256(raw).hexdigest() + '  ' + name + '\n' for name, raw in sorted(files.items())).encode('utf-8')
    output.mkdir(parents=True)
    for name, raw in files.items():
        with (output / name).open('xb') as stream: stream.write(raw)
    return {'report': str(output / 'report.html'), 'status': (manifest or {}).get('status', metrics['status']), 'files': len(files)}