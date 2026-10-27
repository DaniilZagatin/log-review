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

from pko.render.dpss_theme import embedded_fonts


def _script_json(value: Any) -> str:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False)
            .replace("&", "\\u0026").replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def render_scenario_map(model: dict[str, Any]) -> str:
    """Return one self-contained HTML document without modifying ``model``.

    Unknown evidence or run statuses display as unknown, never as confirmed.
    Empty/partial models remain inspectable; facts are not created by rendering.
    """
    if not isinstance(model, dict):
        raise TypeError("scenario map must be a dictionary")
    scenario = model.get("scenario") or {}
    run = model.get("run") or {}
    title = str(scenario.get("title") or "Карта сценария")
    values = {
        "TITLE": html.escape(title, quote=True), "FONTS": embedded_fonts(),
        "STYLE": _STYLE + _NOTATION_STYLE, "SCRIPT": _SCRIPT, "MODEL": _script_json(model),
        "LEGACY_HIDDEN": "" if str(model.get("schema_version", "1.0")).split(".")[0] != "2" else " hidden",
        "RUN_NOTE": html.escape(str(run.get("reason") or ""), quote=True),
        "RUN_NOTE_HIDDEN": "" if run.get("reason") and run.get("status") in ("partial", "failed") else " hidden",
    }
    # A single pass also keeps placeholder-like words in user text literal.
    return re.sub(r"__(TITLE|FONTS|STYLE|SCRIPT|MODEL|LEGACY_HIDDEN|RUN_NOTE|RUN_NOTE_HIDDEN)__",
                  lambda match: values[match.group(1)], _DOCUMENT)


