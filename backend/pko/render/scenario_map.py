"""Standalone, offline graph of atomic operations in one scenario.

The renderer consumes the scenario-map contract, never ProgressModel. It does
not infer business facts or calculate readiness. User and model text is data:
the only dynamic HTML uses textContent; JSON cannot terminate its script tag.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from pko.observability.contracts import validate_report
from pko.render.dpss_theme import embedded_fonts
from pko.render.horizon_theme import horizon_tokens
from pko.render.process_report import REPORT_PANELS, REPORT_SCRIPT, REPORT_STYLE, REPORT_TABS
from pko.stages.contracts import validate_hierarchy


def _script_json(value: Any) -> str:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False)
            .replace("&", "\\u0026").replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def render_scenario_map(model: dict[str, Any], report: dict[str, Any] | None = None,
                        hierarchy: dict[str, Any] | None = None) -> str:
    """Return one self-contained HTML document without modifying ``model``.

    Unknown evidence or run statuses display as unknown, never as confirmed.
    Empty/partial models remain inspectable; facts are not created by rendering.
    A process report adds tabs only after it is validated against this map;
    business stages add the second view only for this exact map revision.
    """
    if not isinstance(model, dict):
        raise TypeError("scenario map must be a dictionary")
    if report is not None:
        validate_report(report, model)
    if hierarchy is not None:
        validate_hierarchy(hierarchy, model)
    scenario = model.get("scenario") or {}
    run = model.get("run") or {}
    title = str(scenario.get("title") or "Карта сценария")
    values = {
        "TITLE": html.escape(title, quote=True), "FONTS": embedded_fonts(),
        "STYLE": _STYLE + _NOTATION_STYLE + horizon_tokens() + _HORIZON_STYLE + _STAGE_STYLE + (REPORT_STYLE if report else ""),
        "SCRIPT": _SCRIPT.replace("/*__REPORT_JS__*/", REPORT_SCRIPT if report else ""),
        "MODEL": _script_json(model),
        "LEGACY_HIDDEN": "" if str(model.get("schema_version", "1.0")).split(".")[0] != "2" else " hidden",
        "RUN_NOTE": html.escape(str(run.get("reason") or ""), quote=True),
        "RUN_NOTE_HIDDEN": "" if run.get("reason") and run.get("status") in ("partial", "failed") else " hidden",
        "REPORT_TABS": REPORT_TABS if report else "",
        "REPORT_PANELS": REPORT_PANELS if report else "",
        "REPORT_DATA": (f'<script id="process-report" type="application/json">{_script_json(report)}</script>'
                        if report else "")
                       + (f'<script id="map-hierarchy" type="application/json">{_script_json(hierarchy)}</script>'
                          if hierarchy else ""),
    }
    # A single pass also keeps placeholder-like words in user text literal.
    return re.sub(r"__(TITLE|FONTS|STYLE|SCRIPT|MODEL|LEGACY_HIDDEN|RUN_NOTE|RUN_NOTE_HIDDEN|REPORT_TABS|REPORT_PANELS|REPORT_DATA)__",
                  lambda match: values[match.group(1)], _DOCUMENT)


_DOCUMENT = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light"><title>__TITLE__ · Белка</title>
__FONTS__<style>__STYLE__</style></head>
<body><a class="skip" href="#workspace">Перейти к карте</a>
<header class="page-header">
<div class="heading-line"><h1 id="page-title">__TITLE__</h1><div class="overview" id="overview"></div>
<div class="header-alerts" id="header-alerts" hidden><button id="status-toggle" type="button" aria-label="Предупреждения" title="Предупреждения" aria-expanded="false" aria-controls="status-panel"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path class="alert-triangle" d="M10.3 3.6a2 2 0 0 1 3.4 0l8.4 14.6A2 2 0 0 1 20.4 21H3.6a2 2 0 0 1-1.7-2.8z"/><path class="alert-mark" d="M12 9v5M12 17.2v.1"/></svg></button>
<section id="status-panel" class="map-popover status-panel" aria-label="Предупреждения" hidden><div class="header-status" id="header-status">
<p id="legacy-note" class="legacy-note"__LEGACY_HIDDEN__>Архивная карта прежней методологии. Её узлы и группировки не подтверждают атомарность по правилам версии 2.0.</p>
<p id="run-note" class="run-note"__RUN_NOTE_HIDDEN__>__RUN_NOTE__</p></div></section></div></div>
<p id="scenario-description" class="description"></p>__REPORT_TABS__</header>
<main id="workspace" tabindex="-1">
<div class="workspace-layout"><div class="main-panels">
<section id="panel-map" aria-label="Подробная карта сценария">
<div class="map-navigation"><div class="view-mode" id="view-mode" role="group" aria-label="Вид карты" hidden><button type="button" data-mode="atomic" aria-pressed="true" title="Каждая операция отдельной карточкой"><span class="mode-long">Атомарные операции</span><span class="mode-short">Операции</span></button><button type="button" data-mode="stages" aria-pressed="false" title="Операции объединены в бизнес-этапы"><span class="mode-long">Бизнес-этапы</span><span class="mode-short">Этапы</span></button></div><div class="stage-picker"><span id="stage-label">Этап</span><button type="button" id="stage-button" aria-haspopup="listbox" aria-expanded="false" aria-controls="stage-menu" aria-labelledby="stage-label stage-current"><span id="stage-current">Все этапы</span><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M6 9l6 6 6-6"/></svg></button><div id="stage-menu" class="stage-menu" role="listbox" aria-labelledby="stage-label"></div><select id="stage-select" aria-label="Выбрать этап сценария" hidden><option value="">Все этапы</option></select></div><button id="clear-selection" type="button" hidden>Снять выделение</button></div>
<section id="notation-panel" class="map-popover notation-panel" aria-label="Обозначения карты" hidden><h2>Как читать карту</h2><div id="notation-items"></div><p>Каждая карточка — отдельная операция. Стрелка показывает продолжение исполнения. Условия выбора следующих операций указаны в карточке исходной операции; на стрелках подписей нет.</p><p>Штриховая связь — вывод по коду, пунктирная — не установлена. Направление стрелки сохраняет возвраты и циклы.</p><p>Этап служит навигации. Выбор этапа выделяет его операции; выбор операции — все пути к ней и после неё. Это возможные маршруты карты. Смысл, внешнее взаимодействие и основания доступны в карточке.</p></section>
<div class="map-toolbar">
<div class="map-actions"><button type="button" id="zoom-out" aria-label="Уменьшить масштаб">−</button><output id="zoom-value" aria-label="Масштаб">100%</output><button type="button" id="zoom-in" aria-label="Увеличить масштаб">+</button><button type="button" id="fit">Вписать</button><button type="button" id="reset">Сбросить</button><button id="notation-toggle" type="button" aria-label="Обозначения" title="Обозначения" aria-expanded="false" aria-controls="notation-panel">?</button></div></div>
<div id="viewport" class="viewport" tabindex="0" aria-label="Карта сценария. Колесо мыши и pinch меняют масштаб относительно курсора; перетаскивание фона перемещает схему. Tab выбирает операции."><div id="canvas-size"><div id="stage"><svg id="connections" xmlns="http://www.w3.org/2000/svg" aria-label="Переходы между операциями"></svg><div id="nodes"></div></div></div><div id="empty-map" class="empty" hidden>Карта пока не содержит операций. Причина и незавершённые участки указаны в результатах исследования.</div></div>
</section>__REPORT_PANELS__
</div><aside id="inspector" class="inspector" aria-label="Описание выбранного элемента" hidden><div class="inspector-head"><span>Описание элемента</span><button type="button" id="close-inspector" aria-label="Закрыть описание">×</button></div><div id="inspector-content"></div></aside></div>
<p class="footnote">Карта восстановлена по переданным материалам. Подтверждение в коде не гарантирует поведение работающих систем и полноту бизнес-процесса.</p>
</main><div id="announcement" class="sr-only" role="status" aria-live="polite"></div>
<noscript><p class="empty">Для просмотра интерактивной карты включите JavaScript. Все данные находятся внутри этого HTML-файла; подключение к сети не требуется.</p></noscript>
<script id="scenario-model" type="application/json">__MODEL__</script>__REPORT_DATA__<script>__SCRIPT__</script></body></html>'''


_STYLE = r'''
:root{--ink:#263844;--muted:#5a6d7c;--border:#d0d7dd;--mint:#f1faf7;--purple:#7044b4;--pale-purple:#f5edfc;--white:#fff;--focus:#7044b4;--shadow:0 3px 12px rgba(45,58,65,.045)}
*{box-sizing:border-box}body{margin:0;background:var(--white);color:var(--ink);font-family:'SB Sans Display',Arial,sans-serif;font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}button,input{font:inherit}button{cursor:pointer}button:disabled{cursor:default;opacity:.45}button,input{min-height:44px}button:focus-visible,input:focus-visible,summary:focus-visible,a:focus-visible,[tabindex]:focus-visible{outline:3px solid var(--focus);outline-offset:3px}button{transition:border-color .15s,background .15s,box-shadow .15s}button:hover{border-color:var(--purple)}[hidden]{display:none!important}h1,h2,h3,p{margin-top:0}h1,h2,h3{font-weight:600;line-height:1.25}h1{font-size:clamp(25px,2.5vw,38px);margin-bottom:0;letter-spacing:-.025em;overflow-wrap:anywhere}h2{font-size:20px}h3{font-size:17px}.page-header,main{max-width:1900px;margin:0 auto;padding:0 32px}.page-header{padding-top:32px;padding-bottom:22px}.eyebrow{color:var(--muted);font-size:12px;font-weight:700;letter-spacing:.12em;margin-bottom:14px}.eyebrow span{color:#9ba9b0;padding:0 9px}.heading-line{display:flex;align-items:center;justify-content:space-between;gap:24px}.heading-line>.badge{flex-shrink:0}.description{margin:13px 0 16px;max-width:1100px;color:var(--muted);font-size:17px;white-space:pre-wrap;overflow-wrap:anywhere}.overview{display:flex;flex-wrap:wrap;gap:10px 26px;color:var(--muted);margin-bottom:14px}.overview b{color:var(--ink);font-size:18px;font-weight:600;margin-right:5px}.scope{max-width:1100px}.scope summary{cursor:pointer;color:var(--purple);width:fit-content;min-height:30px;font-size:14px;padding:2px 0}.scope-content{margin-top:12px;border:1px solid var(--border);border-radius:14px;padding:18px 20px;background:#fcfdfd}.scope-content .field{margin-bottom:12px}.scope-content .field:last-child{margin-bottom:0}.scope-content ul{margin:6px 0;padding-left:20px}.run-note{margin:15px 0 0;padding:10px 14px;border-radius:10px;background:#fff6e5;color:#7a5014;white-space:pre-wrap}.workspace-bar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:16px}.tabs{display:flex;gap:5px;flex-wrap:wrap;padding:5px;background:#f3f5f5;border-radius:12px}.tabs button{border:1px solid transparent;border-radius:9px;padding:8px 15px;background:transparent;color:var(--muted);font-weight:600}.tabs button[aria-selected=true]{background:#fff;color:var(--purple);box-shadow:0 2px 5px #162e3a0b}.search-label{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:13px}.search-label input{border:1px solid var(--border);border-radius:10px;padding:10px 12px;width:260px;background:#fff;color:var(--ink)}.workspace-layout{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:18px;align-items:start}.main-panels{min-width:0;border:1px solid var(--border);border-radius:16px;overflow:hidden;background:#fff}.map-toolbar{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;gap:12px;border-bottom:1px solid var(--border);flex-wrap:wrap}.map-heading{display:flex;flex-direction:column;gap:2px}.map-heading strong{font-size:16px;font-weight:600}.map-heading span{font-size:12px;color:var(--muted)}.map-actions{display:flex;align-items:center;gap:6px}.map-actions button{border:1px solid var(--border);background:#fff;border-radius:8px;padding:7px 12px;font-size:13px;min-width:40px}.map-actions #zoom-in,.map-actions #zoom-out{font-size:20px;padding:3px 11px}.map-actions output{font-size:12px;min-width:42px;text-align:center;color:var(--muted)}.viewport{position:relative;height:clamp(470px,64vh,850px);overflow:auto;background:var(--mint);background-image:radial-gradient(#cbded6 .7px,transparent .7px);background-size:20px 20px;overscroll-behavior:contain;scrollbar-color:#b5c9bf transparent}.viewport.dragging{cursor:grabbing;user-select:none}.viewport:focus-visible{outline-offset:-4px}#canvas-size{position:relative;min-width:100%;min-height:100%}#stage{position:absolute;top:0;left:0;transform-origin:top left}#connections,#lanes,#nodes{position:absolute;top:0;left:0;width:100%;height:100%}#connections{overflow:visible;z-index:2;pointer-events:none}#lanes{z-index:1;pointer-events:none}#nodes{z-index:3;pointer-events:none}.lane{position:absolute;left:18px;right:18px;border:1px solid #d2e2db;border-radius:14px;background:#ffffff60}.lane:nth-child(even){background:#edf7f2b3}.lane-title{position:absolute;left:18px;top:22px;width:158px;padding-right:10px;color:#3d6053;overflow-wrap:anywhere;font-size:15px;font-weight:600;line-height:1.35}.lane-caption{display:block;color:#60796e;font-size:12px;font-weight:400;margin-top:6px}.node{position:absolute;display:flex;flex-direction:column;gap:9px;pointer-events:auto;width:236px;padding:14px 15px;border:1px solid #b9c8c0;border-radius:13px;background:#fff;box-shadow:var(--shadow);text-align:left;color:var(--ink);line-height:1.4;overflow-wrap:anywhere}.node:hover{box-shadow:0 5px 20px #53337618}.node.selected{border:2px solid var(--purple);padding:13px 14px;box-shadow:0 0 0 3px #d1b1eb4a}.node.dimmed{opacity:.32}.node.search-match{border-color:var(--purple);box-shadow:0 0 0 3px #e2cff5}.node-type{font-size:11px;font-weight:600;letter-spacing:.02em;color:#6a4a90;display:flex;align-items:center;gap:6px}.node-type .symbol{display:inline-flex;align-items:center;justify-content:center;width:21px;height:21px;flex-shrink:0;border:1px solid #d8c6e8;border-radius:6px;background:var(--pale-purple);font-size:13px}.node-name{font-size:15px;font-weight:600}.node-status{font-size:10px;color:var(--muted);display:flex;align-items:center;gap:5px}.node-status:before{content:'';display:block;flex-shrink:0;width:6px;height:6px;border-radius:50%;background:#687880}.node-status.observed:before{background:#137668}.node-status.inferred:before{background:#956211}.node.kind-start,.node.kind-end{border-radius:23px}.node.kind-external{border-style:dashed}.node.kind-decision,.node.kind-fork,.node.kind-join{border-top:3px solid #af86cc}.node.kind-wait{border-top:3px solid #87aba0}.edge-visible{fill:none;stroke:#78958a;stroke-width:1.8}.edge-observed .edge-visible{stroke:#638d7c}.edge-inferred .edge-visible{stroke:#a18450;stroke-dasharray:7 4}.edge-unknown .edge-visible{stroke:#87969a;stroke-dasharray:3 5}.edge-hit{fill:none;stroke:transparent;stroke-width:17;pointer-events:stroke;cursor:pointer}.edge.selected .edge-visible{stroke:var(--purple);stroke-width:3}.edge.dimmed{opacity:.18}.edge:focus-visible{outline:none}.edge:focus-visible .edge-visible{stroke:var(--purple);stroke-width:4}.map-footer{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:13px 16px;flex-wrap:wrap;border-top:1px solid var(--border)}.legend{display:flex;flex-wrap:wrap;gap:7px}.badge{display:inline-block;padding:4px 8px;border-radius:6px;font-size:11px;font-weight:600;line-height:1.4;white-space:normal}.badge.observed{color:#155f53;background:#e3f3ed}.badge.inferred{color:#805619;background:#fff1d8}.badge.unknown{color:#53636e;background:#eef1f3}.badge.run-completed{color:#155f53;background:#e3f3ed}.badge.run-partial,.badge.run-running{color:#805619;background:#fff1d8}.badge.run-failed{color:#9b3148;background:#fbe9ee}.map-hint{font-size:11px;color:var(--muted)}.inspector{border:1px solid var(--border);border-radius:16px;background:#fff;min-height:400px;max-height:calc(64vh + 140px);overflow:auto;position:sticky;top:16px;box-shadow:var(--shadow)}#inspector-content{padding:22px 20px}.inspector-kicker{font-size:12px;color:var(--purple);font-weight:600;margin-bottom:12px}.inspector h2{margin:13px 0 16px;overflow-wrap:anywhere}.inspector .muted{color:var(--muted);font-size:14px}.field{margin:18px 0;overflow-wrap:anywhere}.field dt{font-size:12px;color:var(--muted);margin-bottom:5px}.field dd{margin:0;color:var(--ink);font-size:14px;white-space:pre-wrap}.field-list{margin:0}.details{border-top:1px solid #e3e8eb;padding-top:14px;margin-top:20px}.details summary{cursor:pointer;font-size:13px;color:var(--purple);padding:3px 0;min-height:36px}.evidence{padding:10px 12px;background:#f5f7f7;border-radius:8px;margin:10px 0;font-size:12px;overflow-wrap:anywhere}.evidence cite{font-style:normal;color:var(--muted);display:block;margin-bottom:6px}.evidence pre{white-space:pre-wrap;word-break:break-word;font-family:Consolas,monospace;font-size:11px;line-height:1.5;margin:6px 0}.related{display:flex;flex-direction:column;gap:7px}.text-button{width:100%;text-align:left;border:1px solid var(--border);border-radius:9px;background:#fff;padding:9px 11px;color:var(--purple);font-size:13px;white-space:normal;overflow-wrap:anywhere}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}caption{text-align:left;padding:20px;font-size:18px;font-weight:600}th{text-align:left;color:var(--muted);font-size:12px;background:#f8faf9;font-weight:600}td,th{padding:14px 18px;border-bottom:1px solid #e5e9e7;vertical-align:top}td{max-width:350px;white-space:pre-wrap;overflow-wrap:anywhere}td:first-child{min-width:190px;width:25%}td:nth-child(2){min-width:145px}td .text-button{border:0;background:transparent;padding:0;min-height:36px;color:var(--purple);font-weight:600}td .type-label{display:block;color:var(--muted);font-size:11px;margin-top:5px}.gaps-intro{padding:24px 24px 0}.gaps-intro p{color:var(--muted);font-size:14px;max-width:700px}#gaps{padding:0 24px 24px}.gap{border:1px solid var(--border);border-left:3px solid #caa267;border-radius:10px;padding:16px;margin-top:14px}.gap p{white-space:pre-wrap;overflow-wrap:anywhere;margin:0 0 10px}.gap .related{flex-direction:row;flex-wrap:wrap}.gap .text-button{width:auto}.empty{padding:40px 28px;color:var(--muted);max-width:700px;margin:0}.footnote{font-size:12px;color:var(--muted);padding:14px 0 26px;margin:0;max-width:1150px}.sr-only{position:absolute;width:1px;height:1px;padding:0;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.skip{position:absolute;top:-100px;left:20px;z-index:50;background:#fff;padding:12px;color:var(--purple)}.skip:focus{top:10px}
@media(max-width:1200px){.workspace-layout{grid-template-columns:minmax(0,1fr) 300px}.page-header,main{padding-left:22px;padding-right:22px}.search-label>span{display:none}.search-label input{width:225px}.tabs button{padding-left:10px;padding-right:10px;font-size:13px}.map-hint{display:none}}
.heading-badges{display:flex;gap:7px;align-items:center;flex-shrink:0;flex-wrap:wrap}.badge.demo{color:var(--purple);background:var(--pale-purple)}.scope .diagnostic-title{color:#805619;font-weight:600}.inspector-back{display:none;margin-bottom:16px}.gap-category{font-size:12px;color:#805619;font-weight:600;margin-bottom:8px}.checks-notice{border:0;border-bottom:1px solid var(--border);display:block;width:100%;text-align:left;padding:9px 18px;background:#fff8eb;color:#805619;font-size:12px;min-height:38px}.checks-notice:hover{background:#fff1d8}
@media(max-width:900px){.workspace-layout{grid-template-columns:1fr}.inspector{position:static;min-height:0;max-height:none}.workspace-bar{align-items:center;flex-direction:row}.search-label input{width:100%}.search-label{width:225px;flex-shrink:0}.heading-line{align-items:center;flex-direction:row;flex-wrap:wrap;gap:8px 20px}.heading-line h1{flex:1 1 380px}.page-header{padding-top:18px;padding-bottom:14px}.eyebrow{margin-bottom:8px}.description{margin:8px 0 10px;font-size:15px}.overview{margin-bottom:8px}.scope summary{min-height:26px}.heading-badges{gap:6px}.viewport{height:560px}.map-toolbar{padding:12px}.map-actions{margin-left:auto}.tabs{width:fit-content}.map-hint{display:block}.inspector-back{display:block}}
@media(max-width:700px){.workspace-bar{align-items:stretch;flex-direction:column}.search-label{width:100%}.heading-line h1{flex-basis:100%}}
@media(max-width:560px){.page-header,main{padding-left:14px;padding-right:14px}.tabs{width:100%;display:grid;grid-template-columns:1fr 1.4fr 1.8fr;gap:0}.tabs button{font-size:12px;padding:7px 5px;line-height:1.3}.description{font-size:15px}.viewport{height:480px}.map-actions{width:100%;justify-content:flex-end}.map-actions button{padding:6px 10px}.map-footer{padding:12px}.map-hint{display:none}.badge{font-size:10px}.inspector #inspector-content{padding:20px}.overview{gap:7px 16px;font-size:13px}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important;animation:none!important}}
/* Visual tokens and density follow the supplied DPSS reference. The content
   remains a scenario graph; no passport hierarchy or readiness data is used. */
:root{--ink:#000;--muted:#5A6D7C;--border:#D0D7DD;--mint:#F6FAF8;--purple:#8B3FD9;--accent:#C17DF7;--pale-purple:#F3E5FD;--focus:#8B3FD9;--shadow:none}
html,body{height:100%;overflow:hidden}body{height:100dvh;display:flex;flex-direction:column;font-size:14px}.page-header,main{max-width:none;width:100%;margin:0}.page-header{flex:none;padding:12px 24px 10px;border-bottom:1px solid var(--border)}.eyebrow{font-size:12px;letter-spacing:.025em;margin-bottom:4px}.eyebrow span{padding:0 6px}.heading-line{gap:8px 20px}.heading-line h1{font-size:22px;font-weight:700;line-height:1.2;letter-spacing:-.025em}.description{font-size:13px;margin:7px 0;line-height:1.45;max-width:1100px}.overview{gap:8px 20px;font-size:12px;margin-bottom:5px}.overview b{font-size:18px;margin-right:5px}.scope{position:relative;max-width:none}.scope summary{font-size:12px;min-height:24px;padding:2px 0}.scope-content{position:absolute;top:100%;left:0;width:min(920px,calc(100vw - 48px));max-height:65dvh;overflow:auto;z-index:30;margin-top:6px;background:#fff;border-radius:12px;padding:18px 24px;box-shadow:0 10px 30px #26384422}.scope-content .field dt{font-size:13px;font-weight:600;color:var(--ink)}.run-note{margin:6px 0 0;padding:6px 10px;font-size:12px;border-radius:8px;max-height:80px;overflow:auto}main{display:flex;flex-direction:column;flex:1;min-height:0;padding:0}.workspace-bar{flex:none;padding:8px 24px;margin:0;gap:16px;border-bottom:1px solid var(--border)}.tabs{padding:3px;border-radius:8px;background:#E9ECEF}.tabs button{border-radius:8px;font-size:13px;padding:6px 12px;min-height:38px;font-weight:400}.tabs button[aria-selected=true]{font-weight:600;box-shadow:none}.search-label input{min-height:38px;border-radius:8px;font-size:13px;padding:7px 10px}.workspace-layout{display:block;position:relative;flex:1;min-height:0;gap:0}.main-panels{height:100%;border:0;border-radius:0;overflow:hidden}.main-panels>section{height:100%;overflow:auto}#panel-map{position:relative;display:flex;flex-direction:column;overflow:hidden}.map-toolbar{display:contents}.map-heading{display:none}.map-actions{position:absolute;left:16px;bottom:64px;z-index:4;background:#fff;border:1px solid var(--border);padding:4px;border-radius:15px;gap:0;box-shadow:0 2px 8px #1c2b400a;width:auto;margin:0}.map-actions button{border:0;border-radius:8px;min-width:40px;min-height:42px;padding:7px 11px;background:transparent}.map-actions button:hover{background:var(--pale-purple)}.map-actions output{min-width:52px;font-size:13px;font-weight:600;color:#183454}.map-actions #fit{border-left:1px solid var(--border);border-radius:0 8px 8px 0}.viewport{height:auto;flex:1;min-height:0;background:var(--mint);background-image:none;scrollbar-color:#b7c2cb transparent}.map-footer{flex:none;padding:9px 24px;border-top:1px solid var(--border);gap:8px}.map-hint{font-size:11px}.legend{gap:7px}.badge{border-radius:8px;font-size:11px;font-weight:400;padding:4px 8px;color:var(--ink)}.badge.observed,.badge.run-completed{background:#CFF5F3;color:var(--ink)}.badge.inferred,.badge.run-partial,.badge.run-running{background:#FFF2CC;color:var(--ink)}.badge.unknown{background:#E9ECEF;color:var(--muted)}.badge.demo{background:#F3E5FD;color:var(--purple)}.badge.run-failed{background:#FBD6E1;color:var(--ink)}.lane{background:#ffffff28;border-color:#dfe7e2;border-radius:12px}.lane:nth-child(even){background:#ffffff52}.lane-title{color:var(--muted);font-size:13px;font-weight:400}.lane-caption{color:#92A2B0;font-size:12px}.node{width:224px;min-height:88px;padding:12px 16px;gap:7px;border-color:#B7C2CB;border-radius:12px;box-shadow:none;color:var(--ink)}.node:hover{border-color:var(--accent);box-shadow:0 8px 20px -12px #c17df799}.node.selected{padding:12px 16px;border:1px solid var(--accent);background:rgba(193,125,247,.2);box-shadow:none}.node.dimmed{opacity:.2}.node.dimmed:hover{opacity:.8}.node.search-match{border-color:var(--accent);box-shadow:0 0 0 2px #c17df72b}.node-type{color:var(--muted);font-size:11px;font-weight:400;letter-spacing:0}.node-type .symbol{display:none}.node-name{font-size:15px;line-height:1.35}.node-status{font-size:11px}.node.kind-start,.node.kind-end{border-radius:20px}.node.kind-decision,.node.kind-fork,.node.kind-join,.node.kind-wait{border-top-width:1px;border-color:#B7C2CB}.edge-visible,.edge-observed .edge-visible{stroke:#B7C2CB;stroke-width:1.4}.edge-inferred .edge-visible{stroke:#9b8b73}.edge-unknown .edge-visible{stroke:#92A2B0}.edge.selected .edge-visible{stroke:var(--accent);stroke-width:2}.inspector{position:absolute;right:0;top:0;bottom:0;width:480px;max-width:100%;min-height:0;max-height:none;border:0;border-left:1px solid #B7C2CB;border-radius:0;display:flex;flex-direction:column;overflow:hidden;z-index:6;background:#fff;box-shadow:none}.inspector-head{flex:none;display:flex;align-items:center;justify-content:space-between;padding:4px 18px;border-bottom:1px solid var(--border);font-size:12px;color:var(--muted)}.inspector-head button{border:0;background:#fff;min-width:40px;min-height:40px;font-size:24px;color:var(--muted);padding:0}#inspector-content{flex:1;min-height:0;overflow:auto;padding:18px 24px 28px}.inspector h2{font-size:20px;font-weight:700;line-height:1.2;margin:7px 0 16px}.inspector-kicker{font-size:12px;color:var(--muted);font-weight:400;margin-bottom:7px}.field{margin:0 0 16px}.field dt{font-size:13px;color:var(--ink);font-weight:600;margin-bottom:6px}.field dd{font-size:13px;line-height:1.48}.inspector .muted{font-size:13px}.inspector-back{display:none!important}.details{margin-top:16px;padding-top:12px}.details summary{font-size:12px}.text-button{border-radius:8px;font-size:13px}.footnote{display:none}.checks-notice{flex:none;padding:6px 24px;min-height:32px;font-size:12px}
@media(max-width:900px){.workspace-bar{flex-direction:row;align-items:center}.search-label{width:205px;flex-shrink:0}.heading-line h1{flex:1 1 380px}.tabs{width:fit-content}.map-hint{display:none}.inspector{width:420px}.map-actions{width:auto;justify-content:flex-start}}
@media(max-width:700px){.page-header{padding:10px 16px 7px}.heading-line h1{font-size:18px;flex-basis:100%}.description{font-size:12px;margin:5px 0}.heading-badges{gap:5px}.overview{gap:5px 14px;margin-bottom:3px}.overview b{font-size:16px}.scope-content{width:calc(100vw - 32px);max-height:60dvh;padding:16px 20px}.workspace-bar{padding:6px 12px;gap:8px;flex-wrap:wrap}.tabs{width:auto;display:flex;flex:1;gap:1px}.tabs button{font-size:11px;padding:5px 8px;min-height:36px}.search-label{width:150px}.search-label input{font-size:12px;min-height:36px}.inspector{width:100%}.map-footer{padding:7px 12px}.badge{font-size:10px}.legend{gap:4px}.map-actions{left:12px;bottom:60px}.map-actions button{padding:6px 9px}.checks-notice{padding:6px 16px}.inspector #inspector-content{padding:16px 20px 24px}}
.description{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
.node-system{font-size:11px;line-height:1.35;color:var(--muted)}
@media(max-width:480px){.workspace-bar{flex-wrap:nowrap}.search-label{width:36px;position:relative;overflow:visible}.search-label:before{content:'';position:absolute;width:10px;height:10px;border:1.5px solid var(--muted);border-radius:50%;left:10px;top:10px;z-index:1;pointer-events:none}.search-label:after{content:'';position:absolute;width:6px;border-top:1.5px solid var(--muted);transform:rotate(45deg);left:19px;top:22px;z-index:1;pointer-events:none}.search-label:focus-within:before,.search-label:focus-within:after{display:none}.search-label input{width:36px;padding:6px;transition:none}.search-label input:focus{position:absolute;right:0;top:-18px;width:min(300px,calc(100vw - 32px));z-index:8;background:#fff}.search-label input::placeholder{color:transparent}.search-label input:focus::placeholder{color:var(--muted)}.tabs button{padding:5px 6px;font-size:10px}.map-footer{padding-left:10px;padding-right:10px}.legend .badge{font-size:9px;padding:4px 6px}}
@media print{.page-header,main{padding:0}.workspace-bar,.map-actions,.map-hint,.skip{display:none}.workspace-layout{display:block}.inspector{position:static;max-height:none;margin-top:18px}.viewport{height:650px}.main-panels{break-inside:avoid}.scope:not([open]){display:none}.footnote{padding-bottom:0}}
'''


_NOTATION_STYLE = r'''
.stage-picker{display:flex;align-items:center;gap:9px;flex:1;min-width:140px;max-width:330px;font-size:12px;color:var(--muted);padding:3px 8px 3px 12px;background:#f5f7f6;border:1px solid var(--border);border-radius:9px}
.stage-picker select{font:inherit;font-size:13px;min-width:0;width:100%;min-height:32px;border:0;background:transparent;color:var(--ink);cursor:pointer}.stage-picker select:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:4px}
.node.in-scope .node-surface>*{stroke:var(--purple);fill:#faf4ff;stroke-width:1.7}.node.selected .node-surface>*{fill:#ecdcfc;stroke-width:2.5}.edge.in-scope .edge-visible{stroke:var(--purple);stroke-width:2}
.notation-item{display:flex;gap:12px;align-items:center;margin:12px 0;font-size:12px}.notation-item svg{flex:none;width:24px;height:24px;color:var(--muted)}
@media(max-width:1000px) and (min-width:701px){.workspace-bar{flex-wrap:wrap}.stage-picker{max-width:none}.search-label{width:180px}}
@media(max-width:700px){.stage-picker{width:100%;max-width:none;flex:none}}
.map-navigation{display:flex;align-items:center;gap:10px;flex:none;padding:5px 24px;border-bottom:1px solid var(--border);background:#fff;min-height:44px;font-size:12px}
.map-navigation button{border:1px solid var(--border);background:#fff;border-radius:8px;padding:4px 10px;min-height:32px;color:var(--muted);white-space:nowrap}
.map-navigation button[aria-expanded=true]{color:var(--purple);background:var(--pale-purple);border-color:var(--accent)}
#notation-toggle{margin-left:auto}
.map-popover{position:absolute;top:48px;left:16px;width:310px;max-width:calc(100% - 32px);max-height:calc(100% - 110px);overflow:auto;z-index:5;background:#fff;border:1px solid var(--border);border-radius:12px;box-shadow:0 8px 24px #26384418;padding:16px}
.map-popover h2{font-size:16px;margin:0 0 8px}.map-popover p{font-size:12px;color:var(--muted);margin-bottom:12px}.notation-panel{left:auto;right:16px;width:360px}
.node,.node.selected,.node.kind-start,.node.kind-end,.node.kind-decision,.node.kind-fork,.node.kind-join,.node.kind-wait,.node.kind-external{border:0;background:transparent;box-shadow:none;padding:14px 16px;isolation:isolate;overflow:visible}
.node:hover,.node.search-match{box-shadow:none}.node:focus-visible{outline:3px solid var(--focus);outline-offset:4px}
.node-surface{position:absolute;inset:0;width:100%;height:100%;z-index:-1;overflow:visible;pointer-events:none}.node-surface>*{fill:#fff;stroke:#9baab4;stroke-width:1.3;vector-effect:non-scaling-stroke}
.node:hover .node-surface>*,.node.search-match .node-surface>*{stroke:var(--purple);stroke-width:2}.node.selected .node-surface>*{stroke:var(--purple);stroke-width:2;fill:#f7edff}
.node.kind-external .node-surface>*{stroke-dasharray:5 4}.node-content{display:flex;flex-direction:column;gap:7px;width:100%;position:relative}.node.kind-decision{width:280px;padding:0;justify-content:center;align-items:center}.node.kind-decision .node-content{width:140px;text-align:center;align-items:center}
.node.kind-start,.node.kind-end{width:208px;padding:17px 22px}.node-type .symbol{display:inline-flex;width:20px;height:20px;border:0;background:transparent;border-radius:0}.symbol svg{width:20px;height:20px;display:block}.node-type{gap:6px;font-size:11px}.node-group{font-size:11px;color:var(--purple);line-height:1.35}.node-system{font-size:11px;color:var(--muted)}
.node-gap{font-size:11px;color:#805619}.node-status{font-size:11px}.node.kind-fork .node-type,.node.kind-join .node-type{font-weight:600}.edge-visible,.edge-observed .edge-visible{stroke:#8b9ca7}
.group-links{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 16px}.group-links .text-button{width:auto;font-size:12px;padding:6px 8px;min-height:32px}.group-member{display:block;margin-top:7px}.type-label.group-label{color:var(--purple)}
.map-navigation button:hover{border-color:var(--purple);color:var(--purple)}
@media(max-width:700px){.workspace-bar{flex-direction:column;align-items:stretch;flex-wrap:nowrap}.tabs{width:100%;min-width:0;flex:none}.tabs button{flex:1;min-width:0}.search-label{width:100%;min-width:0}.search-label input{width:100%}.map-navigation{padding:5px 12px;gap:6px}.map-navigation button{font-size:11px;padding:4px 7px}.map-popover{top:47px}.node{touch-action:manipulation}#clear-selection{font-size:0;padding:4px 8px}#clear-selection:after{content:'Сброс';font-size:11px}}
@media(max-height:500px){.description{display:none}.page-header{padding-top:6px;padding-bottom:5px}.heading-line h1{font-size:18px}.map-footer{padding-top:5px;padding-bottom:5px}}
@media(max-width:480px){.search-label:before,.search-label:after{display:none}.search-label input:focus{position:static;width:100%}.search-label input::placeholder{color:var(--muted)}}
@media print{.map-navigation,.map-popover{display:none}}

.overview .findings-toggle{border:0;background:transparent;color:var(--muted);padding:0;min-height:24px;font:inherit;white-space:nowrap}.overview .findings-toggle:hover{color:var(--purple);text-decoration:underline}
.legacy-note{margin:6px 0 0;padding:8px 12px;background:#fff6e5;color:#795017;font-size:12px;border-radius:8px}.continuation{padding:12px 0;border-top:1px solid var(--border)}.continuation-target{font-weight:700;color:var(--purple)}.continuation-condition{font-size:13px;font-weight:400;color:var(--ink);margin:8px 0 0;white-space:pre-wrap}.external-interaction h4{font-size:14px;margin:14px 0 8px}.external-interaction+.external-interaction{border-top:1px solid var(--border)}
.operation-meta{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:10px}.operation-meta .inspector-kicker{margin:6px 0 0}.operation-meta .group-links{justify-content:flex-end;margin:0 0 0 auto;max-width:70%;min-width:0}.operation-meta .group-links .text-button{text-align:right;max-width:100%}
.code-explanation{margin:14px 0 0}.code-explanation+.code-explanation{border-top:1px solid var(--border);padding-top:12px}.source-function{display:inline-block;padding:3px 0;min-height:28px;color:var(--purple);font-size:13px;font-weight:600;text-decoration:underline;text-underline-offset:3px;overflow-wrap:anywhere}.code-meaning{font-size:13px;line-height:1.5;margin:8px 0 0;white-space:pre-wrap}.code-fragment,.source-fragment pre{background:#f5f7f7;border-radius:8px;padding:10px 12px;font:11px/1.5 Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;margin:8px 0}.source-fragment cite{display:block;color:var(--muted);font-size:11px;font-style:normal;overflow-wrap:anywhere;margin-top:8px}
.heading-line{align-items:center;flex-wrap:nowrap}.heading-line h1{flex:1;min-width:0}
.node-group{color:#687580}.node-system{color:#243F62}
.heading-line .overview{margin:0;flex-shrink:0;justify-content:flex-end;gap:8px 18px;flex-wrap:nowrap}.overview>span{white-space:nowrap}
.map-actions{bottom:16px}.map-navigation .stage-picker{max-width:330px;flex:0 1 330px;min-width:140px}
@media(max-width:900px){.heading-line{flex-wrap:wrap;gap:6px}.heading-line h1{flex-basis:100%}.heading-line .overview{justify-content:flex-start}}
@media(max-width:700px){.map-navigation .stage-picker{width:auto;flex:1;min-width:0}.map-actions{bottom:16px}.heading-line .overview{gap:5px 14px}}
'''


# Horizon layer: comes after tokens.css, so the earlier rules read Horizon values
# through the legacy variable names; only appearance changes, never content.
_HORIZON_STYLE = r'''
/* The DPSS purple replaces Horizon's green accent; green stays only on the
   success pill ("Подтверждено кодом"), which reads --success. */
:root{--accent:#8B3FD9;--accent-hover:#7A32C4;--accent-soft:#F3E5FD;--accent-ink:#7044B4;--accent-a08:rgba(139,63,217,.08);--accent-a16:rgba(139,63,217,.16);--accent-a24:rgba(139,63,217,.24);--accent-a40:rgba(139,63,217,.40);--accent-a64:rgba(139,63,217,.64);--focus-ring:#8B3FD9;--on-accent:#fff}
:root{--muted:var(--ink-secondary);--border:var(--line-default);--mint:var(--bg-subtle);--purple:var(--accent-ink);--pale-purple:var(--accent-soft);--white:var(--bg);--focus:var(--focus-ring);--shadow:none}
body{background:var(--bg);color:var(--ink);font-family:'SB Sans Display',var(--font-sans)}
button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible,a:focus-visible,[tabindex]:focus-visible,.node:focus-visible{outline:2px solid var(--focus-ring);outline-offset:2px}
button{transition:background var(--duration-base) var(--ease-standard),color var(--duration-base) var(--ease-standard)}
button:hover{border-color:transparent}
.page-header{border-bottom:0;padding:16px 24px 12px}
.heading-line h1{font-weight:500;letter-spacing:-.025em}
.overview b{font-weight:300;letter-spacing:-.02em;color:var(--ink)}
.overview,.description{color:var(--ink-secondary)}
.overview .findings-toggle:hover{color:var(--accent-ink)}
.scope summary,.details summary{color:var(--accent-ink)}
.scope-content{border:0;border-radius:var(--radius-xl);background:var(--bg-raised);box-shadow:var(--shadow-md)}
.run-note,.legacy-note{border-radius:var(--radius-lg);background:var(--warning-soft);color:var(--warning)}
.workspace-bar,.map-navigation{border-bottom:0;background:var(--bg)}
.tabs{background:var(--bg-subtle);border-radius:var(--radius-pill);padding:4px}
.tabs button{border:0;border-radius:var(--radius-pill);color:var(--ink-secondary);padding:6px 16px}
.tabs button:hover{background:var(--bg-muted);color:var(--ink)}
.tabs button[aria-selected=true]{background:var(--bg-inverse);color:var(--ink-inverse);font-weight:500}
.search-label input{border:0;border-radius:var(--radius-pill);background:var(--bg-subtle);padding:7px 16px}
.search-label input:hover{background:var(--bg-muted)}
.stage-picker{border:0;border-radius:var(--radius-pill);background:var(--bg-subtle);padding:3px 12px 3px 16px}
.map-navigation button,.map-actions button{border:0;border-radius:var(--radius-pill);background:var(--bg-subtle);color:var(--ink)}
.map-navigation button:hover,.map-actions button:hover{background:var(--bg-muted);color:var(--ink)}
.map-navigation button[aria-expanded=true]{background:var(--bg-inverse);color:var(--ink-inverse)}
.checks-notice{border-bottom:0;background:var(--warning-soft);color:var(--warning)}
.checks-notice:hover{background:var(--bg-muted)}
.viewport{background:var(--bg-subtle);scrollbar-color:var(--line-control) transparent}
.lane{border:0;border-radius:var(--radius-xl);background:var(--bg)}
.lane:nth-child(even){background:color-mix(in srgb,var(--bg) 55%,transparent)}
.lane-title{color:var(--ink-secondary);font-weight:500}
.lane-caption{color:var(--ink-tertiary)}
.node-surface>*{fill:var(--bg-raised);stroke:var(--line-default);stroke-width:1}
.node:hover .node-surface>*,.node.search-match .node-surface>*{stroke:var(--accent);stroke-width:1.5}
.node.in-scope .node-surface>*{stroke:var(--accent);fill:var(--accent-a08);stroke-width:1.5}
.node.selected .node-surface>*{stroke:var(--accent);fill:var(--accent-soft);stroke-width:2}
.node.terminal-success{--terminal-color:var(--success);--terminal-soft:var(--success-soft)}
.node.terminal-negative{--terminal-color:var(--danger);--terminal-soft:var(--danger-soft)}
.node.terminal-success .node-surface,.node.terminal-negative .node-surface{filter:drop-shadow(0 0 7px color-mix(in srgb,var(--terminal-color) 42%,transparent))}
.node.terminal-success .node-surface>*,.node.terminal-negative .node-surface>*{stroke:var(--terminal-color);stroke-width:1.8}
.node.terminal-success.selected .node-surface>*,.node.terminal-negative.selected .node-surface>*{fill:var(--terminal-soft);stroke-width:2.5}
.node-terminal{font-size:11px;font-weight:500;line-height:1.35;color:var(--ink-secondary)}
.terminal-success .node-terminal,.terminal-negative .node-terminal{color:var(--terminal-color)}
.node-type,.node-system,.node-group,.node-status{color:var(--ink-tertiary)}
.node-status.observed:before{background:var(--success)}
.node-name{font-weight:500;letter-spacing:-.01em}
.node-gap,.gap-category,.scope .diagnostic-title{color:var(--warning)}
.edge-visible,.edge-observed .edge-visible{stroke:var(--line-control);stroke-width:1.2}
.edge-inferred .edge-visible{stroke:var(--warning)}
.edge-unknown .edge-visible{stroke:var(--ink-disabled)}
.edge.selected .edge-visible,.edge.in-scope .edge-visible{stroke:var(--accent);stroke-width:2}
#arrow path{fill:var(--line-control)}
.map-actions{border:0;border-radius:var(--radius-pill);background:var(--bg-raised);box-shadow:var(--shadow-md);padding:4px;gap:4px}
.map-actions button{min-height:40px}
.map-actions #fit{border-left:0;border-radius:var(--radius-pill)}
.map-actions output{color:var(--ink);font-weight:500}
.map-footer{border-top:0}
.badge{border-radius:var(--radius-pill);padding:4px 10px;font-weight:500}
.badge.observed,.badge.run-completed{background:var(--success-soft);color:var(--success)}
.badge.inferred,.badge.run-partial,.badge.run-running{background:var(--warning-soft);color:var(--warning)}
.badge.unknown{background:var(--bg-muted);color:var(--ink-secondary)}
.badge.demo{background:var(--accent-soft);color:var(--accent-ink)}
.badge.run-failed{background:var(--danger-soft);color:var(--danger)}
.map-popover{border:0;border-radius:var(--radius-xl);background:var(--bg-raised);box-shadow:var(--shadow-lg);padding:20px}
.map-popover h2,.inspector h2{font-weight:500;letter-spacing:-.02em}
.inspector{border-left:0;background:var(--bg-raised);box-shadow:var(--shadow-lg)}
.inspector-head{border-bottom:0;color:var(--ink-tertiary)}
.inspector-head button{background:transparent;border-radius:var(--radius-pill)}
.inspector-head button:hover{background:var(--bg-muted);color:var(--ink)}
.inspector-kicker,.field dt{color:var(--ink-secondary)}
.field dt{font-weight:500}
.details,.continuation,.code-explanation+.code-explanation,.external-interaction+.external-interaction{border-color:var(--line-hairline)}
.text-button{border:0;border-radius:var(--radius-pill);background:var(--bg-subtle);color:var(--accent-ink)}
.text-button:hover{background:var(--bg-muted)}
.group-links .text-button{padding:6px 12px}
td .text-button{background:transparent}
th{background:var(--bg-subtle);color:var(--ink-secondary);font-weight:500}
td,th{border-bottom-color:var(--line-hairline);font-variant-numeric:tabular-nums}
.gap{border:0;border-radius:var(--radius-xl);background:var(--bg-subtle)}
.evidence,.code-fragment,.source-fragment pre{background:var(--bg-subtle);border-radius:var(--radius-md)}
.evidence pre,.code-fragment,.source-fragment pre{font-family:var(--font-mono)}
.continuation-target,.source-function{color:var(--accent-ink)}
.skip{border-radius:var(--radius-pill);background:var(--bg-inverse);color:var(--ink-inverse)}
.header-alerts{position:relative;flex:none}
#status-toggle{display:flex;align-items:center;justify-content:center;width:40px;min-height:40px;padding:0;border:0;border-radius:var(--radius-pill);background:transparent}
#status-toggle:hover,#status-toggle[aria-expanded=true]{background:var(--warning-soft)}
#status-toggle svg{width:24px;height:24px}
.alert-triangle{fill:#f6c343;stroke:#d9a21b;stroke-width:1;stroke-linejoin:round}
.alert-mark{fill:none;stroke:var(--ink);stroke-width:2;stroke-linecap:round}
.status-panel{top:calc(100% + 8px);left:auto;right:0;width:380px;max-width:calc(100vw - 32px);max-height:60dvh;padding:12px;z-index:30}
.header-status{display:flex;flex-direction:column;gap:6px}
.header-status .run-note,.header-status .legacy-note,.header-status .checks-notice{display:block;width:100%;margin:0;padding:10px 14px;border:0;border-radius:var(--radius-lg);background:var(--warning-soft);color:var(--warning);font-size:13px;line-height:1.45;text-align:left;max-height:none;white-space:pre-wrap}
.header-status .checks-notice{min-height:40px}
.header-status .checks-notice:hover{background:var(--bg-muted);color:var(--ink)}
#panel-map{position:relative}
.map-navigation{position:absolute;top:12px;left:16px;z-index:5;min-height:0;padding:4px;gap:4px;border-radius:var(--radius-pill);background:var(--bg-raised);box-shadow:var(--shadow-md);max-width:calc(100% - 32px)}
.map-navigation .stage-picker{flex:0 1 300px;min-width:140px}
.stage-picker{position:relative;padding:3px 4px 3px 16px;font-size:12px}
#stage-button{display:flex;align-items:center;gap:8px;flex:1;min-width:0;min-height:32px;padding:4px 10px;border:0;border-radius:var(--radius-pill);background:transparent;color:var(--ink);font-size:13px;font-weight:500;text-align:left;cursor:pointer}
#stage-button:hover:not(:disabled),#stage-button[aria-expanded=true]{background:var(--bg-muted)}
#stage-current{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#stage-button svg{flex:none;width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;transition:transform var(--duration-base) var(--ease-emphasized)}
#stage-button[aria-expanded=true] svg{transform:rotate(180deg)}
.stage-menu{position:absolute;top:calc(100% + 8px);left:0;z-index:20;min-width:100%;width:max-content;max-width:min(420px,calc(100vw - 32px));max-height:min(360px,60dvh);overflow:auto;padding:6px;border-radius:var(--radius-xl);background:var(--bg-raised);box-shadow:var(--shadow-lg);
 opacity:0;visibility:hidden;pointer-events:none;transform:translateY(-6px) scale(.98);transform-origin:top left;
 transition:opacity 160ms var(--ease-standard),transform 240ms var(--ease-emphasized),visibility 0s linear 240ms}
.stage-menu.open{opacity:1;visibility:visible;pointer-events:auto;transform:none;transition:opacity 180ms var(--ease-standard),transform 280ms var(--ease-emphasized),visibility 0s}
.stage-menu .stage-option{display:flex;align-items:center;width:100%;min-height:36px;padding:8px 12px;border:0;border-radius:var(--radius-md);background:transparent;color:var(--ink);font-size:13px;font-weight:400;line-height:1.4;letter-spacing:.005em;text-align:left;white-space:normal;cursor:pointer;transition:background var(--duration-fast) var(--ease-standard)}
.stage-menu .stage-option:hover,.stage-menu .stage-option:focus-visible{background:var(--bg-muted);outline:none}
.stage-menu .stage-option[aria-selected=true]{background:var(--accent-soft);color:var(--accent-ink);font-weight:500}
body{text-rendering:optimizeLegibility;-moz-osx-font-smoothing:grayscale;font-optical-sizing:auto}
.map-actions{left:16px;right:auto;bottom:16px}
#notation-toggle{margin-left:0;min-width:40px;font-weight:500}
#notation-toggle[aria-expanded=true]{background:var(--bg-inverse);color:var(--ink-inverse)}
.notation-panel{top:auto;left:16px;right:auto;bottom:72px;max-height:calc(100% - 100px);transform-origin:bottom left}
@media(max-width:700px){.status-panel{position:fixed;top:56px;left:16px;right:16px;width:auto;max-width:none}
.map-navigation{position:static;max-width:none;padding:6px 8px;border-radius:0;box-shadow:none;background:var(--bg)}.map-navigation .stage-picker{flex:1}.map-actions{left:8px;bottom:8px;right:auto}.notation-panel{left:8px;bottom:64px;width:calc(100% - 16px)}}
'''


# Business-stage view: the mode switch, stage blocks and the stage panel.
_STAGE_STYLE = r'''
.view-mode{display:flex;flex:none;gap:2px;padding:2px;border-radius:var(--radius-pill);background:var(--bg-subtle)}
.map-navigation .view-mode button{min-height:32px;padding:4px 12px;font-size:13px;background:transparent;color:var(--ink-secondary)}
.map-navigation .view-mode button:hover{background:var(--bg-muted);color:var(--ink)}
.map-navigation .view-mode button[aria-pressed=true]{background:var(--bg-inverse);color:var(--ink-inverse);font-weight:500}
.mode-short{display:none}
.node.kind-stage{width:256px;padding:14px 22px 20px 16px}
.node.kind-stage .node-surface>.stage-stack{fill:var(--bg-subtle)}
.node-stage-kicker{font-size:11px;font-weight:500;color:var(--accent-ink)}
.node-stage-result{font-size:12px;line-height:1.4;color:var(--ink-secondary);display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;overflow:hidden}
.stage-outcomes{display:flex;flex-wrap:wrap;gap:4px}
.stage-outcome{font-size:11px;line-height:1.5;padding:1px 8px;border-radius:var(--radius-pill);background:var(--bg-muted);color:var(--ink-secondary)}
.stage-outcome.success{background:var(--success-soft);color:var(--success)}
.stage-outcome.negative{background:var(--danger-soft);color:var(--danger)}
.node .stage-chip{align-self:flex-start;padding:1px 8px;border-radius:var(--radius-pill);background:var(--accent-soft);color:var(--accent-ink)}
.stage-proposal{margin:2px 0 4px}
.stage-end.success .continuation-condition{color:var(--success)}.stage-end.negative .continuation-condition{color:var(--danger)}
.stage-switch{margin-top:16px}
.stage-frame rect{fill:color-mix(in srgb,var(--accent-soft) 40%,var(--bg));stroke:var(--accent);stroke-width:1.4;stroke-dasharray:6 4;vector-effect:non-scaling-stroke}
.stage-frame.in-scope rect,.stage-frame.selected rect{stroke-dasharray:none;stroke-width:2}
.stage-frame.dimmed,.stage-frame-head.dimmed{opacity:.35}
.stage-frame-head{position:absolute;pointer-events:auto;display:flex;flex-direction:column;gap:4px}
.stage-frame-bar{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.stage-frame-title{display:flex;flex-direction:column;gap:3px;min-height:0;padding:2px 4px;border:0;border-radius:var(--radius-md);background:transparent;color:var(--ink);text-align:left}
.stage-frame-title:hover{background:transparent}.stage-frame-title:hover .stage-frame-name{color:var(--accent-ink);text-decoration:underline;text-underline-offset:3px}
.stage-frame-name{font-size:16px;font-weight:600;line-height:1.3}
.stage-frame-close{flex:none;min-height:32px;padding:4px 12px;border:1px solid var(--line-default);border-radius:var(--radius-pill);background:var(--bg-raised);color:var(--ink);font-size:12px}
.stage-frame-close:hover{background:var(--bg-muted);border-color:var(--line-default)}
.stage-frame-result{margin:0;padding:0 4px;max-width:720px;font-size:12px;line-height:1.4;color:var(--ink-secondary)}
@media(max-width:700px){.mode-long{display:none}.mode-short{display:inline}.map-navigation .view-mode button{padding:4px 9px;font-size:12px}}
'''


_GRAPH_LAYOUT_JS = r'''
function layoutScenarioGraph(nodes, edges, sizes, vertical=false, entryIds=[]) {
 const key=value=>String(value),adj=new Map(nodes.map(n=>[key(n.id),[]]));
 const valid=edges.filter(e=>adj.has(key(e.source))&&adj.has(key(e.target)));
 for(const e of valid)if(e.kind!=='data'&&e.kind!=='retry')adj.get(key(e.source)).push(key(e.target));
 let counter=0;const indices=new Map(),low=new Map(),stack=[],inStack=new Set(),components=[];
 function visit(id){indices.set(id,counter);low.set(id,counter++);stack.push(id);inStack.add(id);for(const to of adj.get(id)){if(!indices.has(to)){visit(to);low.set(id,Math.min(low.get(id),low.get(to)));}else if(inStack.has(to))low.set(id,Math.min(low.get(id),indices.get(to)));}if(low.get(id)===indices.get(id)){const group=[];let member;do{member=stack.pop();inStack.delete(member);group.push(member);}while(member!==id);components.push(group.reverse());}}
 for(const node of [...nodes.filter(n=>entryIds.map(key).includes(key(n.id))||n.kind==='start'),...nodes])if(!indices.has(key(node.id)))visit(key(node.id));
 const owner=new Map();components.forEach((group,i)=>group.forEach(id=>owner.set(id,i)));const out=components.map(()=>new Set()),indegree=components.map(()=>0),base=components.map(()=>0);
 for(const [from,tos] of adj)for(const to of tos){const a=owner.get(from),b=owner.get(to);if(a!==b&&!out[a].has(b)){out[a].add(b);indegree[b]++;}}
 const queue=components.map((_,i)=>i).filter(i=>indegree[i]===0);for(let at=0;at<queue.length;at++){const i=queue[at];for(const j of out[i]){base[j]=Math.max(base[j],base[i]+components[i].length);if(--indegree[j]===0)queue.push(j);}}
 const rank=new Map();components.forEach((group,i)=>group.forEach((id,j)=>rank.set(id,base[i]+j)));
 const groups=new Map(),centers=new Map();for(const node of nodes){const id=key(node.id),r=rank.get(id)||0;if(!groups.has(r))groups.set(r,[]);groups.get(r).push(id);}
 const levels=[...groups.keys()].sort((a,b)=>a-b),secondarySize=id=>vertical?sizes.get(id).w:sizes.get(id).h;
 for(const level of levels){let cursor=0;for(const id of groups.get(level)){centers.set(id,cursor+secondarySize(id)/2);cursor+=secondarySize(id)+38;}for(const id of groups.get(level))centers.set(id,centers.get(id)-(cursor-38)/2);}
 const incoming=new Map(nodes.map(n=>[key(n.id),[]])),outgoing=new Map(nodes.map(n=>[key(n.id),[]]));
 for(const e of valid)if(e.kind!=='data'){incoming.get(key(e.target)).push(key(e.source));outgoing.get(key(e.source)).push(key(e.target));}
 for(let pass=0;pass<4;pass++){const forward=pass%2===0;for(const level of forward?levels:[...levels].reverse()){
  const group=groups.get(level),desired=new Map();for(const id of group){const neighbors=(forward?incoming:outgoing).get(id).filter(other=>forward?rank.get(other)<level:rank.get(other)>level).map(other=>centers.get(other));desired.set(id,neighbors.length?neighbors.reduce((sum,n)=>sum+n,0)/neighbors.length:centers.get(id));}
  group.sort((a,b)=>desired.get(a)-desired.get(b));let cursor=-Infinity;for(const id of group){const size=secondarySize(id),top=Math.max(desired.get(id)-size/2,cursor);centers.set(id,top+size/2);cursor=top+size+38;}const shift=group.reduce((sum,id)=>sum+desired.get(id)-centers.get(id),0)/group.length;for(const id of group)centers.set(id,centers.get(id)+shift);
 }}
 // Long jumps thread through rank gaps; only cycles need the outer channel.
 const outerEdges=new Set(valid.filter(e=>rank.get(key(e.target))-rank.get(key(e.source))!==1).map(e=>key(e.id)));
 const backward=valid.filter(e=>key(e.source)!==key(e.target)&&rank.get(key(e.target))<=rank.get(key(e.source))).length;
 const outerSpace=backward?80+backward*24:0,positions=new Map();
 if(!nodes.length)return {rank,positions,outerEdges,width:600,height:400};
 const secondaryMin=Math.min(...nodes.map(n=>centers.get(key(n.id))-secondarySize(key(n.id))/2)),secondaryMax=Math.max(...nodes.map(n=>centers.get(key(n.id))+secondarySize(key(n.id))/2));
 // A backward edge approaches its target from before the first rank; reserve that lead so curves stay on the canvas.
 let primary=32+(backward?24:0);
 for(const level of levels){const group=groups.get(level),primarySize=Math.max(...group.map(id=>vertical?sizes.get(id).h:sizes.get(id).w));for(const id of group){const size=sizes.get(id),secondary=32+centers.get(id)-secondarySize(id)/2-secondaryMin+(vertical?0:outerSpace);positions.set(id,{x:vertical?secondary:primary,y:vertical?primary:secondary,w:size.w,h:size.h});}primary+=primarySize+(vertical?58:82);}
 const crossSize=secondaryMax-secondaryMin+64+outerSpace;let width=vertical?crossSize:primary-82+32,height=vertical?primary-58+32:crossSize;
 if(valid.some(e=>key(e.source)===key(e.target))){if(vertical)height+=70;else width+=70;}
 return {rank,positions,outerEdges,width,height};
}
function smoothScenarioPath(points) {
 // Shared tangents give continuous cubic curves, including long outer routes.
 // Bound handles by adjacent free corridors, so smoothing cannot cut a rank.
 const tangent=points.map((p,i)=>{const before=points[Math.max(0,i-1)],after=points[Math.min(points.length-1,i+1)];
  const left=Math.hypot(p.x-before.x,p.y-before.y),right=Math.hypot(after.x-p.x,after.y-p.y);
  const dx=(left?(p.x-before.x)/left:0)+(right?(after.x-p.x)/right:0),dy=(left?(p.y-before.y)/left:0)+(right?(after.y-p.y)/right:0),length=Math.hypot(dx,dy)||1;
  const handle=Math.min(32,(left&&right?Math.min(left,right):Math.max(left,right))/3);
  return {x:dx/length*handle,y:dy/length*handle};});
 const segments=points.slice(1).map((end,i)=>{const start=points[i];return [start,{x:start.x+tangent[i].x,y:start.y+tangent[i].y},{x:end.x-tangent[i+1].x,y:end.y-tangent[i+1].y},end];});
 return {segments,path:`M ${points[0].x} ${points[0].y} `+segments.map(([,c1,c2,end])=>`C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${end.x} ${end.y}`).join(' ')};
}
function routeScenarioOuterEdge(edge,layout,vertical,index=0,anchors={}) {
 const a=layout.positions.get(String(edge.source)),b=layout.positions.get(String(edge.target));
 const sourceRank=layout.rank.get(String(edge.source)),targetRank=layout.rank.get(String(edge.target));
 // Leave the occupied rank through its free gutter before entering the outer
 // channel. A single Bezier from a lower branch's top crosses its upper sibling.
 const trailing=rank=>Math.max(...[...layout.positions].filter(([id])=>layout.rank.get(id)===rank).map(([,p])=>vertical?p.y+p.h:p.x+p.w));
 let points,label;
 const sa=anchors.source??(vertical?a.x+a.w/2:a.y+a.h/2),sb=anchors.target??(vertical?b.x+b.w/2:b.y+b.h/2),pb=(anchors.targetEdge??(vertical?b.y:b.x))-3;
 if(vertical){const channel=layout.width-28-index*24,from=trailing(sourceRank)+24,to=b.y-24;points=[{x:sa,y:a.y+a.h},{x:sa,y:from},{x:channel,y:from},{x:channel,y:to},{x:sb,y:to},{x:sb,y:pb}];label={x:channel-9,y:(from+to)/2};}
 else{const channel=28+index*24,from=trailing(sourceRank)+28,to=b.x-28;points=[{x:a.x+a.w,y:sa},{x:from,y:sa},{x:from,y:channel},{x:to,y:channel},{x:to,y:sb},{x:pb,y:sb}];label={x:(from+to)/2,y:channel-9};}
 return {...smoothScenarioPath(points),label,outer:true};
}
function scenarioColumns(layout,vertical) {
 // Rank envelopes along the flow (p) with their cards sorted across it (s).
 if(layout.columns)return layout.columns;
 const columns=new Map();
 for(const [id,p] of layout.positions){const r=layout.rank.get(id),box=vertical?{p0:p.y,p1:p.y+p.h,s0:p.x,s1:p.x+p.w}:{p0:p.x,p1:p.x+p.w,s0:p.y,s1:p.y+p.h};
  const column=columns.get(r)||{lead:Infinity,trail:-Infinity,cards:[]};column.lead=Math.min(column.lead,box.p0);column.trail=Math.max(column.trail,box.p1);column.cards.push(box);columns.set(r,column);}
 for(const column of columns.values())column.cards.sort((a,b)=>a.s0-b.s0);
 return layout.columns=columns;
}
function routeScenarioForwardEdge(edge,layout,vertical,port=0,bend=0,used=new Map(),anchors={}) {
 // Straight runs through each rank's free gap, S-curves across the gutters
 // between ranks: a long jump threads past intervening cards instead of
 // detouring around the whole graph. Every tangent points along the flow.
 const a=layout.positions.get(String(edge.source)),b=layout.positions.get(String(edge.target));
 const from=layout.rank.get(String(edge.source)),to=layout.rank.get(String(edge.target)),columns=scenarioColumns(layout,vertical);
 const limit=(vertical?layout.width:layout.height)-6,point=(p,s)=>vertical?{x:s,y:p}:{x:p,y:s};
 // An anchor moves an end along the card border, e.g. to an operation's row inside an open stage.
 const sa=(anchors.source??(vertical?a.x+a.w/2:a.y+a.h/2))+port,sb=(anchors.target??(vertical?b.x+b.w/2:b.y+b.h/2))+port;
 // targetEdge ends the line at a card inside the target box, after a straight run from its border.
 const pa=vertical?a.y+a.h:a.x+a.w,pb=(anchors.targetEdge??(vertical?b.y:b.x))-3,lanes=[...columns.keys()].filter(r=>r>from&&r<to).sort((x,y)=>x-y);
 const waypoints=[[pa,sa]];if(columns.get(from).trail>pa+1)waypoints.push([columns.get(from).trail,sa]);
 let current=sa;
 for(const r of lanes){const column=columns.get(r),cards=column.cards,gaps=[[6,cards[0].s0-12]];
  for(let i=1;i<cards.length;i++)gaps.push([cards[i-1].s1+10,cards[i].s0-10]);gaps.push([cards[cards.length-1].s1+12,limit]);
  const middle=(column.lead+column.trail)/2,desired=sa+(sb-sa)*(middle-pa)/((pb-pa)||1);
  // Keep the previous height while it fits, so a jump stays a straight line.
  const open=gaps.map((gap,index)=>({gap,index})).filter(({gap})=>gap[1]>=gap[0]);
  const fit=open.find(({gap})=>current>=gap[0]&&current<=gap[1]),choice=fit||open.reduce((best,item)=>{const distance=g=>Math.max(g[0]-desired,0,desired-g[1]);return distance(item.gap)<distance(best.gap)?item:best;});
  const slot=r+':'+choice.index,count=used.get(slot)||0;used.set(slot,count+1);
  const base=fit?current:Math.min(choice.gap[1],Math.max(choice.gap[0],desired)),shift=fit?0:(count%2?1:-1)*Math.ceil(count/2)*8;
  current=Math.min(choice.gap[1],Math.max(choice.gap[0],base+shift));waypoints.push([column.lead,current],[column.trail,current]);}
 if(anchors.targetEdge!==undefined&&columns.get(to).lead<pb-1)waypoints.push([columns.get(to).lead,sb]);
 waypoints.push([pb,sb]);
 const lengths=waypoints.slice(1).map(([p],i)=>p-waypoints[i][0]),handles=waypoints.map((_,i)=>Math.min(i?lengths[i-1]:Infinity,i<lengths.length?lengths[i]:Infinity)/2);
 const segments=lengths.map((_,i)=>{const [p0,s0]=waypoints[i],[p1,s1]=waypoints[i+1];return [point(p0,s0),point(p0+handles[i],s0),point(p1-handles[i+1],s1),point(p1,s1)];});
 const path=`M ${segments[0][0].x} ${segments[0][0].y} `+segments.map(([,c1,c2,end])=>`C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${end.x} ${end.y}`).join(' ');
 const [s,c1,c2,e]=segments[Math.floor(segments.length/2)];
 return {segments,path,label:{x:(s.x+3*c1.x+3*c2.x+e.x)/8,y:(s.y+3*c1.y+3*c2.y+e.y)/8-9},outer:false};
}
function routeScenarioEdge(edge,layout,vertical,state,port=0,bend=0,anchors={}) {
 if(layout.rank.get(String(edge.target))>layout.rank.get(String(edge.source)))return routeScenarioForwardEdge(edge,layout,vertical,port,bend,state.gaps||(state.gaps=new Map()),anchors);
 const index=state.back||0;state.back=index+1;
 return routeScenarioOuterEdge(edge,layout,vertical,index,anchors);
}
'''


# Pure function, also run by the Node tests: business stages over the atomic graph.
_STAGE_PROJECTION_JS = r'''
function projectScenarioStages(nodes, edges, stages, membership, open=[]) {
 // A collapsible stage becomes one block; every other operation stays a card.
 // An open block shows its operations inside its frame. Each link keeps the
 // transitions it stands for, so no path is invented: a collapsible stage has
 // one entry from which all its members are reachable.
 const key=value=>String(value),collapsed=new Map(),owner=membership||{},opened=new Set(open.map(key));
 for(const stage of stages)if(stage&&stage.collapsible)collapsed.set(key(stage.id),stage);
 const block=id=>{const stage=owner[key(id)];return stage!==undefined&&stage!==null&&collapsed.has(key(stage))?key(stage):null;};
 // outer: the item placed on the stage map; represent: the card that shows the operation.
 const outer=id=>{const stage=block(id);return stage===null?key(id):'stage:'+stage;};
 const represent=id=>{const stage=block(id);return stage===null||opened.has(stage)?key(id):'stage:'+stage;};
 const items=[],members=new Map();
 for(const node of nodes){const id=outer(node.id);
  if(!members.has(id)){members.set(id,[]);items.push(id.startsWith('stage:')?{id,type:'stage',stage:collapsed.get(id.slice(6)),members:members.get(id),open:opened.has(id.slice(6))}:{id,type:'node',node});}
  members.get(id).push(key(node.id));}
 const links=new Map(),strength={observed:0,inferred:1,unknown:2};
 for(const edge of edges){const source=represent(edge.source),target=represent(edge.target),outerSource=outer(edge.source),outerTarget=outer(edge.target);
  if(source===target&&source.startsWith('stage:'))continue;
  const pair=JSON.stringify([source,target]);
  if(!links.has(pair))links.set(pair,{id:'link:'+links.size,source,target,outerSource,outerTarget,
   inner:outerSource===outerTarget&&outerSource.startsWith('stage:')?outerSource:null,transitions:[]});
  links.get(pair).transitions.push(edge);}
 const projected=[...links.values()].map(link=>{const kinds=new Set(link.transitions.map(edge=>edge.kind));
  const status=link.transitions.map(edge=>strength[edge.evidence_status]===undefined?'unknown':edge.evidence_status).reduce((weakest,value)=>strength[value]>strength[weakest]?value:weakest,'observed');
  return {...link,kind:kinds.size===1?[...kinds][0]:'sequence',evidence_status:status};});
 return {items,links:projected,represent,outer};
}
'''


_SCRIPT = r'''
(() => {
'use strict';
const model = JSON.parse(document.getElementById('scenario-model').textContent);
const array = value => Array.isArray(value) ? value : [];
const text = value => value === null || value === undefined ? '' : typeof value === 'string' ? value : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
const plural = (count, forms) => {const n=Math.abs(count)%100,last=n%10;return forms[n>=11&&n<=19?2:last===1?0:last>=2&&last<=4?1:2];};
const $ = id => document.getElementById(id);
const el = (tag, cls, value) => { const e=document.createElement(tag); if(cls)e.className=cls; if(value!==undefined)e.textContent=text(value); return e; };
const svg = (tag, attributes={}) => { const e=document.createElementNS('http://www.w3.org/2000/svg',tag); for(const [key,value] of Object.entries(attributes))e.setAttribute(key,String(value)); return e; };
const scenario = model.scenario || {};
const legacy = text(model.schema_version||'1.0').split('.')[0]!=='2';
const nodes = array(model.nodes).filter(n => n && typeof n==='object');
const edges = array(model.edges).filter(e => e && typeof e==='object');
const systems = array(legacy?model.systems:[]).filter(s => s && typeof s==='object');
const gaps = array(model.gaps);
const omissions = array(model.source_omissions);
const validationIssues = array(model.validation?.issues);
const openFindings = gaps.length + omissions.length + validationIssues.length;
const executionReviews=array(model.execution_reviews),operationsByNode=new Map();
for(const review of executionReviews)for(const operation of array(review.operations)){const id=text(operation.node_id);if(!operationsByNode.has(id))operationsByNode.set(id,[]);operationsByNode.get(id).push({operation,review});}
const byId = new Map(nodes.map(n => [text(n.id),n]));
const systemById = new Map(systems.map(s => [text(s.id),s]));
const groups=array(model.groups).filter(g=>g&&typeof g==='object'),groupById=new Map(groups.map(g=>[text(g.id),g]));
const groupMembers=group=>nodes.filter(n=>text(n.group_id)===text(group.id)||(group.kind==='stage'&&text(groupById.get(text(n.group_id))?.parent_id)===text(group.id)));
// Legacy reports may contain an operation between a node and its stage.
const nodeGroups=node=>{const group=groupById.get(text(node.group_id));return group?[groupById.get(text(group.parent_id)),group].filter(Boolean):[];};
const nodeSearchText=node=>[node.name,node.description,node.input,node.output,...nodeGroups(node).map(g=>g.name)].map(text).join(' ').toLocaleLowerCase('ru');
// Business stages are a model proposal over this exact map revision; membership is their only link to it.
const hierarchy=(()=>{const source=$('map-hierarchy');if(!source)return null;try{return JSON.parse(source.textContent);}catch(error){return null;}})();
const stageRecords=array(hierarchy?.stages).filter(stage=>stage&&typeof stage==='object');
const membership=hierarchy?.membership&&typeof hierarchy.membership==='object'?hierarchy.membership:{};
const businessStageById=new Map(stageRecords.map(stage=>[text(stage.id),stage]));
const nodeStage=node=>businessStageById.get(text(membership[text(node.id)]));
const stageMembers=stage=>nodes.filter(node=>text(membership[text(node.id)])===text(stage.id));
let viewMode=new URLSearchParams(location.hash.slice(1)).get('view')==='stages'&&stageRecords.length?'stages':'atomic',display=null;
// Stage ids opened on the stage view and the frames currently drawn for them.
const expandedStages=new Set(),frameElements=new Map();
const kindNames={start:'Начало',action:'Действие',decision:'Условие',fork:'Параллельное выполнение',join:'Объединение',wait:'Ожидание',end:'Завершение',external:'Внешний участок'};
const operationKinds={action:'Действие',validation:'Проверка',read:'Чтение данных',write:'Запись данных',integration:'Вызов системы',publish:'Публикация события',wait:'Ожидание',calculation:'Расчёт',transform:'Преобразование данных',error_handling:'Обработка отказа / ошибки'};
const outcomeNames={success:'Успешное завершение',refusal:'Бизнес-отказ',failure:'Технический сбой',cancelled:'Отмена',deferred:'Отложенная обработка',no_decision:'Решение не принято',unknown:'Завершение · исход не уточнён'};
const terminalIds=new Set(array(scenario.terminal_operation_ids).map(text));
function terminalPresentation(node){
 const terminal=legacy?node.kind==='end':terminalIds.has(text(node.id));
 if(!terminal)return null;
 // A dangling operation is not a terminal; names and technical execution status do not determine the business outcome.
 const outcome=Object.prototype.hasOwnProperty.call(outcomeNames,node.outcome)?node.outcome:'unknown';
 const tone=outcome==='success'?'success':['refusal','failure','cancelled'].includes(outcome)?'negative':'neutral';
 const label=node.completion_condition?'При завершении: '+outcomeNames[outcome]:
  outcome==='success'||outcome==='unknown'?outcomeNames[outcome]:'Завершение · '+outcomeNames[outcome].toLocaleLowerCase('ru');
 return {tone,label,outcome,conditional:!!node.completion_condition};
}
function stageOutcomes(members){
 const outcomes=new Map();
 for(const node of members){const end=terminalPresentation(node);if(!end)continue;const key=end.outcome+':'+end.conditional;
  if(!outcomes.has(key))outcomes.set(key,{...end,count:0});outcomes.get(key).count++;}
 return [...outcomes.values()];
}
function stageSemantics(members,edges){
 const incoming=new Map(),outgoing=new Map(),rows=[];
 for(const edge of edges){if(edge.kind==='data')continue;const source=String(edge.source),target=String(edge.target);outgoing.set(source,(outgoing.get(source)||0)+1);incoming.set(target,(incoming.get(target)||0)+1);}
 for(const node of members){const id=String(node.id);
  if((outgoing.get(id)||0)>1){const mode=node.continuation_mode||'sequence';
   const labels={choice:['Выбор продолжения','Выполняется операция, условие которой выполнено.'],all:['Совместный запуск','Запускаются все указанные продолжения при выполнении их условий.'],sequence:['Порядок продолжений','Порядок продолжения указан в условиях переходов.']};
   const [short,label]=labels[mode]||['Продолжение не установлено','Способ выбора продолжения не установлен.'];rows.push({node,dimension:'continuation',mode,short,label});}
  if((incoming.get(id)||0)>1){const mode=node.join_mode||'any',all=mode==='all';
   rows.push({node,dimension:'join',mode,short:all?'Ожидание всех ветвей':'Вход по любой ветви',label:all?'Операция ожидает завершения всех предусмотренных входящих ветвей.':'Операция может начаться по любой входящей ветви; ожидание всех ветвей не требуется.'});}
 }
 return rows;
}
const nodeKindLabel=node=>!legacy?'Атомарная операция':node.kind==='end'&&node.outcome?(outcomeNames[node.outcome]||outcomeNames.unknown):node.kind==='action'?(operationKinds[operationsByNode.get(text(node.id))?.[0]?.operation.kind]||kindNames.action):(kindNames[node.kind]||'Шаг сценария');
function notationIcon(kind){
 const icon=svg('svg',{viewBox:'0 0 24 24','aria-hidden':'true',focusable:'false',fill:'none',stroke:'currentColor','stroke-width':1.6,'stroke-linecap':'round','stroke-linejoin':'round'});
 if(kind==='start'||kind==='end'){icon.append(svg('circle',{cx:12,cy:12,r:9}));if(kind==='end')icon.append(svg('circle',{cx:12,cy:12,r:6}));}
 else if(kind==='decision')icon.append(svg('path',{d:'M12 2 L22 12 L12 22 L2 12 Z'}));
 else if(kind==='wait'){icon.append(svg('circle',{cx:12,cy:12,r:9}),svg('path',{d:'M12 6 V12 L16 15'}));}
 else if(kind==='fork'||kind==='join'){icon.append(svg('path',{d:kind==='fork'?'M3 12 H10 M10 4 V20 M10 5 H21 M10 12 H21 M10 19 H21':'M3 5 H14 M3 12 H14 M3 19 H14 M14 4 V20 M14 12 H21'}));}
 else if(kind==='stage')icon.append(svg('rect',{x:7,y:8,width:14,height:12,rx:3}),svg('rect',{x:3,y:4,width:14,height:12,rx:3,fill:'#fff'}));
 else if(kind==='external')icon.append(svg('rect',{x:3,y:3,width:18,height:18,rx:3,'stroke-dasharray':'3 3'}),svg('path',{d:'M8 16 L16 8 M9 8 H16 V15'}));
 else icon.append(svg('rect',{x:3,y:5,width:18,height:14,rx:3}));return icon;
}
const edgeNames={sequence:'Далее',branch:'Ветвление',call:'Вызов',event:'Сообщение',return:'Ответ',retry:'Повтор',error:'Ошибка',data:'Передача данных'};
const evidenceNames={observed:'Подтверждено кодом',inferred:'Вывод по коду',unknown:'Не установлено'};
const evidenceStatus = value => Object.prototype.hasOwnProperty.call(evidenceNames,value) ? value : 'unknown';
const stateNames={running:'Исследование выполняется',completed:'Исследование завершено',partial:'Исследование не завершено',failed:'Исследование прервано'};
const run=model.run || {}; const runState=Object.prototype.hasOwnProperty.call(stateNames,run.status)?run.status:'unknown';
const badge = value => el('span','badge '+evidenceStatus(value),evidenceNames[evidenceStatus(value)]);
const systemName = node => (systemById.get(text(node.system_id)) || {}).name || 'АС не установлена';
const validEdges=edges.filter(e => byId.has(text(e.source)) && byId.has(text(e.target)));
const positions=new Map(), nodeButtons=new Map(), edgeGroups=new Map();
let selected=null, zoom=1, graphWidth=1000, graphHeight=600, restoreFocus=null, inspectorView={type:'overview'};

$('page-title').textContent=scenario.title || 'Карта сценария';
const fullDescription=text(scenario.description).trim();
// The header names the variant briefly; the submitted description, possibly structured text, stays in the overview.
const briefDescription=scenario.start&&scenario.end?text(scenario.start)+' → '+text(scenario.end):/^[\[{]/.test(fullDescription)?'':fullDescription.split('\n')[0];
$('scenario-description').textContent=briefDescription?(briefDescription.length>260?briefDescription.slice(0,257)+'…':briefDescription):'Описание выбранного варианта сценария не задано.';
for(const [count,label,inspect] of [[nodes.length,plural(nodes.length,legacy?['узел','узла','узлов']:['операция','операции','операций'])],[validEdges.length,plural(validEdges.length,['переход','перехода','переходов'])],[gaps.length,plural(gaps.length,['пробел','пробела','пробелов']),gaps.length>0]]){
 const entry=el(inspect?'button':'span',inspect?'findings-toggle':'');if(inspect){entry.type='button';entry.setAttribute('aria-label','Посмотреть пробелы исследования');entry.addEventListener('click',showFindings);}entry.append(el('b','',count),document.createTextNode(label)); $('overview').append(entry);
}
function field(container,label,value){ if(!text(value).trim())return; const dl=el('dl','field');dl.append(el('dt','',label),el('dd','',value));container.append(dl); }
function listField(container,label,values){ const items=array(values);if(!items.length)return;const section=el('div','field');section.append(el('div','muted',label));const ul=el('ul');for(const item of items)ul.append(el('li','',item));section.append(ul);container.append(section); }
if(run.reason&&(runState==='partial'||runState==='failed')){$('run-note').textContent=text(run.reason);$('run-note').hidden=false;}
if(validationIssues.length||omissions.length){const notice=el('button','checks-notice');notice.type='button';notice.textContent=[omissions.length?'Пропуски материалов: '+omissions.length:'',validationIssues.length?'Незакрытые проверки: '+validationIssues.length:''].filter(Boolean).join(' · ')+' — посмотреть';notice.addEventListener('click',()=>{closePopovers();showFindings();});$('header-status').append(notice);}
if(hierarchy&&hierarchy.status?.result!=='complete')$('header-status').append(el('p','run-note',(stageRecords.length?'Бизнес-этапы построены частично: ':'Бизнес-этапы не построены: ')+text(array(hierarchy.status?.reasons)[0]||'причина не указана')));
$('header-alerts').hidden=![...$('header-status').children].some(item=>!item.hidden);

function evidenceBlock(container,items,label='Основания в исходниках'){
 const evidence=array(items); if(!evidence.length)return;
 const details=el('details','details'); details.append(el('summary','',label+' · '+evidence.length));
 for(const item of evidence){const box=el('div','evidence');if(typeof item==='string'){box.textContent=item;}else{
 const location=[item.source_id || item.source,item.path || item.file].filter(Boolean).join(' / ');
 const line=item.line || item.start_line || item.line_start,end=item.line_end || item.end_line;
 box.append(el('cite','',location+(line?':'+line+(end&&end!==line?'–'+end:''):'')));
 if(item.quote || item.text || item.snippet)box.append(el('pre','',item.quote || item.text || item.snippet));
 if(item.reason || item.note || item.claim)box.append(el('div','',item.reason || item.note || item.claim));
 }details.append(box);}container.append(details);
}
function relatedButton(container,label,callback){const b=el('button','text-button',label);b.type='button';b.addEventListener('click',callback);container.append(b);return b;}
function codeExplanations(container,item,includeFallback=false){
 const entries=array(item.code_explanations).slice();
 if(!entries.length&&includeFallback)entries.push({explanation:item.basis,evidence:item.evidence});
 if(!entries.length)return;
 const section=el('section','details code-explanations');section.append(el('h3','','Обоснование из исходников кода'));
 const pathOf=ref=>text(ref?.path||ref?.file),quoteOf=ref=>text(ref?.quote||ref?.text||ref?.snippet);
 const startOf=ref=>Number(ref?.line_start||ref?.start_line||ref?.line||0),endOf=ref=>Number(ref?.line_end||ref?.end_line||startOf(ref));
 for(const [index,entry] of entries.entries()){
  const refs=array(entry.evidence),path=pathOf(refs[0]),file=path.split(/[\\/]/).pop();
  const card=el('article','code-explanation'),symbol=entry.kind==='function'?text(entry.symbol).replace(/\(\)$/,'')+'()':entry.kind==='lambda'?'Лямбда':entry.symbol||'Фрагмент файла';
  const relevant=array(item.evidence).find(ref=>quoteOf(ref)&&refs.some(source=>pathOf(source)===pathOf(ref)&&startOf(ref)>=startOf(source)&&endOf(ref)<=endOf(source)));
  const quote=quoteOf(relevant||refs[0]),preview=el('pre','code-fragment');
  if(quote){const lines=quote.split('\n');preview.textContent=(lines.length>5?[...lines.slice(0,4),'…']:lines).join('\n');}
  if(refs.length){
   const fragment=el('div','source-fragment');fragment.id='source-'+encodeURIComponent(text(item.id))+'-'+index;fragment.hidden=true;
   const link=el('a','source-function',[file,symbol].filter(Boolean).join(' · '));link.href='#'+fragment.id;link.title=path;
   link.setAttribute('aria-controls',fragment.id);link.setAttribute('aria-expanded','false');
   link.addEventListener('click',event=>{event.preventDefault();fragment.hidden=!fragment.hidden;preview.hidden=!fragment.hidden;link.setAttribute('aria-expanded',String(!fragment.hidden));});
   for(const ref of refs){const start=startOf(ref),end=endOf(ref);fragment.append(el('cite','',pathOf(ref)+(start?':'+start+(end&&end!==start?'–'+end:''):'')));if(quoteOf(ref))fragment.append(el('pre','',quoteOf(ref)));}
   card.append(link);if(quote)card.append(preview);card.append(fragment);
  }
  if(entry.explanation)card.append(el('p','code-meaning',entry.explanation));
  if(!refs.length&&!entry.explanation)card.append(el('p','muted','Подтверждающие фрагменты для этой операции не представлены.'));
  section.append(card);
 }container.append(section);
}
function systemBlock(container,node){
  if(!legacy)return;
 const system=systemById.get(text(node.system_id));field(container,'Автоматизированная система',systemName(node));if(!system)return;
 const details=el('details','details');details.append(el('summary','','Что известно об АС'));
 field(details,'Роль системы',system.description);
 const implementationNames={provided:'Код реализации представлен в материалах',external:'Внешняя система: её реализация не представлена',unknown:'Наличие реализации в материалах не установлено'};
 field(details,'Граница исследования',implementationNames[system.implementation]||implementationNames.unknown);
 const sourceNames=array(system.source_ids).map(id=>array(model.sources).find(source=>text(source.id)===text(id))?.name||id);
 listField(details,'Материалы, в которых установлена система',sourceNames);
 field(details,'Обоснование определения АС',system.basis);
 evidenceBlock(details,system.evidence,'Фрагменты об АС');container.append(details);
}
function closePopovers(){for(const name of ['notation','status']){$(name+'-panel').hidden=true;$(name+'-toggle').setAttribute('aria-expanded','false');}}
for(const name of ['notation','status'])$(name+'-toggle').addEventListener('click',()=>{const open=$(name+'-panel').hidden;closePopovers();$(name+'-panel').hidden=!open;$(name+'-toggle').setAttribute('aria-expanded',String(open));});
document.addEventListener('pointerdown',event=>{if(!event.target.closest('.map-popover,.map-navigation,.map-actions,.header-alerts'))closePopovers();});
document.addEventListener('keydown',event=>{if(event.key!=='Escape')return;for(const name of ['notation','status'])if(!$(name+'-panel').hidden){closePopovers();$(name+'-toggle').focus();event.preventDefault();break;}});
for(const [kind,label] of (legacy?[['start','Начало сценария'],['action','Атомарное действие'],['decision','Условие и альтернативные ветви'],['fork','Запуск параллельных ветвей'],['join','Синхронизация параллельных ветвей'],['wait','Ожидание события или результата'],['end','Исход: успех, отказ, сбой или отмена'],['external','Внешний участок с ограниченным описанием']]:[['operation','Атомарная операция']])){const row=el('div','notation-item');row.append(notationIcon(kind),el('span','',label));$('notation-items').append(row);}
if(stageRecords.length){const row=el('div','notation-item');row.append(notationIcon('stage'),el('span','','Бизнес-этап с одним входом'));$('notation-items').append(row,el('p','','Вид «Бизнес-этапы» — предложение модели поверх карты операций. Этап с одним входом показан блоком; нажатие раскрывает его операции внутри рамки этапа. Этап с несколькими входами показан своими операциями с подписью этапа, чтобы не рисовать путей, которых нет в коде. Связь между блоками объединяет переходы карты.'));}
$('notation-items').append(el('p','', 'Свечение отмечает итог конечной операции: зелёное — успешное завершение, красное — отказ, сбой или отмена. Подпись уточняет исход. Неизвестный итог, перенос срока и отсутствие решения остаются нейтральными. Условное завершение отмечено словами «При завершении»; другие продолжения сохраняются.'));
function showGroup(group){
 inspectorView={type:'group',id:text(group.id)};
 const panel=$('inspector-content');panel.replaceChildren();panel.append(el('div','inspector-kicker',group.kind==='stage'?'ЭТАП':'БИЗНЕС-ОПЕРАЦИЯ'),el('h2','',group.name));
 field(panel,'Бизнес-смысл',group.description);field(panel,group.kind==='stage'?'Промежуточная цель':'Отделимый результат',group.result);
 const parent=groupById.get(text(group.parent_id));if(parent)relatedButton(panel,'Этап: '+parent.name,()=>selectGroup(parent));
 panel.append(el('p','muted','Бизнес-группировка действий по материалам исследования. Порядок исполнения определяется переходами карты.'));
 if(group.kind==='stage')for(const operation of groups.filter(g=>text(g.parent_id)===text(group.id)))relatedButton(panel,'Операция: '+operation.name,()=>selectGroup(operation));
 const members=groupMembers(group);panel.append(el('h3','details','Состав · '+members.length));
 for(const node of members)relatedButton(panel,(node.name||node.id)+(legacy?' · '+systemName(node):''),()=>selectNode(node,true)).classList.add('group-member');
 evidenceBlock(panel,group.evidence,'Основания группировки');
}
function selectGroup(group){
 closePopovers();selected={type:'group',id:group.id};showGroup(group);openInspector();updateHighlight();
 const members=groupMembers(group);requestAnimationFrame(()=>frameGroup(group));announce('Выбрана группа: '+group.name+'. Операций: '+members.length);
}
// The picker lists business stages when they exist, otherwise research-time stages of older maps.
const stages=stageRecords.length?stageRecords:groups.filter(g=>g.kind==='stage');
for(const stage of stages){const option=el('option','',stage.name);option.value=text(stage.id);$('stage-select').append(option);}
$('stage-select').disabled=!stages.length;
if(!stages.length)$('stage-select').options[0].textContent='Этапы не заданы';
 // The hidden select stays the source of truth; this menu only presents it.
 function syncStageMenu(){const select=$('stage-select');$('stage-current').textContent=select.options[select.selectedIndex]?.textContent||'';for(const item of $('stage-menu').children)item.setAttribute('aria-selected',String(item.dataset.value===select.value));}
 function openStageMenu(open){$('stage-menu').classList.toggle('open',open);$('stage-button').setAttribute('aria-expanded',String(open));if(open)([...$('stage-menu').children].find(item=>item.getAttribute('aria-selected')==='true')||$('stage-menu').firstElementChild)?.focus();}
 for(const option of $('stage-select').options){const item=el('button','stage-option',option.textContent);item.type='button';item.tabIndex=-1;item.setAttribute('role','option');item.dataset.value=option.value;
  item.addEventListener('click',()=>{openStageMenu(false);$('stage-button').focus();if($('stage-select').value!==option.value){$('stage-select').value=option.value;$('stage-select').dispatchEvent(new Event('change'));}syncStageMenu();});$('stage-menu').append(item);}
 $('stage-button').disabled=$('stage-select').disabled;
 $('stage-button').addEventListener('click',()=>openStageMenu(!$('stage-menu').classList.contains('open')));
 $('stage-button').addEventListener('keydown',event=>{if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();openStageMenu(true);}});
 $('stage-menu').addEventListener('keydown',event=>{const items=[...$('stage-menu').children],index=items.indexOf(document.activeElement);
  if(event.key==='ArrowDown'||event.key==='ArrowUp'){event.preventDefault();items[(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length].focus();}
  else if(event.key==='Escape'){event.preventDefault();event.stopPropagation();openStageMenu(false);$('stage-button').focus();}else if(event.key==='Tab')openStageMenu(false);});
 document.addEventListener('pointerdown',event=>{if(!event.target.closest('.stage-picker'))openStageMenu(false);});
 syncStageMenu();
$('stage-select').addEventListener('change',()=>{
 const value=$('stage-select').value,stage=businessStageById.get(value),group=stage?null:groupById.get(value);closePopovers();
 if(stage){selectStage(stage);return;}
 closeInspector(false);
 if(group){selected={type:'group',id:group.id};updateHighlight();requestAnimationFrame(()=>frameGroup(group));announce('Выбран этап: '+group.name);}
 else{requestAnimationFrame(initialView);announce('Показаны все этапы');}
});
$('clear-selection').addEventListener('click',()=>closeInspector());
function showOverview(){
 inspectorView={type:'overview'};
 const panel=$('inspector-content'); panel.replaceChildren();panel.append(el('div','inspector-kicker','ВАРИАНТ СЦЕНАРИЯ'),el('h2','',scenario.title || 'Карта процесса'));
 panel.append(el('p','muted',model.summary || 'Выберите операцию, чтобы увидеть её смысл, вход, результат, условия продолжения и подтверждения в коде.'));
 field(panel,'Описание варианта сценария',fullDescription);
 const exclusions=array(scenario.exclusions),excludedTasks=array(model.research_tasks).filter(task=>task.status==='out_of_scope');
 if(exclusions.length){const block=el('div','field');block.append(el('dt','','Вне исследования'));
  for(const item of exclusions){const count=excludedTasks.filter(task=>text(task.exclusion)===text(item)).length;block.append(el('dd','',text(item)+(count?' · исключено участков кода: '+count:'')));}
  panel.append(block);}
 if(array(scenario.assumptions).length)field(panel,'Допущения',array(scenario.assumptions).join('; '));field(panel,'Начало',scenario.start);field(panel,'Завершение',scenario.end);
 panel.append(el('p','muted',legacy?'Архивный граф сохраняет исходные узлы. Соответствие новой методологии атомарных операций не проверено.':'Граф показывает операции выбранного сценария. Условия продолжения находятся в карточке операции. Наличие цитат не доказывает полноту карты.'));
 if(openFindings)relatedButton(panel,'Посмотреть пробелы и проверки ('+openFindings+')',showFindings);
 const outside=stageRecords.length?array(hierarchy.unassigned).filter(row=>byId.has(text(row?.operation_id))):[];
 if(outside.length){const details=el('details','details');details.append(el('summary','','Операции вне бизнес-этапов · '+outside.length));
  for(const row of outside){const node=byId.get(text(row.operation_id)),item=el('article','continuation');relatedButton(item,node.name||node.id,()=>selectNode(node,true)).classList.add('continuation-target');item.append(el('p','continuation-condition',row.reason));details.append(item);}
  panel.append(details);}
 const details=el('details','details');details.append(el('summary','','Что проверено'));
 details.append(el('p','muted','Основания показывают найденные участки исходников. Их наличие не доказывает правильность бизнес-интерпретации и полноту карты.'));
 for(const review of array(model.source_reviews)){const source=array(model.sources).find(s=>s.id===review.source_id);const disposition={reviewed:'Рассмотрен',out_of_scope:'Вне выбранного сценария',unavailable:'Недоступен'}[review.disposition];const inspection=review.disposition==='out_of_scope'&&review.inspected===false?'Исключение без изучения кода':Number.isFinite(review.read_files)?'Прочитано файлов: '+review.read_files:'';field(details,source?.name || review.source_id || 'Источник',[disposition,inspection,review.summary || review.reason || review.description].filter(Boolean).join('\n'));}
 if(Number.isFinite(run.steps))field(details,'Шагов исследования',run.steps);panel.append(details);
}
function externalInteractions(container,node){
 const interactions=array(node.external_interactions);if(!interactions.length)return;
 const section=el('section','details external-interactions');section.append(el('h3','','Связь с внешней системой'));
 for(const item of interactions){const card=el('article','external-interaction');card.append(el('h4','',item.name||'Внешняя система'));
  field(card,'Взаимодействие',item.purpose);field(card,'Направление',{request:'Запрос данных или выполнения действия',send:'Отправка данных',receive:'Получение данных'}[item.direction]||item.direction);
  if(item.endpoint||item.contract){const details=el('details','details');details.append(el('summary','','Точка вызова и контракт'));field(details,'Точка вызова',item.endpoint);field(details,'Контракт',item.contract);card.append(details);}
  evidenceBlock(card,item.evidence,'Основания внешнего взаимодействия');section.append(card);
 }container.append(section);
}
function continuations(container,node){
 const outgoing=validEdges.filter(e=>text(e.source)===text(node.id));if(!outgoing.length)return;
 const section=el('section','details continuations');section.append(el('h3','','Следующие операции'));
 if(outgoing.length>1){const mode={choice:'Выполняется операция, условие которой выполнено.',all:'Запускаются все указанные продолжения при выполнении их условий.',sequence:'Порядок продолжения указан в условиях переходов.'}[node.continuation_mode];section.append(el('p','muted',mode||'Способ выбора продолжения не установлен.'));}
 for(const edge of outgoing){const peer=byId.get(text(edge.target)),row=el('article','continuation');row.dataset.transitionId=text(edge.id);
  relatedButton(row,peer.name||peer.id,()=>selectNode(peer,true)).classList.add('continuation-target');
  row.append(el('p','continuation-condition',edge.condition||(outgoing.length>1?'Условие продолжения не установлено':'После завершения операции')));section.append(row);
 }container.append(section);
}
function showNode(node){
 inspectorView={type:'node',id:text(node.id)};
 const panel=$('inspector-content');panel.replaceChildren();
 const meta=el('div','operation-meta');meta.append(el('div','inspector-kicker',nodeKindLabel(node)));
 const ancestry=nodeGroups(node),stage=nodeStage(node);if(ancestry.length||stage){const links=el('div','group-links');if(stage)relatedButton(links,'Этап: '+stage.name,()=>selectStage(stage));for(const group of ancestry)relatedButton(links,(group.kind==='stage'?'Этап: ':'Группа: ')+group.name,()=>selectGroup(group));meta.append(links);}
 panel.append(meta,badge(node.evidence_status),el('h2','',node.name || node.id));
 field(panel,'Бизнес-смысл',node.description);field(panel,'Завершение сценария',node.completion_condition);continuations(panel,node);
 field(panel,'Вход',node.input);field(panel,'Результат',node.output);field(panel,'Условие выполнения',node.condition);
 const terminal=legacy?node.kind==='end':array(scenario.terminal_operation_ids).map(text).includes(text(node.id));
 if(node.effect_kind&&(node.effect_kind!=='unknown'||terminal))field(panel,'Что установлено',{decision:'Сформировано решение',recommendation:'Сформирована рекомендация',command:'Отправлена команда',result:'Подтверждён результат',unknown:'Не установлено'}[node.effect_kind]||'Не установлено');
 if(node.outcome&&(node.outcome!=='unknown'||terminal))field(panel,'Исход',outcomeNames[node.outcome]||node.outcome);
 if(node.join_mode==='all')field(panel,'Готовность к выполнению','Операция ожидает завершения всех предусмотренных входящих ветвей.');
 externalInteractions(panel,node);systemBlock(panel,node);
 codeExplanations(panel,node,true);
 if(legacy&&node.parent_id&&byId.has(text(node.parent_id)))field(panel,'Часть шага',byId.get(text(node.parent_id)).name);
 const nodeGaps=gaps.filter(g=>array(g.node_ids).map(text).includes(text(node.id)));for(const gap of nodeGaps)field(panel,'Не установлено',gap.description);
}
function showEdge(edge){
 inspectorView={type:'edge',id:text(edge.id)};
 const panel=$('inspector-content');panel.replaceChildren();panel.append(el('div','inspector-kicker',legacy?'ПЕРЕХОД · '+(edgeNames[edge.kind] || 'Связь'):'ПРОДОЛЖЕНИЕ ИСПОЛНЕНИЯ'),badge(edge.evidence_status));
 const from=byId.get(text(edge.source)), to=byId.get(text(edge.target));panel.append(el('h2','',(from.name || from.id)+' → '+(to.name || to.id)));
 field(panel,'Что происходит',edge.description);codeExplanations(panel,edge);field(panel,'Обоснование перехода',edge.basis);field(panel,'Условие перехода',edge.condition);field(panel,'Передаваемые данные',edge.data);field(panel,'Связь экземпляра сценария',edge.correlation);
 const related=el('div','related');relatedButton(related,'Открыть: '+(from.name || from.id),()=>selectNode(from,true));relatedButton(related,'Открыть: '+(to.name || to.id),()=>selectNode(to,true));panel.append(related);
 if(edge.channel||edge.contract){const technical=el('details','details');technical.append(el('summary','','Способ передачи и контракт'));field(technical,'Канал',edge.channel);field(technical,'Контракт',edge.contract);panel.append(technical);}
 evidenceBlock(panel,edge.sender_evidence,'Отправляющая сторона');evidenceBlock(panel,edge.receiver_evidence,'Принимающая сторона');evidenceBlock(panel,edge.evidence);
 if(!array(edge.evidence).length&&!array(edge.sender_evidence).length&&!array(edge.receiver_evidence).length)field(panel,'Основания','Подтверждающие фрагменты для перехода не представлены.');
}
function scenarioNodeScope(nodeId,edges){
 const incoming=new Map(),outgoing=new Map();
 for(const edge of edges){const source=String(edge.source),target=String(edge.target);if(!outgoing.has(source))outgoing.set(source,[]);outgoing.get(source).push(target);if(!incoming.has(target))incoming.set(target,[]);incoming.get(target).push(source);}
 const reachable=adjacency=>{const seen=new Set([String(nodeId)]),pending=[String(nodeId)];while(pending.length){const id=pending.pop();for(const next of adjacency.get(id)||[])if(!seen.has(next)){seen.add(next);pending.push(next);}}return seen;};
 const ancestors=reachable(incoming),descendants=reachable(outgoing),scopeEdges=new Set();
 for(const edge of edges){const source=String(edge.source),target=String(edge.target);if((ancestors.has(source)&&ancestors.has(target))||(descendants.has(source)&&descendants.has(target)))scopeEdges.add(String(edge.id));}
 return {nodes:new Set([...ancestors,...descendants]),edges:scopeEdges};
}
function projectScenarioScope(canonical,view){
 const nodes=new Set([...canonical.nodes].map(id=>view.represent(id))),edges=new Set();
 for(const link of view.links)if((link.transitions||[link]).some(edge=>canonical.edges.has(String(edge.id))))edges.add(String(link.id));
 return {nodes,edges};
}
function stageScope(ids,edges){
 // A whole stage lies on every path through any of its operations.
 const nodes=new Set(),scoped=new Set();
 for(const id of ids){const scope=scenarioNodeScope(id,edges);for(const node of scope.nodes)nodes.add(node);for(const edge of scope.edges)scoped.add(edge);}
 return {nodes,edges:scoped};
}
function updateHighlight(){
 // The selection stays a map element; the current view resolves it. An operation
 // inside a collapsed stage is represented by that stage's block.
 const scope=new Set(),related=new Set();let focus=null,focusLink=null;
 const stage=selected?.type==='stage'?businessStageById.get(text(selected.id)):null,group=selected?.type==='group'?groupById.get(text(selected.id)):null;
 const project=canonical=>{const paths=projectScenarioScope(canonical,display);for(const id of paths.nodes)scope.add(id);for(const id of paths.edges)related.add(id);};
 const members=list=>{const ids=new Set(list.map(node=>text(node.id)));project({nodes:ids,edges:new Set(validEdges.filter(edge=>ids.has(text(edge.source))&&ids.has(text(edge.target))).map(edge=>text(edge.id)))});};
 const block=stage?display.items.get('stage:'+text(stage.id)):null;
 if(selected?.type==='node')focus=display.represent(selected.id);
 else if(block&&!block.open)focus=block.id;
 // Reachability belongs to the atomic graph. Traversing the collapsed graph would
 // incorrectly include another member's exits when only one operation is selected.
 if(selected?.type==='node')project(scenarioNodeScope(selected.id,validEdges));
 else if(block&&!block.open)project(stageScope(stageMembers(stage).map(node=>text(node.id)),validEdges));
 else if(stage)members(stageMembers(stage));
 else if(group)members(groupMembers(group));
 else if(selected?.type==='edge'||selected?.type==='link'){const link=selected.type==='edge'?display.linkOf.get(text(selected.id)):display.linkById.get(text(selected.id));if(link){focusLink=text(link.id);scope.add(link.source);scope.add(link.target);}}
 $('clear-selection').hidden=!selected;
 const picked=stage||(group?.kind==='stage'?group:null);
 $('stage-select').value=picked?text(picked.id):'';syncStageMenu();
 for(const [id,button] of nodeButtons){button.classList.toggle('in-scope',!!selected&&scope.has(id));button.classList.toggle('selected',focus===id);button.setAttribute('aria-pressed',String(focus===id));button.classList.toggle('dimmed',!!selected&&!scope.has(id));}
 for(const [id,line] of edgeGroups){const isSelected=focusLink===id,isRelated=related.has(id);line.classList.toggle('in-scope',isRelated);line.classList.toggle('selected',isSelected);line.classList.toggle('dimmed',!!selected&&!isSelected&&!isRelated);}
 // An open stage stays bright while any of its operations is in scope.
 for(const [id,frame] of frameElements){const item=display.items.get(id);if(!item)continue;const inside=item.members.some(member=>scope.has(member)),chosen=block?.id===id;
  for(const part of [frame.surface,frame.head]){part.classList.toggle('in-scope',!!selected&&inside);part.classList.toggle('selected',chosen);part.classList.toggle('dimmed',!!selected&&!inside&&!chosen);}}
}
function announce(value){$('announcement').textContent=value;}
function availableWidth(){const view=$('viewport');return Math.max(180,view.clientWidth-(!$('inspector').hidden&&window.innerWidth>700?$('inspector').offsetWidth:0));}
// Set by the report tabs: the operation panel is shown only over a visible map.
let mapTabHook=null;
function focusReference(element=document.activeElement){
 const owner=element?.closest?.('[data-node-id],[data-stage-id],[data-edge-id]');
 const transition=owner?.dataset.transitionIds?JSON.parse(owner.dataset.transitionIds)[0]:null;
 const selectedTransition=selected?.type==='link'?display.linkById.get(text(selected.id))?.transitions?.[0]?.id:null;
 const reference=owner?.dataset.nodeId?{type:'node',id:owner.dataset.nodeId}:owner?.dataset.stageId?{type:'stage',id:owner.dataset.stageId}:transition?{type:'edge',id:transition}:selectedTransition?{type:'edge',id:text(selectedTransition)}:selected;
 return {element,reference:reference?{...reference}:null};
}
function restoreMapFocus(target){
 if($('panel-map').inert)return;
 const ref=target?.reference;let element=target?.element;
 if(!element?.isConnected||element===document.body||element.closest('[hidden],[inert]')){
  const id=ref?.type==='node'?display.represent(ref.id):ref?.type==='stage'?'stage:'+text(ref.id):null;
  element=id?(nodeButtons.get(id)||frameElements.get(id)?.head.querySelector('.stage-frame-title')):
   ref?.type==='edge'?edgeGroups.get(text(display.linkOf.get(text(ref.id))?.id)):ref?.type==='link'?edgeGroups.get(text(ref.id)):null;
  if(!element&&ref?.type==='stage'){const stage=businessStageById.get(text(ref.id));element=stage&&nodeButtons.get(text(stage.entry_operation_id));}
 }
 const fallback=[$('view-mode').querySelector('[aria-pressed=true]'),$('stage-button'),$('viewport')].find(node=>node&&!node.closest('[hidden],[inert]')&&!node.disabled);
 (element||fallback).focus({preventScroll:true});
}
function openInspector(){if($('panel-map').inert&&mapTabHook)mapTabHook();if($('inspector').hidden)restoreFocus=focusReference();$('inspector').hidden=false;$('inspector-content').scrollTop=0;applyZoom(zoom);if(window.innerWidth<=700)$('close-inspector').focus({preventScroll:true});}
function closeInspector(restore=true){const target=restoreFocus||focusReference();selected=null;$('inspector').hidden=true;updateHighlight();applyZoom(zoom);if(restore)restoreMapFocus(target);restoreFocus=null;}
// On the stage view an operation inside a closed block is shown by opening that block.
function revealNode(id){
 if(viewMode!=='stages'||display.represent(id)===text(id))return;
 const stage=nodeStage(byId.get(text(id)));if(!stage)return;
 expandedStages.add(text(stage.id));display=buildDisplay();draw();
}
function selectNode(node,center=false){revealNode(node.id);closePopovers();selected={type:'node',id:node.id};showNode(node);openInspector();updateHighlight();announce('Выбрана операция: '+(node.name||node.id));if(center){requestAnimationFrame(()=>centerNode(node));}else if(window.innerWidth>700)centerNode(node);}
function selectEdge(edge){if(viewMode==='stages'&&!display.linkOf.has(text(edge.id)))revealNode(edge.source);closePopovers();selected={type:'edge',id:edge.id,edge};showEdge(edge);openInspector();updateHighlight();announce('Выбран переход: '+(edge.description||edgeNames[edge.kind]||'Связь'));}
function centerNode(node){centerItem(display.represent(node.id));}
function centerItem(id){const p=positions.get(id);if(!p)return;const view=$('viewport'),stage=$('stage');view.scrollTo({left:Math.max(0,(p.x+p.w/2)*zoom+(parseFloat(stage.style.left)||0)-availableWidth()/2),top:Math.max(0,(p.y+p.h/2)*zoom+(parseFloat(stage.style.top)||0)-view.clientHeight/2)});}
function frameGroup(group){frameNodes(groupMembers(group));}
function frameNodes(list){frameBoxes([...new Set(list.map(n=>display.represent(n.id)))].map(id=>positions.get(id)).filter(Boolean));}
function frameBoxes(members){
 if(!members.length)return;
 const left=Math.min(...members.map(p=>p.x)),top=Math.min(...members.map(p=>p.y)),right=Math.max(...members.map(p=>p.x+p.w)),bottom=Math.max(...members.map(p=>p.y+p.h));
 const view=$('viewport');applyZoom(Math.min(1,(availableWidth()-48)/(right-left),Math.max(40,view.clientHeight-96)/(bottom-top)));
 const stage=$('stage');view.scrollTo({left:Math.max(0,(left+right)/2*zoom+(parseFloat(stage.style.left)||0)-availableWidth()/2),top:Math.max(0,(top+bottom)/2*zoom+(parseFloat(stage.style.top)||0)-(view.clientHeight-48)/2)});
}
$('close-inspector').addEventListener('click',()=>closeInspector());
// A single continuation needs no condition, as on the operation panel.
const transitionCondition=edge=>edge.condition||(validEdges.filter(other=>text(other.source)===text(edge.source)).length>1?'Условие продолжения не установлено':'После завершения операции');
const itemName=id=>{const item=display.items.get(id);return item?.type==='stage'?text(item.stage.name||item.stage.id):text(byId.get(id)?.name||id);};
function frameStage(stage){const id='stage:'+text(stage.id),item=display.items.get(id);if(item?.open)frameBoxes([positions.get(id)].filter(Boolean));else if(item)centerItem(id);else frameNodes(stageMembers(stage));}
function expandStage(stage){
 expandedStages.add(text(stage.id));display=buildDisplay();closePopovers();selected={type:'stage',id:stage.id};
 if(!$('inspector').hidden)showStage(stage);
 draw();
 requestAnimationFrame(()=>{frameStage(stage);frameElements.get('stage:'+text(stage.id))?.head.querySelector('.stage-frame-title').focus({preventScroll:true});});
 announce('Этап «'+(stage.name||stage.id)+'» раскрыт. Операций: '+stageMembers(stage).length);
}
function collapseStage(stage){
 expandedStages.delete(text(stage.id));display=buildDisplay();
 if(!$('inspector').hidden&&selected?.type==='stage'&&text(selected.id)===text(stage.id))showStage(stage);
 draw();
 const id='stage:'+text(stage.id);requestAnimationFrame(()=>{centerItem(id);nodeButtons.get(id)?.focus({preventScroll:true});});
 announce('Этап «'+(stage.name||stage.id)+'» свёрнут');
}
function selectStage(stage){
 closePopovers();selected={type:'stage',id:stage.id};showStage(stage);openInspector();updateHighlight();
 requestAnimationFrame(()=>frameStage(stage));announce('Выбран бизнес-этап: '+(stage.name||stage.id));
}
function showStage(stage){
 inspectorView={type:'stage',id:text(stage.id)};
 const panel=$('inspector-content'),members=stageMembers(stage),inside=new Set(members.map(n=>text(n.id)));panel.replaceChildren();
 panel.append(el('div','inspector-kicker','БИЗНЕС-ЭТАП'),el('span','badge unknown stage-proposal','Предложение модели'),el('h2','',stage.name||stage.id));
 field(panel,'Цель этапа',stage.goal);field(panel,'Результат этапа',stage.result);
 const starts=members.filter(n=>array(scenario.entry_operation_ids).map(text).includes(text(n.id)));
 const transitions=(title,list,outer,lead)=>{if(!list.length&&!lead.length)return;const section=el('section','details continuations');section.append(el('h3','',title));
  for(const node of lead){const row=el('article','continuation');relatedButton(row,'Начало сценария: '+(node.name||node.id),()=>selectNode(node,true)).classList.add('continuation-target');section.append(row);}
  for(const edge of list){const peer=byId.get(text(edge[outer])),own=byId.get(text(edge[outer==='source'?'target':'source'])),peerStage=nodeStage(peer),row=el('article','continuation');
   relatedButton(row,outer==='source'?(peer.name||peer.id)+' → '+(own.name||own.id):(own.name||own.id)+' → '+(peer.name||peer.id),()=>selectEdge(edge)).classList.add('continuation-target');
   if(peerStage)row.append(el('p','muted','Этап: '+peerStage.name));
   row.append(el('p','continuation-condition',transitionCondition(edge)));section.append(row);}
  panel.append(section);};
 transitions('Входы в этап',validEdges.filter(e=>inside.has(text(e.target))&&!inside.has(text(e.source))),'source',starts);
 transitions('Продолжения после этапа',validEdges.filter(e=>inside.has(text(e.source))&&!inside.has(text(e.target))),'target',[]);
 const semantics=stageSemantics(members,validEdges);
 if(semantics.length){const section=el('section','details stage-semantics');section.append(el('h3','','Порядок выполнения операций'));
  for(const item of semantics){const row=el('article','continuation');row.dataset.operationId=text(item.node.id);row.dataset[item.dimension+'Mode']=item.mode;
   relatedButton(row,item.node.name||item.node.id,()=>selectNode(item.node,true)).classList.add('continuation-target');row.append(el('p','continuation-condition',item.label));section.append(row);}panel.append(section);}
 const ends=members.map(node=>({node,terminal:terminalPresentation(node)})).filter(item=>item.terminal);
 if(ends.length){const section=el('section','details continuations');section.append(el('h3','','Завершения сценария в этапе'));
  for(const {node,terminal} of ends){const row=el('article','continuation stage-end '+terminal.tone);relatedButton(row,node.name||node.id,()=>selectNode(node,true)).classList.add('continuation-target');row.append(el('p','continuation-condition',terminal.label+(node.completion_condition?'. '+node.completion_condition:'')));section.append(row);}
  panel.append(section);}
 const external=members.flatMap(node=>array(node.external_interactions).map(item=>({node,item})));
 if(external.length){const section=el('section','details continuations');section.append(el('h3','','Связи с внешними системами'));
  for(const {node,item} of external){const row=el('article','continuation');relatedButton(row,(item.name||'Внешняя система')+' · '+(node.name||node.id),()=>selectNode(node,true)).classList.add('continuation-target');if(item.purpose)row.append(el('p','continuation-condition',item.purpose));section.append(row);}
  panel.append(section);}
 for(const gap of gaps.filter(g=>array(g.node_ids).some(id=>inside.has(text(id)))))field(panel,'Не установлено',gap.description);
 if(!stage.collapsible){const names=new Map(nodes.map(n=>[text(n.id),text(n.name||n.id)]));
  field(panel,'Почему этап показан операциями',text(stage.collapse_reason).replace(/[\w.:-]+/g,token=>names.has(token)?'«'+names.get(token)+'»':token)+'. Один блок нарисовал бы пути, которых нет в коде.');}
 field(panel,'Примечание',stage.note);
 const list=el('section','details');list.append(el('h3','','Операции этапа · '+members.length));
 for(const node of members)relatedButton(list,node.name||node.id,()=>selectNode(node,true)).classList.add('group-member');panel.append(list);
 if(viewMode==='stages'&&stage.collapsible){const open=expandedStages.has(text(stage.id));
  relatedButton(panel,open?'Свернуть этап на карте':'Раскрыть операции этапа на карте',()=>open?collapseStage(stage):expandStage(stage)).classList.add('stage-switch');}
 if(viewMode==='stages')relatedButton(panel,'Показать операции этапа на подробной карте',()=>{setMode('atomic');selectStage(stage);}).classList.add('stage-switch');
 else if(stage.collapsible)relatedButton(panel,'Показать этап блоком на карте бизнес-этапов',()=>{setMode('stages');selectStage(stage);}).classList.add('stage-switch');
 panel.append(el('p','muted','Этап предложен моделью по готовой карте операций и наследует её пробелы и ошибки. Входы, продолжения и завершения этапа взяты из переходов карты.'));
}
function selectLink(link){closePopovers();selected={type:'link',id:link.id};showLink(link);openInspector();updateHighlight();announce('Выбран переход: '+itemName(link.source)+' → '+itemName(link.target));}
function showLink(link){
 inspectorView={type:'link',id:text(link.id)};
 const panel=$('inspector-content');panel.replaceChildren();
 panel.append(el('div','inspector-kicker','ПЕРЕХОД НА КАРТЕ ЭТАПОВ'),badge(link.evidence_status),el('h2','',itemName(link.source)+' → '+itemName(link.target)));
 panel.append(el('p','muted','Связь объединяет переходы карты операций: '+link.transitions.length+'. Условия и основания указаны у каждого перехода.'));
 const section=el('section','details continuations');section.append(el('h3','','Переходы карты операций'));
 for(const edge of link.transitions){const from=byId.get(text(edge.source)),to=byId.get(text(edge.target)),row=el('article','continuation');
  relatedButton(row,(from.name||from.id)+' → '+(to.name||to.id),()=>{revealNode(edge.source);revealNode(edge.target);selectEdge(edge);}).classList.add('continuation-target');
  row.append(el('p','continuation-condition',transitionCondition(edge)));section.append(row);}
 panel.append(section);
}
function selectDisplayLink(link){if(viewMode==='atomic')selectEdge(link);else if(link.transitions.length===1&&!link.source.startsWith('stage:')&&!link.target.startsWith('stage:'))selectEdge(link.transitions[0]);else selectLink(link);}
function buildDisplay(){
 const view=viewMode==='stages'?projectScenarioStages(nodes,validEdges,stageRecords,membership,[...expandedStages])
  :{items:nodes.map(node=>({id:text(node.id),type:'node',node})),links:validEdges,represent:id=>text(id),outer:id=>text(id)};
 const linkOf=new Map();for(const link of view.links)for(const edge of link.transitions||[link])linkOf.set(text(edge.id),link);
 return {list:view.items,items:new Map(view.items.map(item=>[item.id,item])),links:view.links,represent:view.represent,outer:view.outer,linkOf,linkById:new Map(view.links.map(link=>[text(link.id),link]))};
}
function setMode(mode){
 if(mode===viewMode||(mode==='stages'&&!stageRecords.length))return;
 viewMode=mode;display=buildDisplay();syncModeButtons();
 try{const params=new URLSearchParams(location.hash.slice(1));if(mode==='stages')params.set('view','stages');else params.delete('view');history.replaceState(null,'',params.toString()?'#'+params:location.pathname+location.search);}catch(error){}
 // A link exists only on the stage view; other selections are kept and framed again.
 if(selected?.type==='link')closeInspector(false);
 draw();
 const stage=selected?.type==='stage'?businessStageById.get(text(selected.id)):null,node=selected?.type==='node'?byId.get(text(selected.id)):null;
 if(stage){showStage(stage);frameStage(stage);}else if(node)centerNode(node);else initialView();
 announce(mode==='stages'?'Показаны бизнес-этапы':'Показаны атомарные операции');
}
function syncModeButtons(){for(const button of $('view-mode').querySelectorAll('button'))button.setAttribute('aria-pressed',String(button.dataset.mode===viewMode));}
function captureMapState(){
 const view=$('viewport');return {mode:viewMode,expanded:[...expandedStages],zoom,left:view.scrollLeft,top:view.scrollTop,selected:selected?{...selected}:null,
  inspectorHidden:$('inspector').hidden,inspectorView:{...inspectorView},inspectorScroll:$('inspector-content').scrollTop,restoreFocus};
}
function restoreMapState(state){
 if(!state)return;viewMode=state.mode;expandedStages.clear();for(const id of state.expanded)expandedStages.add(id);
 display=buildDisplay();selected=state.selected;restoreFocus=state.restoreFocus;syncModeButtons();$('inspector').hidden=state.inspectorHidden;
 const view=state.inspectorView,value=view.type==='node'?byId.get(view.id):view.type==='edge'?validEdges.find(edge=>text(edge.id)===view.id):
  view.type==='stage'?businessStageById.get(view.id):view.type==='group'?groupById.get(view.id):view.type==='link'?display.linkById.get(view.id):null;
 if(value)({node:showNode,edge:showEdge,stage:showStage,group:showGroup,link:showLink}[view.type])(value);
 else if(view.type==='findings'){showFindings();$('inspector').hidden=state.inspectorHidden;}else showOverview();
 draw();applyZoom(state.zoom);$('viewport').scrollTo({left:state.left,top:state.top});$('inspector-content').scrollTop=state.inspectorScroll;
}
$('view-mode').hidden=!stageRecords.length;
for(const button of $('view-mode').querySelectorAll('button'))button.addEventListener('click',()=>setMode(button.dataset.mode));
display=buildDisplay();syncModeButtons();
document.addEventListener('keydown',event=>{if(event.defaultPrevented||$('panel-map').inert)return;if(event.key==='Escape'&&!$('inspector').hidden){event.preventDefault();closeInspector();}});

function showFindings(){
 inspectorView={type:'findings'};
 closePopovers();selected=null;const panel=$('inspector-content');panel.replaceChildren();
 panel.append(el('h2','','Что осталось установить'));
 field(panel,'Результат исследования',stateNames[runState]||'Статус исследования не установлен');
 listField(panel,'Материалы, не вошедшие в исследование',omissions);
 listField(panel,'Примечания к источникам',model.source_notes);
 listField(panel,'Незакрытые проверки карты',validationIssues);
 listField(panel,'Ограничения анализа',array(model.limitations).length?model.limitations:run.limitations);
if(!openFindings)panel.append(el('p','empty','Агент не зафиксировал пробелов. Это не является гарантией полноты карты.'));
for(const gap of gaps){const card=el('article','gap');card.append(el('p','',gap.description || 'Описание пробела не задано.'));const links=el('div','related');for(const id of array(gap.node_ids)){const node=byId.get(text(id));if(node)relatedButton(links,node.name || node.id,()=>selectNode(node,true));}for(const id of array(legacy?gap.system_ids:[])){const system=systemById.get(text(id));if(system){const label=el('span','badge '+evidenceStatus(system.evidence_status),'АС: '+(system.name || system.id)+(evidenceStatus(system.evidence_status)==='unknown'?' · Не установлено':''));links.append(label);}}for(const id of array(gap.edge_ids)){const edge=validEdges.find(e=>text(e.id)===text(id));if(edge){const from=byId.get(text(edge.source)),to=byId.get(text(edge.target));relatedButton(links,(from.name||from.id)+' → '+(to.name||to.id),()=>selectEdge(edge));}}
 const tasks=array(model.research_tasks).filter(t=>array(gap.task_ids).map(text).includes(text(t.id)));if(tasks.length)listField(card,'Незавершённые задачи исследования',tasks.map(t=>t.target));
 evidenceBlock(card,gap.evidence,'Основания пробела');card.append(links);panel.append(card);}
for(const [category,items] of [['Материал не исследован',omissions],['Незакрытая проверка карты',validationIssues]])for(const item of items){const card=el('article','gap');card.append(el('div','gap-category',category),el('p','',typeof item==='object'?(item.description||item.message||item.reason||item):item));panel.append(card);}
if(edges.length!==validEdges.length){const card=el('article','gap');card.append(el('p','','Часть переходов ссылается на отсутствующие шаги и не может быть показана: '+(edges.length-validEdges.length)+'.'));panel.append(card);}

 openInspector();updateHighlight();
}

__GRAPH_LAYOUT_JS__
function operationCard(node){
 const id=text(node.id),kind=!legacy?'operation':Object.prototype.hasOwnProperty.call(kindNames,node.kind)?node.kind:'action';
 const button=el('button','node kind-'+kind);button.type='button';button.dataset.nodeId=id;
 const terminal=terminalPresentation(node);if(terminal)button.classList.add('terminal-'+terminal.tone);
 button.setAttribute('aria-label',nodeKindLabel(node)+': '+(node.name||node.id)+(legacy?'. '+systemName(node):'')+'. '+evidenceNames[evidenceStatus(node.evidence_status)]+(terminal?'. '+terminal.label:''));button.setAttribute('aria-pressed','false');
 const content=el('span','node-content');
 content.append(el('span','node-name',node.name||node.id));
 const group=groupById.get(text(node.group_id));if(group){const label=el('span','node-group',group.name);label.title=nodeGroups(node).map(g=>g.name).join(' / ');content.append(label);}
 // Inside an open stage the frame already names the stage.
 const stage=nodeStage(node);if(stage&&display.outer(id)===id){const label=el('span','node-group'+(viewMode==='stages'?' stage-chip':''),stage.name);label.title=viewMode==='stages'?'Бизнес-этап с несколькими входами показан своими операциями':'Бизнес-этап';content.append(label);}
 if(legacy)content.append(el('span','node-system','АС · '+systemName(node)));
 content.append(el('span','node-status '+evidenceStatus(node.evidence_status),evidenceNames[evidenceStatus(node.evidence_status)]));
 if(terminal)content.append(el('span','node-terminal',terminal.label));
 const findingCount=gaps.filter(g=>array(g.node_ids).map(text).includes(id)).length;if(findingCount)content.append(el('span','node-gap','Не установлено: '+findingCount));
 button.append(content);button.addEventListener('click',()=>selectNode(node));$('nodes').append(button);
 if(kind==='decision')button.style.height=Math.max(230,content.offsetHeight*2+32)+'px';
 return {id,kind,button};
}
function stageFrameHead(item){
 const stage=item.stage,head=el('div','stage-frame-head'),bar=el('div','stage-frame-bar'),title=el('button','stage-frame-title');head.dataset.stageId=text(stage.id);
 title.type='button';title.setAttribute('aria-label','Бизнес-этап: '+(stage.name||stage.id)+'. Открыть описание этапа');
 title.append(el('span','node-stage-kicker','Бизнес-этап · '+item.members.length+' '+plural(item.members.length,['операция','операции','операций'])),el('span','stage-frame-name',stage.name||stage.id));
 title.addEventListener('click',()=>selectStage(stage));
 const close=el('button','stage-frame-close','Свернуть');close.type='button';close.setAttribute('aria-expanded','true');close.setAttribute('aria-label','Свернуть этап «'+(stage.name||stage.id)+'»');
 close.addEventListener('click',()=>collapseStage(stage));
 bar.append(title,close);head.append(bar);if(stage.result)head.append(el('p','stage-frame-result',stage.result));
 $('nodes').append(head);return head;
}
function frameExit(id,box,vertical){
 // A line leaving an open stage starts at its operation and passes the frame's
 // later cards through their gaps, so it never seems to leave from another card.
 const local=box.local,last=Math.max(...local.rank.values())+1,q=local.positions.get(id);
 const route=row=>{const positions=new Map(local.positions),rank=new Map(local.rank);
  positions.set('\0exit',vertical?{x:row-.5,y:local.height-4,w:1,h:1}:{x:local.width-4,y:row-.5,w:1,h:1});rank.set('\0exit',last);
  return routeScenarioForwardEdge({source:id,target:'\0exit'},{...local,positions,rank,columns:undefined},vertical);};
 let routed=route(vertical?q.x+q.w/2:q.y+q.h/2);
 // End at the height reached after the last card, so the run to the border is straight.
 const before=routed.segments[routed.segments.length-1][0],reached=vertical?before.x:before.y;
 if(Math.abs(reached-(vertical?q.x+q.w/2:q.y+q.h/2))>.5)routed=route(reached);
 const at=point=>`${point.x+box.ox} ${point.y+box.oy}`;
 return {path:`M ${at(routed.segments[0][0])} `+routed.segments.map(([,c1,c2,end])=>`C ${at(c1)}, ${at(c2)}, ${at(end)}`).join(' '),row:(vertical?box.ox:box.oy)+reached};
}
function drawLinks(list,layout,parent,vertical,inner=false,frames=new Map()){
 // Lines of one layout: the stage map, or the inside of one open stage.
 const routing={},pairCounts=new Map(),pairUsed=new Map();
 const ends=link=>inner?[text(link.source),text(link.target)]:[text(link.outerSource??link.source),text(link.outerTarget??link.target)];
 const cross=id=>{const p=positions.get(text(id));return p?(vertical?p.x+p.w/2:p.y+p.h/2):undefined;};
 for(const link of list){const key=JSON.stringify(ends(link));pairCounts.set(key,(pairCounts.get(key)||0)+1);}
 for(const link of list){const [source,target]=ends(link),a=layout.positions.get(source),key=JSON.stringify([source,target]),pairIndex=pairUsed.get(key)||0;pairUsed.set(key,pairIndex+1);
  const bend=(pairIndex-(pairCounts.get(key)-1)/2)*28,port=Math.max(-18,Math.min(18,bend/2));let path;
  if(source===target){const reach=65+pairIndex*24;let start,c1,c2,end;
   if(vertical){start={x:a.x+a.w/2,y:a.y+a.h};c1={x:start.x,y:start.y+reach};c2={x:a.x+a.w+reach,y:a.y+a.h/2+reach};end={x:a.x+a.w+3,y:a.y+a.h/2};}
   else{start={x:a.x+a.w,y:a.y+a.h/2};c1={x:start.x+reach,y:start.y};c2={x:a.x+a.w/2+reach,y:a.y-reach};end={x:a.x+a.w/2,y:a.y-3};}
   path=`M ${start.x} ${start.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${end.x} ${end.y}`;}
  else{// A line to or from an open stage meets the operation's row on the frame border.
   // The entry operation is first inside its frame, so the line continues straight to it.
   const entry=target!==text(link.target)?positions.get(text(link.target)):null;
   const exit=source!==text(link.source)&&frames.has(source)?frameExit(text(link.source),frames.get(source),vertical):null;
   const anchors={source:exit?exit.row:undefined,target:target!==text(link.target)?cross(link.target):undefined,
    targetEdge:entry?(vertical?entry.y:entry.x):undefined};
   path=routeScenarioEdge({...link,source,target},layout,vertical,routing,exit?0:port,bend,anchors).path;
   if(exit)path=exit.path+' L '+path.replace(/^M\s*/,'');}
  const group=svg('g',{'class':'edge edge-'+evidenceStatus(link.evidence_status),tabindex:0,role:'button','data-edge-id':text(link.id),'data-transition-ids':JSON.stringify((link.transitions||[link]).map(edge=>text(edge.id))),'aria-label':'Переход: '+itemName(text(link.source))+' → '+itemName(text(link.target))});
  group.append(svg('path',{d:path,'class':'edge-visible','marker-end':'url(#arrow)'}),svg('path',{d:path,'class':'edge-hit'}));
  group.addEventListener('click',()=>selectDisplayLink(link));group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();selectDisplayLink(link);}});
  parent.append(group);edgeGroups.set(text(link.id),group);
 }
}
function draw(){
 const focus=document.activeElement?.closest?.('#nodes,#connections')?focusReference():null;
 const nodeContainer=$('nodes');nodeContainer.replaceChildren();$('connections').replaceChildren();positions.clear();nodeButtons.clear();edgeGroups.clear();frameElements.clear();
 if(!nodes.length){$('empty-map').hidden=false;$('canvas-size').hidden=true;return;}else{$('empty-map').hidden=true;$('canvas-size').hidden=false;}
 const vertical=$('viewport').clientWidth<740,sizes=new Map(),measured=[],frames=[];
 for(const item of display.list){
  if(item.type==='stage'&&item.open){frames.push(item);for(const id of item.members)measured.push(operationCard(byId.get(id)));}
  else if(item.type==='stage'){const button=stageCard(item);nodeContainer.append(button);measured.push({id:item.id,kind:'stage',button});}
  else measured.push(operationCard(item.node));
 }
 // Cards of one kind take the largest card's size, so the map reads as a uniform grid.
 const largest=filter=>{const cards=measured.filter(filter);return {w:Math.max(0,...cards.map(item=>item.button.offsetWidth)),h:Math.max(0,...cards.map(item=>item.button.offsetHeight))};};
 const cardSize=largest(item=>item.kind!=='decision'&&item.kind!=='stage'),stageSize=largest(item=>item.kind==='stage');
 for(const {id,kind,button} of measured){
  const size=kind==='stage'?stageSize:cardSize;
  if(kind!=='decision'){button.style.width=size.w+'px';button.style.height=size.h+'px';}
  const w=button.offsetWidth,h=button.offsetHeight,surface=svg('svg',{'class':'node-surface',viewBox:'0 0 '+w+' '+h,'aria-hidden':'true',focusable:'false'});
  if(kind==='decision')surface.append(svg('path',{d:`M ${w/2} 1 L ${w-1} ${h/2} L ${w/2} ${h-1} L 1 ${h/2} Z`}));
  else if(kind==='stage')surface.append(svg('rect',{'class':'stage-stack',x:7,y:7,width:w-8,height:h-8,rx:12}),svg('rect',{x:1,y:1,width:w-8,height:h-8,rx:12}));
  else surface.append(svg('rect',{x:1,y:1,width:w-2,height:h-2,rx:kind==='start'||kind==='end'?Math.min(38,h/2):12}));
  button.prepend(surface);nodeButtons.set(id,button);sizes.set(id,{w,h});
 }
 // An open stage lays out its own operations first and takes their size on the stage map.
 const inside=new Map();
 for(const item of frames){
  const local=layoutScenarioGraph(item.members.map(id=>({id,kind:byId.get(id).kind})),display.links.filter(link=>link.inner===item.id),sizes,vertical,[text(item.stage.entry_operation_id)]);
  const head=stageFrameHead(item),w=Math.max(local.width,340);head.style.width=(w-32)+'px';
  const top=head.offsetHeight+14;sizes.set(item.id,{w,h:top+local.height-14});
  inside.set(item.id,{local,head,top,dx:(w-local.width)/2});
 }
 const layout=layoutScenarioGraph(display.list.map(item=>({id:item.id,kind:item.type==='stage'?'stage':item.node.kind})),
  display.links.filter(link=>!link.inner).map(link=>({id:link.id,source:link.outerSource??link.source,target:link.outerTarget??link.target,kind:link.kind})),
  sizes,vertical,array(scenario.entry_operation_ids).map(display.outer));
 graphWidth=layout.width;graphHeight=layout.height;
 for(const [id,p] of layout.positions){positions.set(id,p);const button=nodeButtons.get(id);if(button){button.style.left=p.x+'px';button.style.top=p.y+'px';}}
 for(const [id,box] of inside){const p=positions.get(id);box.head.style.left=(p.x+16)+'px';box.head.style.top=(p.y+14)+'px';box.ox=p.x+box.dx;box.oy=p.y+box.top-14;
  for(const [member,q] of box.local.positions){const at={...q,x:p.x+box.dx+q.x,y:p.y+box.top-14+q.y};positions.set(member,at);const button=nodeButtons.get(member);button.style.left=at.x+'px';button.style.top=at.y+'px';}}
 $('stage').style.width=graphWidth+'px';$('stage').style.height=graphHeight+'px';
 const drawing=$('connections');drawing.setAttribute('width',graphWidth);drawing.setAttribute('height',graphHeight);drawing.setAttribute('viewBox','0 0 '+graphWidth+' '+graphHeight);
 const defs=svg('defs');const marker=svg('marker',{id:'arrow',viewBox:'0 0 10 10',refX:8,refY:5,markerWidth:6,markerHeight:6,orient:'auto-start-reverse'});marker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#8b9ca7'}));defs.append(marker);drawing.append(defs);
 // Frames lie under every line; each frame's own lines are drawn in its coordinates.
 for(const [id,box] of inside){const p=positions.get(id),surface=svg('g',{'class':'stage-frame','data-stage-id':text(display.items.get(id).stage.id),'aria-hidden':'true'});
  surface.append(svg('rect',{x:p.x+1,y:p.y+1,width:p.w-2,height:p.h-2,rx:16}));drawing.append(surface);frameElements.set(id,{surface,head:box.head});}
 drawLinks(display.links.filter(link=>!link.inner),layout,drawing,vertical,false,inside);
 for(const [id,box] of inside){const group=svg('g',{transform:`translate(${box.ox} ${box.oy})`});drawing.append(group);
  drawLinks(display.links.filter(link=>link.inner===id),box.local,group,vertical,true);}
 applyZoom(zoom);updateHighlight();if(focus)restoreMapFocus(focus);
}
function stageCard(item){
 const stage=item.stage,members=item.members.map(id=>byId.get(id)).filter(Boolean),button=el('button','node kind-stage');button.type='button';button.dataset.stageId=text(stage.id);
 const ends=stageOutcomes(members),semantics=stageSemantics(members,validEdges);
 const findings=gaps.filter(g=>array(g.node_ids).some(id=>item.members.includes(text(id)))).length,content=el('span','node-content');
 content.append(el('span','node-stage-kicker','Бизнес-этап · '+members.length+' '+plural(members.length,['операция','операции','операций'])),el('span','node-name',stage.name||stage.id));
 if(stage.result)content.append(el('span','node-stage-result',stage.result));
 if(ends.length){const row=el('span','stage-outcomes'),count=value=>value>1?' · '+value:'';
  for(const end of ends){const badge=el('span','stage-outcome '+end.tone,end.label+count(end.count));badge.dataset.outcome=end.outcome;badge.dataset.conditional=String(end.conditional);row.append(badge);}content.append(row);}
 if(semantics.length){const row=el('span','stage-outcomes stage-semantics');for(const label of new Set(semantics.map(item=>item.short)))row.append(el('span','stage-outcome',label));content.append(row);}
 if(findings)content.append(el('span','node-gap','Не установлено: '+findings));
 button.append(content);button.setAttribute('aria-pressed','false');button.setAttribute('aria-expanded','false');
 button.setAttribute('aria-label','Бизнес-этап: '+(stage.name||stage.id)+'. Операций: '+members.length+(ends.length?'. '+ends.map(end=>end.label+(end.count>1?' · '+end.count:'')).join('. '):'')+(semantics.length?'. '+[...new Set(semantics.map(item=>item.short))].join('. '):'')+'. Нажмите, чтобы раскрыть операции этапа');
 button.addEventListener('click',()=>expandStage(stage));return button;
}
function minimumZoom(){return Math.max(.001,Math.min(.16,(availableWidth()-24)/graphWidth,($('viewport').clientHeight-24)/graphHeight));}
function applyZoom(value,anchor){
 const view=$('viewport'),stage=$('stage'),canvas=$('canvas-size');
 const next=Math.min(1.7,Math.max(minimumZoom(),value));if(!Number.isFinite(next))return;
 // Capture the point in graph coordinates before layout can clamp scroll offsets.
 const point=anchor?{x:(view.scrollLeft+anchor.x-(parseFloat(stage.style.left)||0))/zoom,y:(view.scrollTop+anchor.y-(parseFloat(stage.style.top)||0))/zoom}:null;
 if(anchor&&next===zoom)return;zoom=next;
 let scrollX=0,scrollY=0;stage.style.transform='scale('+zoom+')';
 // Scrollbars can change the opposite axis; settle sizes before setting scroll.
 for(let pass=0;pass<3;pass++){
  const clientWidth=view.clientWidth,clientHeight=view.clientHeight,visibleWidth=availableWidth();
  const padX=Math.max(0,(visibleWidth-graphWidth*zoom)/2),padY=Math.max(0,(clientHeight-graphHeight*zoom)/2);
  let left=padX,top=padY;
  if(point){scrollX=point.x*zoom+left-anchor.x;scrollY=point.y*zoom+top-anchor.y;
   // A small graph still needs movable whitespace to keep the cursor anchor.
   if(scrollX<0){left-=scrollX;scrollX=0;}if(scrollY<0){top-=scrollY;scrollY=0;}
  }
  stage.style.left=left+'px';stage.style.top=top+'px';
  const width=graphWidth*zoom+left+padX+(clientWidth-visibleWidth),height=graphHeight*zoom+top+padY;
  canvas.style.width=Math.max(width,point?Math.ceil(scrollX+clientWidth):0)+'px';
  canvas.style.height=Math.max(height,point?Math.ceil(scrollY+clientHeight):0)+'px';
  if(!point||(view.clientWidth===clientWidth&&view.clientHeight===clientHeight))break;
 }
 if(point){view.scrollLeft=scrollX;view.scrollTop=scrollY;}
 $('zoom-value').textContent=Math.round(zoom*100)+'%';$('zoom-out').disabled=zoom<=minimumZoom();$('zoom-in').disabled=zoom>=1.7;
}
// Fit across the flow only: cards stay readable and the flow scrolls.
function fit(){if(!nodes.length)return;const view=$('viewport'),vertical=view.clientWidth<740;applyZoom(Math.min(vertical?(availableWidth()-24)/graphWidth:(view.clientHeight-24)/graphHeight,1));view.scrollTo(0,0);}
function initialView(){if(!nodes.length)return;if(selected?.type==='group'){const group=groupById.get(text(selected.id));if(group){frameGroup(group);return;}}if(selected?.type==='stage'){const stage=businessStageById.get(text(selected.id));if(stage){frameStage(stage);return;}}const fitted=Math.min((availableWidth()-24)/graphWidth,($('viewport').clientHeight-24)/graphHeight,1);applyZoom(Math.max(.8,fitted));if(fitted<.8)centerNode(byId.get(text(array(scenario.entry_operation_ids)[0]))||nodes.find(n=>legacy&&n.kind==='start')||nodes[0]);else $('viewport').scrollTo(0,0);}
function zoomBy(factor){const view=$('viewport');applyZoom(zoom*factor,{x:availableWidth()/2,y:view.clientHeight/2});}
$('zoom-in').addEventListener('click',()=>zoomBy(1.2));$('zoom-out').addEventListener('click',()=>zoomBy(1/1.2));$('fit').addEventListener('click',fit);
$('reset').addEventListener('click',()=>{if(expandedStages.size){expandedStages.clear();display=buildDisplay();draw();}closeInspector(false);showOverview();updateHighlight();applyZoom(1);if(nodes.length)centerNode(byId.get(text(array(scenario.entry_operation_ids)[0]))||nodes.find(n=>legacy&&n.kind==='start')||nodes[0]);announce('Выбор сброшен. Масштаб 100 процентов.');});
const viewport=$('viewport');viewport.addEventListener('keydown',event=>{if(event.target!==viewport)return;if(event.key==='+'||event.key==='='){event.preventDefault();zoomBy(1.2);}else if(event.key==='-'){event.preventDefault();zoomBy(1/1.2);}else if(event.key==='0'){event.preventDefault();fit();}else if(event.key==='Escape'){selected=null;showOverview();updateHighlight();}});
let gestureZoom=null,lastCursor=null;
viewport.addEventListener('pointermove',event=>{lastCursor={x:event.clientX,y:event.clientY};});
function cursorAnchor(event){
 const rect=viewport.getBoundingClientRect();let x=event.clientX,y=event.clientY;
 if(!Number.isFinite(x)||!Number.isFinite(y)||x<rect.left||x>rect.right||y<rect.top||y>rect.bottom){x=lastCursor?.x??rect.left+availableWidth()/2;y=lastCursor?.y??rect.top+viewport.clientHeight/2;}
 return {x:x-rect.left-viewport.clientLeft,y:y-rect.top-viewport.clientTop};
}
viewport.addEventListener('wheel',event=>{
 if(!nodes.length||!Number.isFinite(event.deltaY))return;
 if(gestureZoom!==null){event.preventDefault();return;}if(event.deltaY===0)return;
 event.preventDefault();
 const unit=event.deltaMode===1?16:event.deltaMode===2?viewport.clientHeight:1;
 const delta=Math.max(-240,Math.min(240,event.deltaY*unit));
 applyZoom(zoom*Math.exp(-delta*(event.ctrlKey ? .01 : .002)),cursorAnchor(event));
},{passive:false});
// Safari supplies GestureEvents; Chromium/Firefox trackpad pinch uses ctrl+wheel.
viewport.addEventListener('gesturestart',event=>{if(!nodes.length)return;event.preventDefault();gestureZoom=zoom;},{passive:false});
viewport.addEventListener('gesturechange',event=>{if(gestureZoom===null)return;event.preventDefault();if(Number.isFinite(event.scale)&&event.scale>0)applyZoom(gestureZoom*event.scale,cursorAnchor(event));},{passive:false});
viewport.addEventListener('gestureend',event=>{if(gestureZoom===null)return;event.preventDefault();gestureZoom=null;},{passive:false});
let drag=null;viewport.addEventListener('pointerdown',event=>{if(event.button!==0||event.pointerType==='touch'||event.target.closest('button,.edge'))return;drag={id:event.pointerId,x:event.clientX,y:event.clientY,left:viewport.scrollLeft,top:viewport.scrollTop};viewport.setPointerCapture(event.pointerId);viewport.classList.add('dragging');});viewport.addEventListener('pointermove',event=>{if(!drag||drag.id!==event.pointerId)return;viewport.scrollLeft=drag.left-(event.clientX-drag.x);viewport.scrollTop=drag.top-(event.clientY-drag.y);});const stopDrag=()=>{drag=null;viewport.classList.remove('dragging');};viewport.addEventListener('pointerup',stopDrag);viewport.addEventListener('pointercancel',stopDrag);viewport.addEventListener('lostpointercapture',stopDrag);
// Under a report tab the map keeps the user's zoom and scroll; only its geometry is redrawn.
let resizeFrame=0;window.addEventListener('resize',()=>{cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>{
 if($('panel-map').inert){const view=$('viewport'),left=view.scrollLeft,top=view.scrollTop;draw();view.scrollLeft=left;view.scrollTop=top;}else{draw();initialView();}});});
showOverview();draw();initialView();updateHighlight();
 if(document.fonts?.ready)document.fonts.ready.then(()=>{const view=$('viewport'),left=view.scrollLeft,top=view.scrollTop;draw();view.scrollLeft=left;view.scrollTop=top;});
/*__REPORT_JS__*/
})();
'''.replace('__GRAPH_LAYOUT_JS__', _GRAPH_LAYOUT_JS + _STAGE_PROJECTION_JS)
