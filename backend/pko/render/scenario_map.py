"""Standalone, offline map of one scenario across automated systems.

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
        "STYLE": _STYLE, "SCRIPT": _SCRIPT, "MODEL": _script_json(model),
        "DEMO_HIDDEN": "" if run.get("demo") is True else " hidden",
        "DETAIL_HIDDEN": " hidden" if model.get("schema_version") == "1.1" and isinstance(model.get("execution_reviews"), list) else "",
        "RUN_NOTE": html.escape(str(run.get("reason") or ""), quote=True),
        "RUN_NOTE_HIDDEN": "" if run.get("reason") and run.get("status") in ("partial", "failed") else " hidden",
    }
    # A single pass also keeps placeholder-like words in user text literal.
    return re.sub(r"__(TITLE|FONTS|STYLE|SCRIPT|MODEL|DEMO_HIDDEN|DETAIL_HIDDEN|RUN_NOTE|RUN_NOTE_HIDDEN)__",
                  lambda match: values[match.group(1)], _DOCUMENT)


_DOCUMENT = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light"><title>__TITLE__ · Белка</title>
__FONTS__<style>__STYLE__</style></head>
<body><a class="skip" href="#workspace">Перейти к карте</a>
<header class="page-header">
<div class="heading-line"><h1 id="page-title">__TITLE__</h1><div class="heading-badges"><span id="demo-badge" class="badge demo"__DEMO_HIDDEN__>Учебный пример</span><span id="detail-badge" class="badge unknown"__DETAIL_HIDDEN__>Детализация не проверена</span><span id="run-status" class="badge unknown"></span></div></div>
<p id="scenario-description" class="description"></p>
<div class="overview" id="overview"></div>
<details class="scope"><summary>Границы сценария и материалы исследования</summary><div id="scope-content" class="scope-content"></div></details>
<p id="run-note" class="run-note"__RUN_NOTE_HIDDEN__>__RUN_NOTE__</p></header>
<main id="workspace" tabindex="-1">
<div class="workspace-bar"><div class="tabs" role="tablist" aria-label="Представление результата">
<button id="tab-map" role="tab" aria-selected="true" aria-controls="panel-map" type="button">Карта</button>
<button id="tab-registry" role="tab" aria-selected="false" aria-controls="panel-registry" tabindex="-1" type="button">Реестр шагов</button>
<button id="tab-gaps" role="tab" aria-selected="false" aria-controls="panel-gaps" tabindex="-1" type="button">Пробелы исследования</button>
</div><label class="search-label" for="search"><span>Найти шаг</span><input id="search" type="search" placeholder="Название, описание или АС" autocomplete="off" aria-describedby="search-help" title="Enter — перейти к следующему найденному шагу"><span id="search-help" class="sr-only">Enter — перейти к следующему найденному шагу</span></label></div>
<div class="workspace-layout"><div class="main-panels">
<section id="panel-map" role="tabpanel" aria-labelledby="tab-map">
<div class="map-toolbar"><div class="map-heading"><strong>Сквозной сценарий</strong><span id="search-result" role="status" aria-live="polite">Подробная карта выбранного сценария</span></div>
<div class="map-actions"><button type="button" id="zoom-out" aria-label="Уменьшить масштаб">−</button><output id="zoom-value" aria-label="Масштаб">100%</output><button type="button" id="zoom-in" aria-label="Увеличить масштаб">+</button><button type="button" id="fit">Вписать</button><button type="button" id="reset">Сбросить</button></div></div>
<div id="viewport" class="viewport" tabindex="0" aria-label="Карта сценария. Используйте прокрутку для перемещения, кнопки для масштаба; Tab для выбора шагов."><div id="canvas-size"><div id="stage"><svg id="connections" xmlns="http://www.w3.org/2000/svg" aria-label="Переходы между шагами"></svg><div id="nodes"></div></div></div><div id="empty-map" class="empty" hidden>Карта пока не содержит шагов. Причина и незавершённые участки указаны в результатах исследования.</div></div>
<div class="map-footer"><div class="legend" aria-label="Основания карты"><span class="badge observed">Подтверждено кодом</span><span class="badge inferred">Вывод по коду</span><span class="badge unknown">Не установлено</span></div><span class="map-hint">Выберите шаг или переход, чтобы открыть описание</span></div>
</section>
<section id="panel-registry" role="tabpanel" aria-labelledby="tab-registry" hidden><div class="table-wrap"><table><caption>Шаги выбранного сценария</caption><thead><tr><th scope="col">Шаг</th><th scope="col">Система</th><th scope="col">Назначение</th><th scope="col">Основание</th></tr></thead><tbody id="registry"></tbody></table></div><p id="registry-empty" class="empty" hidden>По запросу ничего не найдено.</p></section>
<section id="panel-gaps" role="tabpanel" aria-labelledby="tab-gaps" hidden><div class="gaps-intro"><h2>Что осталось установить</h2><p>Пробелы показывают границы исследования. Отсутствие найденного подтверждения не доказывает отсутствие реализации.</p></div><div id="gaps"></div></section>
</div><aside id="inspector" class="inspector" aria-label="Описание выбранного элемента" hidden><div class="inspector-head"><span>Описание элемента</span><button type="button" id="close-inspector" aria-label="Закрыть описание">×</button></div><div id="inspector-content"></div></aside></div>
<p class="footnote">Карта восстановлена по переданным материалам. Подтверждение в коде не гарантирует поведение работающих систем и полноту бизнес-процесса.</p>
</main><div id="announcement" class="sr-only" role="status" aria-live="polite"></div>
<noscript><p class="empty">Для просмотра интерактивной карты включите JavaScript. Все данные находятся внутри этого HTML-файла; подключение к сети не требуется.</p></noscript>
<script id="scenario-model" type="application/json">__MODEL__</script><script>__SCRIPT__</script></body></html>'''


_STYLE = r'''
:root{--ink:#263844;--muted:#5a6d7c;--border:#d0d7dd;--mint:#f1faf7;--purple:#7044b4;--pale-purple:#f5edfc;--white:#fff;--focus:#7044b4;--shadow:0 3px 12px rgba(45,58,65,.045)}
*{box-sizing:border-box}body{margin:0;background:var(--white);color:var(--ink);font-family:'SB Sans Display',Arial,sans-serif;font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}button,input{font:inherit}button{cursor:pointer}button:disabled{cursor:default;opacity:.45}button,input{min-height:44px}button:focus-visible,input:focus-visible,summary:focus-visible,a:focus-visible,[tabindex]:focus-visible{outline:3px solid var(--focus);outline-offset:3px}button{transition:border-color .15s,background .15s,box-shadow .15s}button:hover{border-color:var(--purple)}[hidden]{display:none!important}h1,h2,h3,p{margin-top:0}h1,h2,h3{font-weight:600;line-height:1.25}h1{font-size:clamp(25px,2.5vw,38px);margin-bottom:0;letter-spacing:-.025em;overflow-wrap:anywhere}h2{font-size:20px}h3{font-size:17px}.page-header,main{max-width:1900px;margin:0 auto;padding:0 32px}.page-header{padding-top:32px;padding-bottom:22px}.eyebrow{color:var(--muted);font-size:12px;font-weight:700;letter-spacing:.12em;margin-bottom:14px}.eyebrow span{color:#9ba9b0;padding:0 9px}.heading-line{display:flex;align-items:center;justify-content:space-between;gap:24px}.heading-line>.badge{flex-shrink:0}.description{margin:13px 0 16px;max-width:1100px;color:var(--muted);font-size:17px;white-space:pre-wrap;overflow-wrap:anywhere}.overview{display:flex;flex-wrap:wrap;gap:10px 26px;color:var(--muted);margin-bottom:14px}.overview b{color:var(--ink);font-size:18px;font-weight:600;margin-right:5px}.scope{max-width:1100px}.scope summary{cursor:pointer;color:var(--purple);width:fit-content;min-height:30px;font-size:14px;padding:2px 0}.scope-content{margin-top:12px;border:1px solid var(--border);border-radius:14px;padding:18px 20px;background:#fcfdfd}.scope-content .field{margin-bottom:12px}.scope-content .field:last-child{margin-bottom:0}.scope-content ul{margin:6px 0;padding-left:20px}.run-note{margin:15px 0 0;padding:10px 14px;border-radius:10px;background:#fff6e5;color:#7a5014;white-space:pre-wrap}.workspace-bar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:16px}.tabs{display:flex;gap:5px;flex-wrap:wrap;padding:5px;background:#f3f5f5;border-radius:12px}.tabs button{border:1px solid transparent;border-radius:9px;padding:8px 15px;background:transparent;color:var(--muted);font-weight:600}.tabs button[aria-selected=true]{background:#fff;color:var(--purple);box-shadow:0 2px 5px #162e3a0b}.search-label{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:13px}.search-label input{border:1px solid var(--border);border-radius:10px;padding:10px 12px;width:260px;background:#fff;color:var(--ink)}.workspace-layout{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:18px;align-items:start}.main-panels{min-width:0;border:1px solid var(--border);border-radius:16px;overflow:hidden;background:#fff}.map-toolbar{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;gap:12px;border-bottom:1px solid var(--border);flex-wrap:wrap}.map-heading{display:flex;flex-direction:column;gap:2px}.map-heading strong{font-size:16px;font-weight:600}.map-heading span{font-size:12px;color:var(--muted)}.map-actions{display:flex;align-items:center;gap:6px}.map-actions button{border:1px solid var(--border);background:#fff;border-radius:8px;padding:7px 12px;font-size:13px;min-width:40px}.map-actions #zoom-in,.map-actions #zoom-out{font-size:20px;padding:3px 11px}.map-actions output{font-size:12px;min-width:42px;text-align:center;color:var(--muted)}.viewport{position:relative;height:clamp(470px,64vh,850px);overflow:auto;background:var(--mint);background-image:radial-gradient(#cbded6 .7px,transparent .7px);background-size:20px 20px;overscroll-behavior:contain;scrollbar-color:#b5c9bf transparent}.viewport.dragging{cursor:grabbing;user-select:none}.viewport:focus-visible{outline-offset:-4px}#canvas-size{position:relative;min-width:100%;min-height:100%}#stage{position:absolute;top:0;left:0;transform-origin:top left}#connections,#lanes,#nodes{position:absolute;top:0;left:0;width:100%;height:100%}#connections{overflow:visible;z-index:2;pointer-events:none}#lanes{z-index:1;pointer-events:none}#nodes{z-index:3;pointer-events:none}.lane{position:absolute;left:18px;right:18px;border:1px solid #d2e2db;border-radius:14px;background:#ffffff60}.lane:nth-child(even){background:#edf7f2b3}.lane-title{position:absolute;left:18px;top:22px;width:158px;padding-right:10px;color:#3d6053;overflow-wrap:anywhere;font-size:15px;font-weight:600;line-height:1.35}.lane-caption{display:block;color:#60796e;font-size:12px;font-weight:400;margin-top:6px}.node{position:absolute;display:flex;flex-direction:column;gap:9px;pointer-events:auto;width:236px;padding:14px 15px;border:1px solid #b9c8c0;border-radius:13px;background:#fff;box-shadow:var(--shadow);text-align:left;color:var(--ink);line-height:1.4;overflow-wrap:anywhere}.node:hover{box-shadow:0 5px 20px #53337618}.node.selected{border:2px solid var(--purple);padding:13px 14px;box-shadow:0 0 0 3px #d1b1eb4a}.node.dimmed{opacity:.32}.node.search-match{border-color:var(--purple);box-shadow:0 0 0 3px #e2cff5}.node-type{font-size:11px;font-weight:600;letter-spacing:.02em;color:#6a4a90;display:flex;align-items:center;gap:6px}.node-type .symbol{display:inline-flex;align-items:center;justify-content:center;width:21px;height:21px;flex-shrink:0;border:1px solid #d8c6e8;border-radius:6px;background:var(--pale-purple);font-size:13px}.node-name{font-size:15px;font-weight:600}.node-status{font-size:10px;color:var(--muted);display:flex;align-items:center;gap:5px}.node-status:before{content:'';display:block;flex-shrink:0;width:6px;height:6px;border-radius:50%;background:#687880}.node-status.observed:before{background:#137668}.node-status.inferred:before{background:#956211}.node.kind-start,.node.kind-end{border-radius:23px}.node.kind-external{border-style:dashed}.node.kind-decision,.node.kind-fork,.node.kind-join{border-top:3px solid #af86cc}.node.kind-wait{border-top:3px solid #87aba0}.edge-visible{fill:none;stroke:#78958a;stroke-width:1.8}.edge-observed .edge-visible{stroke:#638d7c}.edge-inferred .edge-visible{stroke:#a18450;stroke-dasharray:7 4}.edge-unknown .edge-visible{stroke:#87969a;stroke-dasharray:3 5}.edge-hit{fill:none;stroke:transparent;stroke-width:17;pointer-events:stroke;cursor:pointer}.edge.selected .edge-visible{stroke:var(--purple);stroke-width:3}.edge.dimmed{opacity:.18}.edge:focus-visible{outline:none}.edge:focus-visible .edge-visible{stroke:var(--purple);stroke-width:4}.edge-label{pointer-events:none;fill:#466052;font-size:11px;font-family:inherit;paint-order:stroke;stroke:var(--mint);stroke-width:5px;stroke-linejoin:round}.map-footer{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:13px 16px;flex-wrap:wrap;border-top:1px solid var(--border)}.legend{display:flex;flex-wrap:wrap;gap:7px}.badge{display:inline-block;padding:4px 8px;border-radius:6px;font-size:11px;font-weight:600;line-height:1.4;white-space:normal}.badge.observed{color:#155f53;background:#e3f3ed}.badge.inferred{color:#805619;background:#fff1d8}.badge.unknown{color:#53636e;background:#eef1f3}.badge.run-completed{color:#155f53;background:#e3f3ed}.badge.run-partial,.badge.run-running{color:#805619;background:#fff1d8}.badge.run-failed{color:#9b3148;background:#fbe9ee}.map-hint{font-size:11px;color:var(--muted)}.inspector{border:1px solid var(--border);border-radius:16px;background:#fff;min-height:400px;max-height:calc(64vh + 140px);overflow:auto;position:sticky;top:16px;box-shadow:var(--shadow)}#inspector-content{padding:22px 20px}.inspector-kicker{font-size:12px;color:var(--purple);font-weight:600;margin-bottom:12px}.inspector h2{margin:13px 0 16px;overflow-wrap:anywhere}.inspector .muted{color:var(--muted);font-size:14px}.field{margin:18px 0;overflow-wrap:anywhere}.field dt{font-size:12px;color:var(--muted);margin-bottom:5px}.field dd{margin:0;color:var(--ink);font-size:14px;white-space:pre-wrap}.field-list{margin:0}.details{border-top:1px solid #e3e8eb;padding-top:14px;margin-top:20px}.details summary{cursor:pointer;font-size:13px;color:var(--purple);padding:3px 0;min-height:36px}.evidence{padding:10px 12px;background:#f5f7f7;border-radius:8px;margin:10px 0;font-size:12px;overflow-wrap:anywhere}.evidence cite{font-style:normal;color:var(--muted);display:block;margin-bottom:6px}.evidence pre{white-space:pre-wrap;word-break:break-word;font-family:Consolas,monospace;font-size:11px;line-height:1.5;margin:6px 0}.related{display:flex;flex-direction:column;gap:7px}.text-button{width:100%;text-align:left;border:1px solid var(--border);border-radius:9px;background:#fff;padding:9px 11px;color:var(--purple);font-size:13px;white-space:normal;overflow-wrap:anywhere}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px}caption{text-align:left;padding:20px;font-size:18px;font-weight:600}th{text-align:left;color:var(--muted);font-size:12px;background:#f8faf9;font-weight:600}td,th{padding:14px 18px;border-bottom:1px solid #e5e9e7;vertical-align:top}td{max-width:350px;white-space:pre-wrap;overflow-wrap:anywhere}td:first-child{min-width:190px;width:25%}td:nth-child(2){min-width:145px}td .text-button{border:0;background:transparent;padding:0;min-height:36px;color:var(--purple);font-weight:600}td .type-label{display:block;color:var(--muted);font-size:11px;margin-top:5px}.gaps-intro{padding:24px 24px 0}.gaps-intro p{color:var(--muted);font-size:14px;max-width:700px}#gaps{padding:0 24px 24px}.gap{border:1px solid var(--border);border-left:3px solid #caa267;border-radius:10px;padding:16px;margin-top:14px}.gap p{white-space:pre-wrap;overflow-wrap:anywhere;margin:0 0 10px}.gap .related{flex-direction:row;flex-wrap:wrap}.gap .text-button{width:auto}.empty{padding:40px 28px;color:var(--muted);max-width:700px;margin:0}.footnote{font-size:12px;color:var(--muted);padding:14px 0 26px;margin:0;max-width:1150px}.sr-only{position:absolute;width:1px;height:1px;padding:0;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.skip{position:absolute;top:-100px;left:20px;z-index:50;background:#fff;padding:12px;color:var(--purple)}.skip:focus{top:10px}
@media(max-width:1200px){.workspace-layout{grid-template-columns:minmax(0,1fr) 300px}.page-header,main{padding-left:22px;padding-right:22px}.search-label>span{display:none}.search-label input{width:225px}.tabs button{padding-left:10px;padding-right:10px;font-size:13px}.map-hint{display:none}}
.heading-badges{display:flex;gap:7px;align-items:center;flex-shrink:0;flex-wrap:wrap}.badge.demo{color:var(--purple);background:var(--pale-purple)}.scope .diagnostic-title{color:#805619;font-weight:600}.inspector-back{display:none;margin-bottom:16px}.gap-category{font-size:12px;color:#805619;font-weight:600;margin-bottom:8px}.checks-notice{border:0;border-bottom:1px solid var(--border);display:block;width:100%;text-align:left;padding:9px 18px;background:#fff8eb;color:#805619;font-size:12px;min-height:38px}.checks-notice:hover{background:#fff1d8}
@media(max-width:900px){.workspace-layout{grid-template-columns:1fr}.inspector{position:static;min-height:0;max-height:none}.workspace-bar{align-items:center;flex-direction:row}.search-label input{width:100%}.search-label{width:225px;flex-shrink:0}.heading-line{align-items:center;flex-direction:row;flex-wrap:wrap;gap:8px 20px}.heading-line h1{flex:1 1 380px}.page-header{padding-top:18px;padding-bottom:14px}.eyebrow{margin-bottom:8px}.description{margin:8px 0 10px;font-size:15px}.overview{margin-bottom:8px}.scope summary{min-height:26px}.heading-badges{gap:6px}.viewport{height:560px}.map-toolbar{padding:12px}.map-actions{margin-left:auto}.tabs{width:fit-content}.map-hint{display:block}.inspector-back{display:block}}
@media(max-width:700px){.workspace-bar{align-items:stretch;flex-direction:column}.search-label{width:100%}.heading-line h1{flex-basis:100%}}
@media(max-width:560px){.page-header,main{padding-left:14px;padding-right:14px}.tabs{width:100%;display:grid;grid-template-columns:1fr 1.4fr 1.8fr;gap:0}.tabs button{font-size:12px;padding:7px 5px;line-height:1.3}.description{font-size:15px}.viewport{height:480px}.map-actions{width:100%;justify-content:flex-end}.map-actions button{padding:6px 10px}.map-footer{padding:12px}.map-hint{display:none}.badge{font-size:10px}.inspector #inspector-content{padding:20px}.overview{gap:7px 16px;font-size:13px}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important;animation:none!important}}
/* Visual tokens and density follow the supplied DPSS reference. The content
   remains a scenario graph; no passport hierarchy or readiness data is used. */
:root{--ink:#000;--muted:#5A6D7C;--border:#D0D7DD;--mint:#F6FAF8;--purple:#8B3FD9;--accent:#C17DF7;--pale-purple:#F3E5FD;--focus:#8B3FD9;--shadow:none}
html,body{height:100%;overflow:hidden}body{height:100dvh;display:flex;flex-direction:column;font-size:14px}.page-header,main{max-width:none;width:100%;margin:0}.page-header{flex:none;padding:12px 24px 10px;border-bottom:1px solid var(--border)}.eyebrow{font-size:12px;letter-spacing:.025em;margin-bottom:4px}.eyebrow span{padding:0 6px}.heading-line{gap:8px 20px}.heading-line h1{font-size:22px;font-weight:700;line-height:1.2;letter-spacing:-.025em}.description{font-size:13px;margin:7px 0;line-height:1.45;max-width:1100px}.overview{gap:8px 20px;font-size:12px;margin-bottom:5px}.overview b{font-size:18px;margin-right:5px}.scope{position:relative;max-width:none}.scope summary{font-size:12px;min-height:24px;padding:2px 0}.scope-content{position:absolute;top:100%;left:0;width:min(920px,calc(100vw - 48px));max-height:65dvh;overflow:auto;z-index:30;margin-top:6px;background:#fff;border-radius:12px;padding:18px 24px;box-shadow:0 10px 30px #26384422}.scope-content .field dt{font-size:13px;font-weight:600;color:var(--ink)}.run-note{margin:6px 0 0;padding:6px 10px;font-size:12px;border-radius:8px;max-height:80px;overflow:auto}main{display:flex;flex-direction:column;flex:1;min-height:0;padding:0}.workspace-bar{flex:none;padding:8px 24px;margin:0;gap:16px;border-bottom:1px solid var(--border)}.tabs{padding:3px;border-radius:8px;background:#E9ECEF}.tabs button{border-radius:8px;font-size:13px;padding:6px 12px;min-height:38px;font-weight:400}.tabs button[aria-selected=true]{font-weight:600;box-shadow:none}.search-label input{min-height:38px;border-radius:8px;font-size:13px;padding:7px 10px}.workspace-layout{display:block;position:relative;flex:1;min-height:0;gap:0}.main-panels{height:100%;border:0;border-radius:0;overflow:hidden}.main-panels>section{height:100%;overflow:auto}#panel-map{position:relative;display:flex;flex-direction:column;overflow:hidden}.map-toolbar{display:contents}.map-heading{display:none}.map-actions{position:absolute;left:16px;bottom:64px;z-index:4;background:#fff;border:1px solid var(--border);padding:4px;border-radius:15px;gap:0;box-shadow:0 2px 8px #1c2b400a;width:auto;margin:0}.map-actions button{border:0;border-radius:8px;min-width:40px;min-height:42px;padding:7px 11px;background:transparent}.map-actions button:hover{background:var(--pale-purple)}.map-actions output{min-width:52px;font-size:13px;font-weight:600;color:#183454}.map-actions #fit{border-left:1px solid var(--border);border-radius:0 8px 8px 0}.viewport{height:auto;flex:1;min-height:0;background:var(--mint);background-image:none;scrollbar-color:#b7c2cb transparent}.map-footer{flex:none;padding:9px 24px;border-top:1px solid var(--border);gap:8px}.map-hint{font-size:11px}.legend{gap:7px}.badge{border-radius:8px;font-size:11px;font-weight:400;padding:4px 8px;color:var(--ink)}.badge.observed,.badge.run-completed{background:#CFF5F3;color:var(--ink)}.badge.inferred,.badge.run-partial,.badge.run-running{background:#FFF2CC;color:var(--ink)}.badge.unknown{background:#E9ECEF;color:var(--muted)}.badge.demo{background:#F3E5FD;color:var(--purple)}.badge.run-failed{background:#FBD6E1;color:var(--ink)}.lane{background:#ffffff28;border-color:#dfe7e2;border-radius:12px}.lane:nth-child(even){background:#ffffff52}.lane-title{color:var(--muted);font-size:13px;font-weight:400}.lane-caption{color:#92A2B0;font-size:12px}.node{width:224px;min-height:88px;padding:12px 16px;gap:7px;border-color:#B7C2CB;border-radius:12px;box-shadow:none;color:var(--ink)}.node:hover{border-color:var(--accent);box-shadow:0 8px 20px -12px #c17df799}.node.selected{padding:12px 16px;border:1px solid var(--accent);background:rgba(193,125,247,.2);box-shadow:none}.node.dimmed{opacity:.2}.node.dimmed:hover{opacity:.8}.node.search-match{border-color:var(--accent);box-shadow:0 0 0 2px #c17df72b}.node-type{color:var(--muted);font-size:11px;font-weight:400;letter-spacing:0}.node-type .symbol{display:none}.node-name{font-size:15px;line-height:1.35}.node-status{font-size:11px}.node.kind-start,.node.kind-end{border-radius:20px}.node.kind-decision,.node.kind-fork,.node.kind-join,.node.kind-wait{border-top-width:1px;border-color:#B7C2CB}.edge-visible,.edge-observed .edge-visible{stroke:#B7C2CB;stroke-width:1.4}.edge-inferred .edge-visible{stroke:#9b8b73}.edge-unknown .edge-visible{stroke:#92A2B0}.edge.selected .edge-visible{stroke:var(--accent);stroke-width:2}.edge-label{fill:var(--muted)}.inspector{position:absolute;right:0;top:0;bottom:0;width:480px;max-width:100%;min-height:0;max-height:none;border:0;border-left:1px solid #B7C2CB;border-radius:0;display:flex;flex-direction:column;overflow:hidden;z-index:6;background:#fff;box-shadow:none}.inspector-head{flex:none;display:flex;align-items:center;justify-content:space-between;padding:4px 18px;border-bottom:1px solid var(--border);font-size:12px;color:var(--muted)}.inspector-head button{border:0;background:#fff;min-width:40px;min-height:40px;font-size:24px;color:var(--muted);padding:0}#inspector-content{flex:1;min-height:0;overflow:auto;padding:18px 24px 28px}.inspector h2{font-size:20px;font-weight:700;line-height:1.2;margin:7px 0 16px}.inspector-kicker{font-size:12px;color:var(--muted);font-weight:400;margin-bottom:7px}.field{margin:0 0 16px}.field dt{font-size:13px;color:var(--ink);font-weight:600;margin-bottom:6px}.field dd{font-size:13px;line-height:1.48}.inspector .muted{font-size:13px}.inspector-back{display:none!important}.details{margin-top:16px;padding-top:12px}.details summary{font-size:12px}.text-button{border-radius:8px;font-size:13px}.footnote{display:none}.checks-notice{flex:none;padding:6px 24px;min-height:32px;font-size:12px}
@media(max-width:900px){.workspace-bar{flex-direction:row;align-items:center}.search-label{width:205px;flex-shrink:0}.heading-line h1{flex:1 1 380px}.tabs{width:fit-content}.map-hint{display:none}.inspector{width:420px}.map-actions{width:auto;justify-content:flex-start}}
@media(max-width:700px){.page-header{padding:10px 16px 7px}.heading-line h1{font-size:18px;flex-basis:100%}.description{font-size:12px;margin:5px 0}.heading-badges{gap:5px}.overview{gap:5px 14px;margin-bottom:3px}.overview b{font-size:16px}.scope-content{width:calc(100vw - 32px);max-height:60dvh;padding:16px 20px}.workspace-bar{padding:6px 12px;gap:8px;flex-wrap:wrap}.tabs{width:auto;display:flex;flex:1;gap:1px}.tabs button{font-size:11px;padding:5px 8px;min-height:36px}.search-label{width:150px}.search-label input{font-size:12px;min-height:36px}.inspector{width:100%}.map-footer{padding:7px 12px}.badge{font-size:10px}.legend{gap:4px}.map-actions{left:12px;bottom:60px}.map-actions button{padding:6px 9px}.checks-notice{padding:6px 16px}.inspector #inspector-content{padding:16px 20px 24px}}
.description{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
.node-system{font-size:11px;line-height:1.35;color:var(--muted)}
@media(max-width:480px){.workspace-bar{flex-wrap:nowrap}.search-label{width:36px;position:relative;overflow:visible}.search-label:before{content:'';position:absolute;width:10px;height:10px;border:1.5px solid var(--muted);border-radius:50%;left:10px;top:10px;z-index:1;pointer-events:none}.search-label:after{content:'';position:absolute;width:6px;border-top:1.5px solid var(--muted);transform:rotate(45deg);left:19px;top:22px;z-index:1;pointer-events:none}.search-label:focus-within:before,.search-label:focus-within:after{display:none}.search-label input{width:36px;padding:6px;transition:none}.search-label input:focus{position:absolute;right:0;top:-18px;width:min(300px,calc(100vw - 32px));z-index:8;background:#fff}.search-label input::placeholder{color:transparent}.search-label input:focus::placeholder{color:var(--muted)}.tabs button{padding:5px 6px;font-size:10px}.map-footer{padding-left:10px;padding-right:10px}.legend .badge{font-size:9px;padding:4px 6px}}
@media print{.page-header,main{padding:0}.workspace-bar,.map-actions,.map-hint,.skip{display:none}.workspace-layout{display:block}.inspector{position:static;max-height:none;margin-top:18px}.viewport{height:650px}.main-panels{break-inside:avoid}.scope:not([open]){display:none}.footnote{padding-bottom:0}}
'''


_GRAPH_LAYOUT_JS = r'''
function layoutScenarioGraph(nodes, edges, sizes, vertical=false) {
 const key=value=>String(value),adj=new Map(nodes.map(n=>[key(n.id),[]]));
 const valid=edges.filter(e=>adj.has(key(e.source))&&adj.has(key(e.target)));
 for(const e of valid)if(e.kind!=='data'&&e.kind!=='retry')adj.get(key(e.source)).push(key(e.target));
 let counter=0;const indices=new Map(),low=new Map(),stack=[],inStack=new Set(),components=[];
 function visit(id){indices.set(id,counter);low.set(id,counter++);stack.push(id);inStack.add(id);for(const to of adj.get(id)){if(!indices.has(to)){visit(to);low.set(id,Math.min(low.get(id),low.get(to)));}else if(inStack.has(to))low.set(id,Math.min(low.get(id),indices.get(to)));}if(low.get(id)===indices.get(id)){const group=[];let member;do{member=stack.pop();inStack.delete(member);group.push(member);}while(member!==id);components.push(group.reverse());}}
 for(const node of [...nodes.filter(n=>n.kind==='start'),...nodes])if(!indices.has(key(node.id)))visit(key(node.id));
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
function routeScenarioOuterEdge(edge,layout,vertical,index=0) {
 const a=layout.positions.get(String(edge.source)),b=layout.positions.get(String(edge.target));
 const sourceRank=layout.rank.get(String(edge.source)),targetRank=layout.rank.get(String(edge.target));
 // Leave the occupied rank through its free gutter before entering the outer
 // channel. A single Bezier from a lower branch's top crosses its upper sibling.
 const trailing=rank=>Math.max(...[...layout.positions].filter(([id])=>layout.rank.get(id)===rank).map(([,p])=>vertical?p.y+p.h:p.x+p.w));
 let points,label;
 if(vertical){const channel=layout.width-28-index*24,from=trailing(sourceRank)+24,to=b.y-24;points=[{x:a.x+a.w/2,y:a.y+a.h},{x:a.x+a.w/2,y:from},{x:channel,y:from},{x:channel,y:to},{x:b.x+b.w/2,y:to},{x:b.x+b.w/2,y:b.y-3}];label={x:channel-9,y:(from+to)/2};}
 else{const channel=28+index*24,from=trailing(sourceRank)+28,to=b.x-28;points=[{x:a.x+a.w,y:a.y+a.h/2},{x:from,y:a.y+a.h/2},{x:from,y:channel},{x:to,y:channel},{x:to,y:b.y+b.h/2},{x:b.x-3,y:b.y+b.h/2}];label={x:(from+to)/2,y:channel-9};}
 const segments=[];let current=points[0];
 const lineTo=target=>{segments.push([current,current,target,target]);current=target;};
 for(let i=1;i<points.length-1;i++){const before=points[i-1],corner=points[i],after=points[i+1],first=Math.hypot(corner.x-before.x,corner.y-before.y),second=Math.hypot(after.x-corner.x,after.y-corner.y),radius=Math.min(18,first/2,second/2);if(!first||!second)continue;const entry={x:corner.x+(before.x-corner.x)*radius/first,y:corner.y+(before.y-corner.y)*radius/first},exit={x:corner.x+(after.x-corner.x)*radius/second,y:corner.y+(after.y-corner.y)*radius/second};lineTo(entry);segments.push([entry,corner,corner,exit]);current=exit;}
 lineTo(points[points.length-1]);
 return {segments,label,path:`M ${points[0].x} ${points[0].y} `+segments.map(([,c1,c2,end])=>`C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${end.x} ${end.y}`).join(' ')};
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
const nodes = array(model.nodes).filter(n => n && typeof n==='object');
const edges = array(model.edges).filter(e => e && typeof e==='object');
const systems = array(model.systems).filter(s => s && typeof s==='object');
const gaps = array(model.gaps);
const omissions = array(model.source_omissions);
const validationIssues = array(model.validation?.issues);
const openFindings = gaps.length + omissions.length + validationIssues.length;
const executionReviews=array(model.execution_reviews),operationsByNode=new Map();
for(const review of executionReviews)for(const operation of array(review.operations)){const id=text(operation.node_id);if(!operationsByNode.has(id))operationsByNode.set(id,[]);operationsByNode.get(id).push({operation,review});}
const byId = new Map(nodes.map(n => [text(n.id),n]));
const systemById = new Map(systems.map(s => [text(s.id),s]));
const kindNames={start:'Начало',action:'Действие',decision:'Условие',fork:'Параллельное выполнение',join:'Объединение',wait:'Ожидание',end:'Завершение',external:'Внешний участок'};
const operationKinds={action:'Операция',validation:'Проверка',read:'Чтение данных',write:'Запись данных',integration:'Вызов системы',publish:'Публикация события',wait:'Ожидание',calculation:'Расчёт',transform:'Преобразование данных',error_handling:'Обработка ошибки'};
const nodeKindLabel=node=>node.kind==='action'?(operationKinds[operationsByNode.get(text(node.id))?.[0]?.operation.kind]||kindNames.action):(kindNames[node.kind]||'Шаг сценария');
const symbols={start:'○',action:'→',decision:'◇',fork:'⑂',join:'⑃',wait:'◷',end:'●',external:'↗'};
const edgeNames={sequence:'Далее',branch:'Ветвление',call:'Вызов',event:'Сообщение',return:'Ответ',retry:'Повтор',error:'Ошибка',data:'Передача данных'};
const evidenceNames={observed:'Подтверждено кодом',inferred:'Вывод по коду',unknown:'Не установлено'};
const evidenceStatus = value => Object.prototype.hasOwnProperty.call(evidenceNames,value) ? value : 'unknown';
const stateNames={running:'Исследование выполняется',completed:'Исследование завершено',partial:'Исследование не завершено',failed:'Исследование прервано'};
const run=model.run || {}; const runState=Object.prototype.hasOwnProperty.call(stateNames,run.status)?run.status:'unknown';
const badge = value => el('span','badge '+evidenceStatus(value),evidenceNames[evidenceStatus(value)]);
const systemName = node => (systemById.get(text(node.system_id)) || {}).name || 'АС не установлена';
const validEdges=edges.filter(e => byId.has(text(e.source)) && byId.has(text(e.target)));
const positions=new Map(), nodeButtons=new Map(), edgeGroups=new Map(), registryRows=new Map();
let selected=null, query='', zoom=1, graphWidth=1000, graphHeight=600, activeTab='map', restoreFocus=null, layoutDirty=false, matchIndex=-1;

$('page-title').textContent=scenario.title || 'Карта сценария';
const fullDescription=text(scenario.description).trim();
$('scenario-description').textContent=fullDescription?(fullDescription.length>260?fullDescription.slice(0,257)+'…':fullDescription):'Описание выбранного варианта сценария не задано.';
$('run-status').textContent=stateNames[runState] || 'Статус исследования не установлен';
$('run-status').className='badge '+(runState==='unknown'?'unknown':'run-'+runState);
$('demo-badge').hidden=run.demo!==true;
for(const [count,label] of [[systems.length,'АС'],[nodes.length,plural(nodes.length,['шаг','шага','шагов'])],[validEdges.length,plural(validEdges.length,['переход','перехода','переходов'])],[gaps.length,plural(gaps.length,['пробел','пробела','пробелов'])]]){
 const entry=el('span'); entry.append(el('b','',count),document.createTextNode(label)); $('overview').append(entry);
}
function field(container,label,value){ if(!text(value).trim())return; const dl=el('dl','field');dl.append(el('dt','',label),el('dd','',value));container.append(dl); }
function listField(container,label,values){ const items=array(values);if(!items.length)return;const section=el('div','field');section.append(el('div','muted',label));const ul=el('ul');for(const item of items)ul.append(el('li','',item));section.append(ul);container.append(section); }
field($('scope-content'),'Описание варианта сценария',fullDescription);field($('scope-content'),'Начало',scenario.start);field($('scope-content'),'Завершение',scenario.end);
if(Array.isArray(scenario.exclusions))listField($('scope-content'),'За пределами сценария',scenario.exclusions);else field($('scope-content'),'За пределами сценария',scenario.exclusions);
if(Array.isArray(scenario.assumptions))listField($('scope-content'),'Принятые допущения',scenario.assumptions);else field($('scope-content'),'Принятые допущения',scenario.assumptions);
for(const source of array(model.sources))field($('scope-content'),'Источник: '+(source.name || source.id),[source.kind,Number.isFinite(source.file_count)?source.file_count+' файлов':''].filter(Boolean).join(' · '));
if(!array(model.sources).length)field($('scope-content'),'Материалы','Источники не указаны.');
field($('scope-content'),'Результат исследования',run.reason);
listField($('scope-content'),'Ограничения анализа',array(model.limitations).length?model.limitations:run.limitations);
listField($('scope-content'),'Примечания к источникам',model.source_notes);
listField($('scope-content'),'Материалы, не вошедшие в исследование',omissions);
listField($('scope-content'),'Незакрытые проверки карты',validationIssues);
field($('scope-content'),'Границы достоверности','Карта восстановлена по переданным материалам. Подтверждение в коде не гарантирует поведение работающих систем и полноту бизнес-процесса.');
if(!$('detail-badge').hidden)field($('scope-content'),'Детализация карты','Эта версия результата не содержит проверенного реестра атомарных операций. Узлы показаны полностью, но детальность исследования не подтверждена.');
if(run.reason&&(runState==='partial'||runState==='failed')){$('run-note').textContent=text(run.reason);$('run-note').hidden=false;}
if(validationIssues.length||omissions.length){const notice=el('button','checks-notice');notice.type='button';notice.textContent=[omissions.length?'Пропуски материалов: '+omissions.length:'',validationIssues.length?'Незакрытые проверки: '+validationIssues.length:''].filter(Boolean).join(' · ')+' — посмотреть';notice.addEventListener('click',()=>activateTab('gaps'));$('panel-map').insertBefore(notice,$('viewport'));}

function evidenceBlock(container,items,label='Основания в исходниках'){
 const evidence=array(items); if(!evidence.length)return;
 const details=el('details','details'); details.append(el('summary','',label+' · '+evidence.length));
 for(const item of evidence){const box=el('div','evidence');if(typeof item==='string'){box.textContent=item;}else{
 const location=[item.source_id || item.source,item.path || item.file].filter(Boolean).join(' / ');
 const line=item.line || item.start_line || item.line_start;
 box.append(el('cite','',location+(line?':'+line:'')));
 if(item.quote || item.text || item.snippet)box.append(el('pre','',item.quote || item.text || item.snippet));
 if(item.reason || item.note || item.claim)box.append(el('div','',item.reason || item.note || item.claim));
 }details.append(box);}container.append(details);
}
function relatedButton(container,label,callback){const b=el('button','text-button',label);b.type='button';b.addEventListener('click',callback);container.append(b);return b;}
function showOverview(){
 const panel=$('inspector-content'); panel.replaceChildren();panel.append(el('div','inspector-kicker','ВАРИАНТ СЦЕНАРИЯ'),el('h2','',scenario.title || 'Карта процесса'));
 panel.append(el('p','muted',model.summary || 'Выберите шаг на карте, чтобы увидеть его бизнес-смысл, вход, результат и подтверждения в коде. Переходы открываются отдельно.'));
 field(panel,'Начало',scenario.start);field(panel,'Завершение',scenario.end);
 panel.append(el('p','muted','Граф показывает шаги и переходы выбранного сценария. АС указана у каждого шага. Пунктирные связи содержат вывод или остаются неподтверждёнными.'));
 if(openFindings)relatedButton(panel,'Посмотреть пробелы и проверки ('+openFindings+')',()=>activateTab('gaps'));
 const details=el('details','details');details.append(el('summary','','Что проверено'));
 details.append(el('p','muted','Основания показывают найденные участки исходников. Их наличие не доказывает правильность бизнес-интерпретации и полноту карты.'));
 for(const review of array(model.source_reviews)){const source=array(model.sources).find(s=>s.id===review.source_id);const disposition={reviewed:'Рассмотрен',out_of_scope:'Вне выбранного сценария',unavailable:'Недоступен'}[review.disposition];const inspection=review.disposition==='out_of_scope'&&review.inspected===false?'Исключение без изучения кода':Number.isFinite(review.read_files)?'Прочитано файлов: '+review.read_files:'';field(details,source?.name || review.source_id || 'Источник',[disposition,inspection,review.summary || review.reason || review.description].filter(Boolean).join('\n'));}
 if(Number.isFinite(run.steps))field(details,'Шагов исследования',run.steps);panel.append(details);
}
function showNode(node){
 const panel=$('inspector-content');panel.replaceChildren();panel.append(el('div','inspector-kicker',nodeKindLabel(node)),badge(node.evidence_status),el('h2','',node.name || node.id));
 field(panel,'Бизнес-смысл',node.description);field(panel,'Автоматизированная система',systemName(node));field(panel,'Вход',node.input);field(panel,'Результат',node.output);field(panel,'Условие выполнения',node.condition);
 for(const {operation,review} of operationsByNode.get(text(node.id))||[]){const section=el('div','details');section.append(el('h3','','Атомарная операция'));field(section,'Действие',operation.action);field(section,'Проверяемый результат',operation.effect);field(section,'При ошибке',operation.failure);field(section,'Условие операции',operation.condition);evidenceBlock(section,operation.evidence,'Основания операции');panel.append(section);}
 if(node.parent_id && byId.has(text(node.parent_id)))field(panel,'Часть шага',byId.get(text(node.parent_id)).name);
 const related=validEdges.filter(e=>text(e.source)===text(node.id) || text(e.target)===text(node.id));
 if(related.length){const area=el('div','details');area.append(el('h3','','Переходы'));const buttons=el('div','related');for(const edge of related){const outgoing=text(edge.source)===text(node.id);const peer=byId.get(text(outgoing?edge.target:edge.source));relatedButton(buttons,(outgoing?'→ ':'← ')+(peer.name || peer.id)+' · '+(edgeNames[edge.kind] || 'Связь'),()=>selectEdge(edge));}area.append(buttons);panel.append(area);}
 const nodeGaps=gaps.filter(g=>array(g.node_ids).map(text).includes(text(node.id)));for(const gap of nodeGaps)field(panel,'Не установлено',gap.description);
 evidenceBlock(panel,node.evidence);if(!array(node.evidence).length)field(panel,'Основания','Подтверждающие фрагменты для этого шага не представлены.');
}
function showEdge(edge){
 const panel=$('inspector-content');panel.replaceChildren();panel.append(el('div','inspector-kicker','ПЕРЕХОД · '+(edgeNames[edge.kind] || 'Связь')),badge(edge.evidence_status));
 const from=byId.get(text(edge.source)), to=byId.get(text(edge.target));panel.append(el('h2','',(from.name || from.id)+' → '+(to.name || to.id)));
 field(panel,'Что происходит',edge.description);field(panel,'Условие перехода',edge.condition);field(panel,'Передаваемые данные',edge.data);field(panel,'Связь экземпляра сценария',edge.correlation);
 const related=el('div','related');relatedButton(related,'Открыть: '+(from.name || from.id),()=>selectNode(from,true));relatedButton(related,'Открыть: '+(to.name || to.id),()=>selectNode(to,true));panel.append(related);
 const technical=el('details','details');technical.append(el('summary','','Способ передачи и контракт'));field(technical,'Канал',edge.channel);field(technical,'Контракт',edge.contract);panel.append(technical);
 evidenceBlock(panel,edge.sender_evidence,'Отправляющая сторона');evidenceBlock(panel,edge.receiver_evidence,'Принимающая сторона');evidenceBlock(panel,edge.evidence);
 if(!array(edge.evidence).length&&!array(edge.sender_evidence).length&&!array(edge.receiver_evidence).length)field(panel,'Основания','Подтверждающие фрагменты для перехода не представлены.');
}
function updateHighlight(){
 const neighborIds=new Set();if(selected?.type==='node'){neighborIds.add(text(selected.id));for(const e of validEdges)if(text(e.source)===text(selected.id)||text(e.target)===text(selected.id)){neighborIds.add(text(e.source));neighborIds.add(text(e.target));}}
 if(selected?.type==='edge'){const e=selected.edge;neighborIds.add(text(e.source));neighborIds.add(text(e.target));}
 for(const node of nodes){const button=nodeButtons.get(text(node.id));const haystack=[node.name,node.description,systemName(node),node.input,node.output].map(text).join(' ').toLocaleLowerCase('ru');const match=!query||haystack.includes(query);if(button){button.classList.toggle('selected',selected?.type==='node'&&text(selected.id)===text(node.id));button.setAttribute('aria-pressed',String(selected?.type==='node'&&text(selected.id)===text(node.id)));button.classList.toggle('dimmed',!match||(!!selected&&!neighborIds.has(text(node.id))));button.classList.toggle('search-match',!!query&&match);}const row=registryRows.get(text(node.id));if(row)row.hidden=!match;}
 for(const edge of validEdges){const group=edgeGroups.get(text(edge.id));if(!group)continue;const isSelected=selected?.type==='edge'&&text(selected.id)===text(edge.id);const isRelated=selected?.type==='node'&&(text(edge.source)===text(selected.id)||text(edge.target)===text(selected.id));group.classList.toggle('selected',isSelected);group.classList.toggle('dimmed',!!selected&&!isSelected&&!isRelated);}
 const found=nodes.filter(n=>!query||[n.name,n.description,systemName(n),n.input,n.output].map(text).join(' ').toLocaleLowerCase('ru').includes(query)).length;
 $('search-result').textContent=query?'Найдено шагов: '+found+' из '+nodes.length:'Подробная карта выбранного сценария';$('registry-empty').hidden=found>0;
}
function announce(value){$('announcement').textContent=value;}
function availableWidth(){const view=$('viewport');return Math.max(180,view.clientWidth-(!$('inspector').hidden&&window.innerWidth>700?$('inspector').offsetWidth:0));}
function openInspector(){if($('inspector').hidden)restoreFocus=document.activeElement;$('inspector').hidden=false;$('inspector-content').scrollTop=0;applyZoom(zoom);if(window.innerWidth<=700)$('close-inspector').focus({preventScroll:true});}
function closeInspector(restore=true){selected=null;$('inspector').hidden=true;updateHighlight();applyZoom(zoom);if(restore&&restoreFocus&&document.contains(restoreFocus))restoreFocus.focus({preventScroll:true});restoreFocus=null;}
function selectNode(node,center=false){selected={type:'node',id:node.id};showNode(node);openInspector();updateHighlight();announce('Выбран шаг: '+(node.name||node.id));if(center){activateTab('map');requestAnimationFrame(()=>centerNode(node));}else if(window.innerWidth>700)centerNode(node);}
function selectEdge(edge){selected={type:'edge',id:edge.id,edge};showEdge(edge);openInspector();updateHighlight();announce('Выбран переход: '+(edge.description||edgeNames[edge.kind]||'Связь'));}
function centerNode(node){const p=positions.get(text(node.id));if(!p)return;const view=$('viewport'),stage=$('stage');view.scrollTo({left:Math.max(0,(p.x+p.w/2)*zoom+(parseFloat(stage.style.left)||0)-availableWidth()/2),top:Math.max(0,(p.y+p.h/2)*zoom+(parseFloat(stage.style.top)||0)-view.clientHeight/2)});}
function activateTab(name){activeTab=name;for(const candidate of ['map','registry','gaps']){const active=candidate===name;const tab=$('tab-'+candidate);tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;$('panel-'+candidate).hidden=!active;}if(name==='map')requestAnimationFrame(()=>{if(layoutDirty){layoutDirty=false;draw();initialView();}else applyZoom(zoom);});}
for(const name of ['map','registry','gaps']){const tab=$('tab-'+name);tab.addEventListener('click',()=>{closeInspector(false);activateTab(name);});tab.addEventListener('keydown',event=>{const names=['map','registry','gaps'];let index=names.indexOf(name);if(event.key==='ArrowRight')index=(index+1)%3;else if(event.key==='ArrowLeft')index=(index+2)%3;else if(event.key==='Home')index=0;else if(event.key==='End')index=2;else return;event.preventDefault();closeInspector(false);activateTab(names[index]);$('tab-'+names[index]).focus();});}
$('close-inspector').addEventListener('click',()=>closeInspector());
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&!$('inspector').hidden){event.preventDefault();closeInspector();}else if(event.key==='Escape'){const scope=document.querySelector('.scope');if(scope.open){scope.open=false;scope.querySelector('summary').focus();}}});
document.addEventListener('pointerdown',event=>{const scope=document.querySelector('.scope');if(scope.open&&!scope.contains(event.target))scope.open=false;});
$('search').addEventListener('input',event=>{query=event.target.value.trim().toLocaleLowerCase('ru');matchIndex=-1;updateHighlight();announce($('search-result').textContent+(query?'. Enter — перейти к совпадению.':''));});
$('search').addEventListener('keydown',event=>{if(event.key==='Escape'){$('search').value='';query='';matchIndex=-1;updateHighlight();}else if(event.key==='Enter'&&query){const matches=nodes.filter(n=>[n.name,n.description,systemName(n),n.input,n.output].map(text).join(' ').toLocaleLowerCase('ru').includes(query));if(matches.length){event.preventDefault();matchIndex=(matchIndex+1)%matches.length;selectNode(matches[matchIndex],true);}}});

for(const node of nodes){const row=el('tr');const name=el('td');relatedButton(name,node.name || node.id,()=>selectNode(node,true));name.append(el('span','type-label',nodeKindLabel(node)));const status=el('td');status.append(badge(node.evidence_status));row.append(name,el('td','',systemName(node)),el('td','',node.description || 'Описание не задано'),status);$('registry').append(row);registryRows.set(text(node.id),row);}
if(!openFindings)$('gaps').append(el('p','empty','Агент не зафиксировал пробелов. Это не является гарантией полноты карты.'));
for(const gap of gaps){const card=el('article','gap');card.append(el('p','',gap.description || 'Описание пробела не задано.'));const links=el('div','related');for(const id of array(gap.node_ids)){const node=byId.get(text(id));if(node)relatedButton(links,node.name || node.id,()=>selectNode(node,true));}for(const id of array(gap.system_ids)){const system=systemById.get(text(id));if(system){const label=el('span','badge '+evidenceStatus(system.evidence_status),'АС: '+(system.name || system.id)+(evidenceStatus(system.evidence_status)==='unknown'?' · Не установлено':''));links.append(label);}}card.append(links);$('gaps').append(card);}
for(const [category,items] of [['Материал не исследован',omissions],['Незакрытая проверка карты',validationIssues]])for(const item of items){const card=el('article','gap');card.append(el('div','gap-category',category),el('p','',typeof item==='object'?(item.description||item.message||item.reason||item):item));$('gaps').append(card);}
if(edges.length!==validEdges.length){const card=el('article','gap');card.append(el('p','','Часть переходов ссылается на отсутствующие шаги и не может быть показана: '+(edges.length-validEdges.length)+'.'));$('gaps').append(card);}

__GRAPH_LAYOUT_JS__
function draw(){
 const nodeContainer=$('nodes');nodeContainer.replaceChildren();$('connections').replaceChildren();positions.clear();nodeButtons.clear();edgeGroups.clear();
 if(!nodes.length){$('empty-map').hidden=false;$('canvas-size').hidden=true;return;}else{$('empty-map').hidden=true;$('canvas-size').hidden=false;}
 const vertical=$('viewport').clientWidth<740,sizes=new Map();
 for(const node of nodes){const id=text(node.id),kind=Object.prototype.hasOwnProperty.call(kindNames,node.kind)?node.kind:'action';const button=el('button','node kind-'+kind);button.type='button';button.setAttribute('aria-label',nodeKindLabel(node)+': '+(node.name||node.id)+'. '+systemName(node)+'. '+evidenceNames[evidenceStatus(node.evidence_status)]);button.setAttribute('aria-pressed','false');const type=el('span','node-type');type.append(el('span','symbol',symbols[kind]),document.createTextNode(nodeKindLabel(node)));button.append(type,el('span','node-name',node.name || node.id),el('span','node-system','АС · '+systemName(node)),el('span','node-status '+evidenceStatus(node.evidence_status),evidenceNames[evidenceStatus(node.evidence_status)]));button.addEventListener('click',()=>selectNode(node));nodeContainer.append(button);nodeButtons.set(id,button);sizes.set(id,{w:224,h:button.offsetHeight});}
 const layout=layoutScenarioGraph(nodes,validEdges,sizes,vertical),rank=layout.rank;graphWidth=layout.width;graphHeight=layout.height;
 for(const [id,p] of layout.positions){positions.set(id,p);const button=nodeButtons.get(id);button.style.left=p.x+'px';button.style.top=p.y+'px';}
 $('stage').style.width=graphWidth+'px';$('stage').style.height=graphHeight+'px';
 const drawing=$('connections');drawing.setAttribute('width',graphWidth);drawing.setAttribute('height',graphHeight);drawing.setAttribute('viewBox','0 0 '+graphWidth+' '+graphHeight);
 const defs=svg('defs');const marker=svg('marker',{id:'arrow',viewBox:'0 0 10 10',refX:8,refY:5,markerWidth:6,markerHeight:6,orient:'auto-start-reverse'});marker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#B7C2CB'}));defs.append(marker);drawing.append(defs);
 const labelContext=document.createElement('canvas').getContext('2d');if(labelContext)labelContext.font="11px 'SB Sans Display', Arial, sans-serif";
 const labelWidth=value=>labelContext?labelContext.measureText(value).width:value.length*6.5;
 function wrapLabel(value,width,maxLines){const words=text(value).trim().split(/\s+/),lines=[];let line='';for(const word of words){if(line&&labelWidth(line+' '+word)<=width){line+=' '+word;continue;}if(line){lines.push(line);line='';}if(labelWidth(word)<=width){line=word;continue;}let part='';for(const char of word){if(part&&labelWidth(part+char)>width){lines.push(part);part='';}part+=char;}line=part;}if(line)lines.push(line);if(lines.length>maxLines){lines.length=maxLines;while(lines[maxLines-1]&&labelWidth(lines[maxLines-1]+'…')>width)lines[maxLines-1]=lines[maxLines-1].slice(0,-1);lines[maxLines-1]+='…';}return lines;}
 let backIndex=0;const pairCounts=new Map(),pairUsed=new Map();for(const edge of validEdges){const key=JSON.stringify([edge.source,edge.target]);pairCounts.set(key,(pairCounts.get(key)||0)+1);}
 for(const edge of validEdges){const a=positions.get(text(edge.source)),b=positions.get(text(edge.target)),key=JSON.stringify([edge.source,edge.target]),pairIndex=pairUsed.get(key)||0;pairUsed.set(key,pairIndex+1);const bend=(pairIndex-(pairCounts.get(key)-1)/2)*28,port=Math.max(-18,Math.min(18,bend/2));let start,c1,c2,end,routed;
 if(text(edge.source)===text(edge.target)){const reach=65+pairIndex*24;if(vertical){start={x:a.x+a.w/2,y:a.y+a.h};c1={x:start.x,y:start.y+reach};c2={x:a.x+a.w+reach,y:a.y+a.h/2+reach};end={x:a.x+a.w+3,y:a.y+a.h/2};}else{start={x:a.x+a.w,y:a.y+a.h/2};c1={x:start.x+reach,y:start.y};c2={x:a.x+a.w/2+reach,y:a.y-reach};end={x:a.x+a.w/2,y:a.y-3};}backIndex++;}
 else if(layout.outerEdges.has(text(edge.id)))routed=routeScenarioOuterEdge(edge,layout,vertical,backIndex++);
 else if(vertical){start={x:a.x+a.w/2+port,y:a.y+a.h};end={x:b.x+b.w/2+port,y:b.y-3};const reach=Math.max(28,(end.y-start.y)*.5);c1={x:start.x+bend,y:start.y+reach};c2={x:end.x+bend,y:end.y-reach};}
 else{start={x:a.x+a.w,y:a.y+a.h/2+port};end={x:b.x-3,y:b.y+b.h/2+port};const reach=Math.max(34,(end.x-start.x)*.5);c1={x:start.x+reach,y:start.y+bend};c2={x:end.x-reach,y:end.y+bend};}
 const path=routed?routed.path:`M ${start.x} ${start.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${end.x} ${end.y}`;
 const labelX=routed?routed.label.x:(start.x+3*c1.x+3*c2.x+end.x)/8,labelY=routed?routed.label.y:(start.y+3*c1.y+3*c2.y+end.y)/8-9;
 const group=svg('g',{'class':'edge edge-'+evidenceStatus(edge.evidence_status),tabindex:0,role:'button','aria-label':(edgeNames[edge.kind]||'Связь')+': '+(byId.get(text(edge.source)).name||edge.source)+' → '+(byId.get(text(edge.target)).name||edge.target)+(edge.condition?'. '+edge.condition:'')});
 const title=svg('title');title.textContent=[edge.description,edge.condition,evidenceNames[evidenceStatus(edge.evidence_status)]].filter(Boolean).join(' · ');group.append(title,svg('path',{d:path,'class':'edge-visible','marker-end':'url(#arrow)'}),svg('path',{d:path,'class':'edge-hit'}));
 if(edge.kind!=='sequence'||edge.condition){const forward=rank.get(text(edge.target))>rank.get(text(edge.source));const freeWidth=vertical&&routed?90:!vertical&&forward?Math.max(24,b.x-(a.x+a.w)-18):170;const lines=wrapLabel(edge.condition || edgeNames[edge.kind] || 'Связь',freeWidth,vertical&&forward&&!routed?2:4);const label=svg('text',{'text-anchor':vertical&&routed?'end':'middle','class':'edge-label'});const top=labelY-(lines.length-1)*6.5;for(let line=0;line<lines.length;line++){const span=svg('tspan',{x:labelX,y:top+line*13});span.textContent=lines[line];label.append(span);}group.append(label);}
 group.addEventListener('click',()=>selectEdge(edge));group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();selectEdge(edge);}});drawing.append(group);edgeGroups.set(text(edge.id),group);
 }
 applyZoom(zoom);updateHighlight();
}
function minimumZoom(){return Math.max(.001,Math.min(.16,(availableWidth()-24)/graphWidth,($('viewport').clientHeight-24)/graphHeight));}
function applyZoom(value,anchor){const view=$('viewport');const old=zoom;zoom=Math.min(1.7,Math.max(minimumZoom(),value));const padX=Math.max(0,(availableWidth()-graphWidth*zoom)/2),padY=Math.max(0,(view.clientHeight-graphHeight*zoom)/2);$('stage').style.transform='scale('+zoom+')';$('stage').style.left=padX+'px';$('stage').style.top=padY+'px';$('canvas-size').style.width=graphWidth*zoom+padX*2+(view.clientWidth-availableWidth())+'px';$('canvas-size').style.height=graphHeight*zoom+padY*2+'px';$('zoom-value').textContent=Math.round(zoom*100)+'%';$('zoom-out').disabled=zoom<=minimumZoom();$('zoom-in').disabled=zoom>=1.7;if(anchor){view.scrollLeft=(view.scrollLeft+anchor.x)*zoom/old-anchor.x;view.scrollTop=(view.scrollTop+anchor.y)*zoom/old-anchor.y;}}
function fit(){if(!nodes.length)return;const view=$('viewport');applyZoom(Math.min((availableWidth()-24)/graphWidth,(view.clientHeight-24)/graphHeight,1));view.scrollTo(0,0);}
function initialView(){if(!nodes.length)return;const fitted=Math.min((availableWidth()-24)/graphWidth,($('viewport').clientHeight-24)/graphHeight,1);applyZoom(Math.max(.8,fitted));if(fitted<.8)centerNode(nodes.find(n=>n.kind==='start')||nodes[0]);else $('viewport').scrollTo(0,0);}
function zoomBy(factor){const view=$('viewport');applyZoom(zoom*factor,{x:availableWidth()/2,y:view.clientHeight/2});}
$('zoom-in').addEventListener('click',()=>zoomBy(1.2));$('zoom-out').addEventListener('click',()=>zoomBy(1/1.2));$('fit').addEventListener('click',fit);
$('reset').addEventListener('click',()=>{closeInspector(false);query='';$('search').value='';showOverview();updateHighlight();applyZoom(1);$('viewport').scrollTo(0,0);announce('Выбор и поиск сброшены. Масштаб 100 процентов.');});
const viewport=$('viewport');viewport.addEventListener('keydown',event=>{if(event.target!==viewport)return;if(event.key==='+'||event.key==='='){event.preventDefault();zoomBy(1.2);}else if(event.key==='-'){event.preventDefault();zoomBy(1/1.2);}else if(event.key==='0'){event.preventDefault();fit();}else if(event.key==='Escape'){selected=null;showOverview();updateHighlight();}});
viewport.addEventListener('wheel',event=>{if(!event.ctrlKey&&!event.metaKey)return;event.preventDefault();const rect=viewport.getBoundingClientRect();applyZoom(zoom*(event.deltaY<0?1.08:1/1.08),{x:event.clientX-rect.left,y:event.clientY-rect.top});},{passive:false});
let drag=null;viewport.addEventListener('pointerdown',event=>{if(event.button!==0||event.pointerType==='touch'||event.target.closest('button,.edge'))return;drag={id:event.pointerId,x:event.clientX,y:event.clientY,left:viewport.scrollLeft,top:viewport.scrollTop};viewport.setPointerCapture(event.pointerId);viewport.classList.add('dragging');});viewport.addEventListener('pointermove',event=>{if(!drag||drag.id!==event.pointerId)return;viewport.scrollLeft=drag.left-(event.clientX-drag.x);viewport.scrollTop=drag.top-(event.clientY-drag.y);});const stopDrag=()=>{drag=null;viewport.classList.remove('dragging');};viewport.addEventListener('pointerup',stopDrag);viewport.addEventListener('pointercancel',stopDrag);viewport.addEventListener('lostpointercapture',stopDrag);
let resizeFrame=0;window.addEventListener('resize',()=>{cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>{if(activeTab==='map'){draw();initialView();}else layoutDirty=true;});});
showOverview();draw();initialView();updateHighlight();
if(document.fonts?.ready)document.fonts.ready.then(()=>{if(activeTab==='map'){draw();initialView();}else layoutDirty=true;});
})();
'''.replace('__GRAPH_LAYOUT_JS__', _GRAPH_LAYOUT_JS)