_DOCUMENT = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light"><title>__TITLE__ · Белка</title>
__FONTS__<style>__STYLE__</style></head>
<body><a class="skip" href="#workspace">Перейти к карте</a>
<header class="page-header">
<div class="heading-line"><h1 id="page-title">__TITLE__</h1><div class="overview" id="overview"></div></div>
<p id="scenario-description" class="description"></p>
<p id="legacy-note" class="legacy-note"__LEGACY_HIDDEN__>Архивная карта прежней методологии. Её узлы и группировки не подтверждают атомарность по правилам версии 2.0.</p>
<p id="run-note" class="run-note"__RUN_NOTE_HIDDEN__>__RUN_NOTE__</p></header>
<main id="workspace" tabindex="-1">
<div class="workspace-layout"><div class="main-panels">
<section id="panel-map" aria-label="Подробная карта сценария">
<div class="map-navigation"><label class="stage-picker" for="stage-select"><span>Этап</span><select id="stage-select" aria-label="Выбрать этап сценария"><option value="">Все этапы</option></select></label><button id="clear-selection" type="button" hidden>Снять выделение</button><button id="notation-toggle" type="button" aria-expanded="false" aria-controls="notation-panel">Обозначения</button></div>
<section id="notation-panel" class="map-popover notation-panel" aria-label="Обозначения карты" hidden><h2>Как читать карту</h2><div id="notation-items"></div><p>Каждая карточка — отдельная операция. Стрелка показывает продолжение исполнения. Условия выбора следующих операций указаны в карточке исходной операции; на стрелках подписей нет.</p><p>Штриховая связь — вывод по коду, пунктирная — не установлена. Направление стрелки сохраняет возвраты и циклы.</p><p>Этап служит навигации. Выбор этапа выделяет его операции; выбор операции — все пути к ней и после неё. Это возможные маршруты карты. Смысл, внешнее взаимодействие и основания доступны в карточке.</p></section>
<div class="map-toolbar">
<div class="map-actions"><button type="button" id="zoom-out" aria-label="Уменьшить масштаб">−</button><output id="zoom-value" aria-label="Масштаб">100%</output><button type="button" id="zoom-in" aria-label="Увеличить масштаб">+</button><button type="button" id="fit">Вписать</button><button type="button" id="reset">Сбросить</button></div></div>
<div id="viewport" class="viewport" tabindex="0" aria-label="Карта сценария. Колесо мыши и pinch меняют масштаб относительно курсора; перетаскивание фона перемещает схему. Tab выбирает операции."><div id="canvas-size"><div id="stage"><svg id="connections" xmlns="http://www.w3.org/2000/svg" aria-label="Переходы между операциями"></svg><div id="nodes"></div></div></div><div id="empty-map" class="empty" hidden>Карта пока не содержит операций. Причина и незавершённые участки указаны в результатах исследования.</div></div>
</section>
</div><aside id="inspector" class="inspector" aria-label="Описание выбранного элемента" hidden><div class="inspector-head"><span>Описание элемента</span><button type="button" id="close-inspector" aria-label="Закрыть описание">×</button></div><div id="inspector-content"></div></aside></div>
<p class="footnote">Карта восстановлена по переданным материалам. Подтверждение в коде не гарантирует поведение работающих систем и полноту бизнес-процесса.</p>
</main><div id="announcement" class="sr-only" role="status" aria-live="polite"></div>
<noscript><p class="empty">Для просмотра интерактивной карты включите JavaScript. Все данные находятся внутри этого HTML-файла; подключение к сети не требуется.</p></noscript>
<script id="scenario-model" type="application/json">__MODEL__</script><script>__SCRIPT__</script></body></html>'''


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
 // Long jumps bypass intervening cards just as cycles bypass the graph body.
 const outerEdges=new Set(valid.filter(e=>rank.get(key(e.target))-rank.get(key(e.source))!==1).map(e=>key(e.id)));
 const outerSpace=outerEdges.size?80+outerEdges.size*24:0,positions=new Map();
 if(!nodes.length)return {rank,positions,outerEdges,width:600,height:400};
 const secondaryMin=Math.min(...nodes.map(n=>centers.get(key(n.id))-secondarySize(key(n.id))/2)),secondaryMax=Math.max(...nodes.map(n=>centers.get(key(n.id))+secondarySize(key(n.id))/2));let primary=32;
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
function routeScenarioOuterEdge(edge,layout,vertical,index=0) {
 const a=layout.positions.get(String(edge.source)),b=layout.positions.get(String(edge.target));
 const sourceRank=layout.rank.get(String(edge.source)),targetRank=layout.rank.get(String(edge.target));
 // Leave the occupied rank through its free gutter before entering the outer
 // channel. A single Bezier from a lower branch's top crosses its upper sibling.
 const trailing=rank=>Math.max(...[...layout.positions].filter(([id])=>layout.rank.get(id)===rank).map(([,p])=>vertical?p.y+p.h:p.x+p.w));
 let points,label;
 if(vertical){const channel=layout.width-28-index*24,from=trailing(sourceRank)+24,to=b.y-24;points=[{x:a.x+a.w/2,y:a.y+a.h},{x:a.x+a.w/2,y:from},{x:channel,y:from},{x:channel,y:to},{x:b.x+b.w/2,y:to},{x:b.x+b.w/2,y:b.y-3}];label={x:channel-9,y:(from+to)/2};}
 else{const channel=28+index*24,from=trailing(sourceRank)+28,to=b.x-28;points=[{x:a.x+a.w,y:a.y+a.h/2},{x:from,y:a.y+a.h/2},{x:from,y:channel},{x:to,y:channel},{x:to,y:b.y+b.h/2},{x:b.x-3,y:b.y+b.h/2}];label={x:(from+to)/2,y:channel-9};}
 return {...smoothScenarioPath(points),label,outer:true};
}
function routeScenarioForwardEdge(edge,layout,vertical,port=0,bend=0) {
 const a=layout.positions.get(String(edge.source)),b=layout.positions.get(String(edge.target)),rank=layout.rank.get(String(edge.source));
 const trailing=Math.max(...[...layout.positions].filter(([id])=>layout.rank.get(id)===rank).map(([,p])=>vertical?p.y+p.h:p.x+p.w));
 // Cards in a rank have unequal heights/widths. Turn only in the shared free
 // gutter, otherwise a diagonal from a short card crosses its taller sibling.
 const lane=Math.max(-8,Math.min(8,bend/3));let points;
 if(vertical){const x=a.x+a.w/2+port,z=b.x+b.w/2+port;points=[{x,y:a.y+a.h},{x,y:trailing+16+lane},{x:z,y:b.y-16+lane},{x:z,y:b.y-3}];}
 else{const y=a.y+a.h/2+port,z=b.y+b.h/2+port;points=[{x:a.x+a.w,y},{x:trailing+22+lane,y},{x:b.x-22+lane,y:z},{x:b.x-3,y:z}];}
 const route=smoothScenarioPath(points),[s,c1,c2,e]=route.segments[1];
 return {...route,label:{x:(s.x+3*c1.x+3*c2.x+e.x)/8,y:(s.y+3*c1.y+3*c2.y+e.y)/8-9},outer:false};
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
const kindNames={start:'Начало',action:'Действие',decision:'Условие',fork:'Параллельное выполнение',join:'Объединение',wait:'Ожидание',end:'Завершение',external:'Внешний участок'};
const operationKinds={action:'Действие',validation:'Проверка',read:'Чтение данных',write:'Запись данных',integration:'Вызов системы',publish:'Публикация события',wait:'Ожидание',calculation:'Расчёт',transform:'Преобразование данных',error_handling:'Обработка отказа / ошибки'};
const outcomeNames={success:'Успешное завершение',refusal:'Бизнес-отказ',failure:'Технический сбой',cancelled:'Отмена',deferred:'Отложенная обработка',unknown:'Завершение · исход не уточнён'};
const nodeKindLabel=node=>!legacy?'Атомарная операция':node.kind==='end'&&node.outcome?(outcomeNames[node.outcome]||outcomeNames.unknown):node.kind==='action'?(operationKinds[operationsByNode.get(text(node.id))?.[0]?.operation.kind]||kindNames.action):(kindNames[node.kind]||'Шаг сценария');
function notationIcon(kind){
 const icon=svg('svg',{viewBox:'0 0 24 24','aria-hidden':'true',focusable:'false',fill:'none',stroke:'currentColor','stroke-width':1.6,'stroke-linecap':'round','stroke-linejoin':'round'});
 if(kind==='start'||kind==='end'){icon.append(svg('circle',{cx:12,cy:12,r:9}));if(kind==='end')icon.append(svg('circle',{cx:12,cy:12,r:6}));}
 else if(kind==='decision')icon.append(svg('path',{d:'M12 2 L22 12 L12 22 L2 12 Z'}));
 else if(kind==='wait'){icon.append(svg('circle',{cx:12,cy:12,r:9}),svg('path',{d:'M12 6 V12 L16 15'}));}
 else if(kind==='fork'||kind==='join'){icon.append(svg('path',{d:kind==='fork'?'M3 12 H10 M10 4 V20 M10 5 H21 M10 12 H21 M10 19 H21':'M3 5 H14 M3 12 H14 M3 19 H14 M14 4 V20 M14 12 H21'}));}
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
let selected=null, zoom=1, graphWidth=1000, graphHeight=600, restoreFocus=null;

$('page-title').textContent=scenario.title || 'Карта сценария';
const fullDescription=text(scenario.description).trim();
$('scenario-description').textContent=fullDescription?(fullDescription.length>260?fullDescription.slice(0,257)+'…':fullDescription):'Описание выбранного варианта сценария не задано.';
for(const [count,label,inspect] of [[nodes.length,plural(nodes.length,legacy?['узел','узла','узлов']:['операция','операции','операций'])],[validEdges.length,plural(validEdges.length,['переход','перехода','переходов'])],[gaps.length,plural(gaps.length,['пробел','пробела','пробелов']),gaps.length>0]]){
 const entry=el(inspect?'button':'span',inspect?'findings-toggle':'');if(inspect){entry.type='button';entry.setAttribute('aria-label','Посмотреть пробелы исследования');entry.addEventListener('click',showFindings);}entry.append(el('b','',count),document.createTextNode(label)); $('overview').append(entry);
}
function field(container,label,value){ if(!text(value).trim())return; const dl=el('dl','field');dl.append(el('dt','',label),el('dd','',value));container.append(dl); }
function listField(container,label,values){ const items=array(values);if(!items.length)return;const section=el('div','field');section.append(el('div','muted',label));const ul=el('ul');for(const item of items)ul.append(el('li','',item));section.append(ul);container.append(section); }
if(run.reason&&(runState==='partial'||runState==='failed')){$('run-note').textContent=text(run.reason);$('run-note').hidden=false;}
if(validationIssues.length||omissions.length){const notice=el('button','checks-notice');notice.type='button';notice.textContent=[omissions.length?'Пропуски материалов: '+omissions.length:'',validationIssues.length?'Незакрытые проверки: '+validationIssues.length:''].filter(Boolean).join(' · ')+' — посмотреть';notice.addEventListener('click',showFindings);$('panel-map').insertBefore(notice,$('viewport'));}

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
function closePopovers(){for(const name of ['notation']){$(name+'-panel').hidden=true;$(name+'-toggle').setAttribute('aria-expanded','false');}}
for(const name of ['notation'])$(name+'-toggle').addEventListener('click',()=>{const open=$(name+'-panel').hidden;closePopovers();$(name+'-panel').hidden=!open;$(name+'-toggle').setAttribute('aria-expanded',String(open));});
document.addEventListener('pointerdown',event=>{if(!event.target.closest('.map-popover,.map-navigation'))closePopovers();});
document.addEventListener('keydown',event=>{if(event.key!=='Escape')return;for(const name of ['notation'])if(!$(name+'-panel').hidden){closePopovers();$(name+'-toggle').focus();event.preventDefault();break;}});
for(const [kind,label] of (legacy?[['start','Начало сценария'],['action','Атомарное действие'],['decision','Условие и альтернативные ветви'],['fork','Запуск параллельных ветвей'],['join','Синхронизация параллельных ветвей'],['wait','Ожидание события или результата'],['end','Исход: успех, отказ, сбой или отмена'],['external','Внешний участок с ограниченным описанием']]:[['operation','Атомарная операция']])){const row=el('div','notation-item');row.append(notationIcon(kind),el('span','',label));$('notation-items').append(row);}
function showGroup(group){
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
const stages=groups.filter(g=>g.kind==='stage');
for(const stage of stages){const option=el('option','',stage.name);option.value=text(stage.id);$('stage-select').append(option);}
$('stage-select').disabled=!stages.length;
if(!stages.length)$('stage-select').options[0].textContent='Этапы не заданы';
$('stage-select').addEventListener('change',()=>{
 const group=groupById.get($('stage-select').value);closePopovers();closeInspector(false);
 if(group){selected={type:'group',id:group.id};updateHighlight();requestAnimationFrame(()=>frameGroup(group));announce('Выбран этап: '+group.name);}
 else{requestAnimationFrame(initialView);announce('Показаны все этапы');}
});
$('clear-selection').addEventListener('click',()=>closeInspector());
function showOverview(){
 const panel=$('inspector-content'); panel.replaceChildren();panel.append(el('div','inspector-kicker','ВАРИАНТ СЦЕНАРИЯ'),el('h2','',scenario.title || 'Карта процесса'));
 panel.append(el('p','muted',model.summary || 'Выберите операцию, чтобы увидеть её смысл, вход, результат, условия продолжения и подтверждения в коде.'));
 field(panel,'Описание варианта сценария',fullDescription);field(panel,'Начало',scenario.start);field(panel,'Завершение',scenario.end);
 panel.append(el('p','muted',legacy?'Архивный граф сохраняет исходные узлы. Соответствие новой методологии атомарных операций не проверено.':'Граф показывает операции выбранного сценария. Условия продолжения находятся в карточке операции. Наличие цитат не доказывает полноту карты.'));
 if(openFindings)relatedButton(panel,'Посмотреть пробелы и проверки ('+openFindings+')',showFindings);
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
 const panel=$('inspector-content');panel.replaceChildren();
 const meta=el('div','operation-meta');meta.append(el('div','inspector-kicker',nodeKindLabel(node)));
 const ancestry=nodeGroups(node);if(ancestry.length){const links=el('div','group-links');for(const group of ancestry)relatedButton(links,(group.kind==='stage'?'Этап: ':'Группа: ')+group.name,()=>selectGroup(group));meta.append(links);}
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
function updateHighlight(){
 const nodeScope=selected?.type==='node'?scenarioNodeScope(selected.id,validEdges):null;
 const scopeIds=nodeScope?.nodes||new Set();
 if(selected?.type==='edge'){const e=selected.edge;scopeIds.add(text(e.source));scopeIds.add(text(e.target));}
 if(selected?.type==='group'){const group=groupById.get(text(selected.id));if(group)for(const node of groupMembers(group))scopeIds.add(text(node.id));}
 const selectedGroup=selected?.type==='group'?groupById.get(text(selected.id)):null;
 $('clear-selection').hidden=!selected;
 const selectedStage=selectedGroup?.kind==='stage'?selectedGroup:null;
 $('stage-select').value=selectedStage?text(selectedStage.id):'';
 for(const node of nodes){const button=nodeButtons.get(text(node.id));if(button){button.classList.toggle('in-scope',!!selected&&scopeIds.has(text(node.id)));button.classList.toggle('selected',selected?.type==='node'&&text(selected.id)===text(node.id));button.setAttribute('aria-pressed',String(selected?.type==='node'&&text(selected.id)===text(node.id)));button.classList.toggle('dimmed',!!selected&&!scopeIds.has(text(node.id)));}}
 for(const edge of validEdges){const group=edgeGroups.get(text(edge.id));if(!group)continue;const isSelected=selected?.type==='edge'&&text(selected.id)===text(edge.id);const isRelated=selected?.type==='node'?nodeScope.edges.has(text(edge.id)):selected?.type==='group'&&scopeIds.has(text(edge.source))&&scopeIds.has(text(edge.target));group.classList.toggle('in-scope',isRelated);group.classList.toggle('selected',isSelected);group.classList.toggle('dimmed',!!selected&&!isSelected&&!isRelated);}
}
function announce(value){$('announcement').textContent=value;}
function availableWidth(){const view=$('viewport');return Math.max(180,view.clientWidth-(!$('inspector').hidden&&window.innerWidth>700?$('inspector').offsetWidth:0));}
function openInspector(){if($('inspector').hidden)restoreFocus=document.activeElement;$('inspector').hidden=false;$('inspector-content').scrollTop=0;applyZoom(zoom);if(window.innerWidth<=700)$('close-inspector').focus({preventScroll:true});}
function closeInspector(restore=true){selected=null;$('inspector').hidden=true;updateHighlight();applyZoom(zoom);if(restore&&restoreFocus&&document.contains(restoreFocus))restoreFocus.focus({preventScroll:true});restoreFocus=null;}
function selectNode(node,center=false){closePopovers();selected={type:'node',id:node.id};showNode(node);openInspector();updateHighlight();announce('Выбрана операция: '+(node.name||node.id));if(center){requestAnimationFrame(()=>centerNode(node));}else if(window.innerWidth>700)centerNode(node);}
function selectEdge(edge){closePopovers();selected={type:'edge',id:edge.id,edge};showEdge(edge);openInspector();updateHighlight();announce('Выбран переход: '+(edge.description||edgeNames[edge.kind]||'Связь'));}
function centerNode(node){const p=positions.get(text(node.id));if(!p)return;const view=$('viewport'),stage=$('stage');view.scrollTo({left:Math.max(0,(p.x+p.w/2)*zoom+(parseFloat(stage.style.left)||0)-availableWidth()/2),top:Math.max(0,(p.y+p.h/2)*zoom+(parseFloat(stage.style.top)||0)-view.clientHeight/2)});}
function frameGroup(group){
 const members=groupMembers(group).map(n=>positions.get(text(n.id))).filter(Boolean);if(!members.length)return;
 const left=Math.min(...members.map(p=>p.x)),top=Math.min(...members.map(p=>p.y)),right=Math.max(...members.map(p=>p.x+p.w)),bottom=Math.max(...members.map(p=>p.y+p.h));
 const view=$('viewport');applyZoom(Math.min(1,(availableWidth()-48)/(right-left),Math.max(40,view.clientHeight-96)/(bottom-top)));
 const stage=$('stage');view.scrollTo({left:Math.max(0,(left+right)/2*zoom+(parseFloat(stage.style.left)||0)-availableWidth()/2),top:Math.max(0,(top+bottom)/2*zoom+(parseFloat(stage.style.top)||0)-(view.clientHeight-48)/2)});
}
$('close-inspector').addEventListener('click',()=>closeInspector());
document.addEventListener('keydown',event=>{if(event.defaultPrevented)return;if(event.key==='Escape'&&!$('inspector').hidden){event.preventDefault();closeInspector();}});

function showFindings(){
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
function draw(){
 const nodeContainer=$('nodes');nodeContainer.replaceChildren();$('connections').replaceChildren();positions.clear();nodeButtons.clear();edgeGroups.clear();
 if(!nodes.length){$('empty-map').hidden=false;$('canvas-size').hidden=true;return;}else{$('empty-map').hidden=true;$('canvas-size').hidden=false;}
 const vertical=$('viewport').clientWidth<740,sizes=new Map();
 for(const node of nodes){
  const id=text(node.id),kind=!legacy?'operation':Object.prototype.hasOwnProperty.call(kindNames,node.kind)?node.kind:'action';
  const button=el('button','node kind-'+kind);button.type='button';button.dataset.nodeId=id;
  button.setAttribute('aria-label',nodeKindLabel(node)+': '+(node.name||node.id)+(legacy?'. '+systemName(node):'')+'. '+evidenceNames[evidenceStatus(node.evidence_status)]);button.setAttribute('aria-pressed','false');
  const content=el('span','node-content');
  content.append(el('span','node-name',node.name||node.id));
  const group=groupById.get(text(node.group_id));if(group){const label=el('span','node-group',group.name);label.title=nodeGroups(node).map(g=>g.name).join(' / ');content.append(label);}
  if(legacy)content.append(el('span','node-system','АС · '+systemName(node)));
  content.append(el('span','node-status '+evidenceStatus(node.evidence_status),evidenceNames[evidenceStatus(node.evidence_status)]));
  const findingCount=gaps.filter(g=>array(g.node_ids).map(text).includes(id)).length;if(findingCount)content.append(el('span','node-gap','Не установлено: '+findingCount));
  button.append(content);button.addEventListener('click',()=>selectNode(node));nodeContainer.append(button);
  if(kind==='decision')button.style.height=Math.max(230,content.offsetHeight*2+32)+'px';
  const w=button.offsetWidth,h=button.offsetHeight,surface=svg('svg',{'class':'node-surface',viewBox:'0 0 '+w+' '+h,'aria-hidden':'true',focusable:'false'});
  if(kind==='decision')surface.append(svg('path',{d:`M ${w/2} 1 L ${w-1} ${h/2} L ${w/2} ${h-1} L 1 ${h/2} Z`}));
  else surface.append(svg('rect',{x:1,y:1,width:w-2,height:h-2,rx:kind==='start'||kind==='end'?Math.min(38,h/2):12}));
  button.prepend(surface);nodeButtons.set(id,button);sizes.set(id,{w,h});
 }
 const layout=layoutScenarioGraph(nodes,validEdges,sizes,vertical,array(scenario.entry_operation_ids)),rank=layout.rank;graphWidth=layout.width;graphHeight=layout.height;
 for(const [id,p] of layout.positions){positions.set(id,p);const button=nodeButtons.get(id);button.style.left=p.x+'px';button.style.top=p.y+'px';}
 $('stage').style.width=graphWidth+'px';$('stage').style.height=graphHeight+'px';
 const drawing=$('connections');drawing.setAttribute('width',graphWidth);drawing.setAttribute('height',graphHeight);drawing.setAttribute('viewBox','0 0 '+graphWidth+' '+graphHeight);
 const defs=svg('defs');const marker=svg('marker',{id:'arrow',viewBox:'0 0 10 10',refX:8,refY:5,markerWidth:6,markerHeight:6,orient:'auto-start-reverse'});marker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#8b9ca7'}));defs.append(marker);drawing.append(defs);
 let backIndex=0;const pairCounts=new Map(),pairUsed=new Map();for(const edge of validEdges){const key=JSON.stringify([edge.source,edge.target]);pairCounts.set(key,(pairCounts.get(key)||0)+1);}
 for(const edge of validEdges){const a=positions.get(text(edge.source)),b=positions.get(text(edge.target)),key=JSON.stringify([edge.source,edge.target]),pairIndex=pairUsed.get(key)||0;pairUsed.set(key,pairIndex+1);const bend=(pairIndex-(pairCounts.get(key)-1)/2)*28,port=Math.max(-18,Math.min(18,bend/2));let start,c1,c2,end,routed;
 if(text(edge.source)===text(edge.target)){const reach=65+pairIndex*24;if(vertical){start={x:a.x+a.w/2,y:a.y+a.h};c1={x:start.x,y:start.y+reach};c2={x:a.x+a.w+reach,y:a.y+a.h/2+reach};end={x:a.x+a.w+3,y:a.y+a.h/2};}else{start={x:a.x+a.w,y:a.y+a.h/2};c1={x:start.x+reach,y:start.y};c2={x:a.x+a.w/2+reach,y:a.y-reach};end={x:a.x+a.w/2,y:a.y-3};}backIndex++;}
 else if(layout.outerEdges.has(text(edge.id)))routed=routeScenarioOuterEdge(edge,layout,vertical,backIndex++);
 else routed=routeScenarioForwardEdge(edge,layout,vertical,port,bend);
 const path=routed?routed.path:`M ${start.x} ${start.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${end.x} ${end.y}`;
 const group=svg('g',{'class':'edge edge-'+evidenceStatus(edge.evidence_status),tabindex:0,role:'button','data-edge-id':text(edge.id),'aria-label':'Переход: '+(byId.get(text(edge.source)).name||edge.source)+' → '+(byId.get(text(edge.target)).name||edge.target)});
  group.append(svg('path',{d:path,'class':'edge-visible','marker-end':'url(#arrow)'}),svg('path',{d:path,'class':'edge-hit'}));
  group.addEventListener('click',()=>selectEdge(edge));group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();selectEdge(edge);}});drawing.append(group);edgeGroups.set(text(edge.id),group);
 }
 applyZoom(zoom);updateHighlight();
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
function fit(){if(!nodes.length)return;const view=$('viewport');applyZoom(Math.min((availableWidth()-24)/graphWidth,(view.clientHeight-24)/graphHeight,1));view.scrollTo(0,0);}
function initialView(){if(!nodes.length)return;if(selected?.type==='group'){const group=groupById.get(text(selected.id));if(group){frameGroup(group);return;}}const fitted=Math.min((availableWidth()-24)/graphWidth,($('viewport').clientHeight-24)/graphHeight,1);applyZoom(Math.max(.8,fitted));if(fitted<.8)centerNode(byId.get(text(array(scenario.entry_operation_ids)[0]))||nodes.find(n=>legacy&&n.kind==='start')||nodes[0]);else $('viewport').scrollTo(0,0);}
function zoomBy(factor){const view=$('viewport');applyZoom(zoom*factor,{x:availableWidth()/2,y:view.clientHeight/2});}
$('zoom-in').addEventListener('click',()=>zoomBy(1.2));$('zoom-out').addEventListener('click',()=>zoomBy(1/1.2));$('fit').addEventListener('click',fit);
$('reset').addEventListener('click',()=>{closeInspector(false);showOverview();updateHighlight();applyZoom(1);if(nodes.length)centerNode(byId.get(text(array(scenario.entry_operation_ids)[0]))||nodes.find(n=>legacy&&n.kind==='start')||nodes[0]);announce('Выбор сброшен. Масштаб 100 процентов.');});
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
let resizeFrame=0;window.addEventListener('resize',()=>{cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>{draw();initialView();});});
showOverview();draw();initialView();updateHighlight();
if(document.fonts?.ready)document.fonts.ready.then(()=>{draw();initialView();});
})();
'''.replace('__GRAPH_LAYOUT_JS__', _GRAPH_LAYOUT_JS)
