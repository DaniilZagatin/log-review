"""Local source snapshots -> one researcher -> offline map and durable audit."""
from __future__ import annotations

import json
import math
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pko.audit import audit_run, record
from pko.errors import PkoError
from pko.llm.tracing import flush_tracing, init_phoenix_tracing, trace_span
from pko.output.publisher import write_outputs
from pko.render.scenario_map import render_scenario_map
from pko.scenario_map.agent import DEFAULT_MAX_SECONDS, DEFAULT_MAX_STEPS, DEFAULT_MODEL_TIMEOUT, run_agent
from pko.scenario_map.config import researcher_spec
from pko.scenario_map.sources import build_workspace


def run_scenario(scenario: str, sources: list, out_dir: Path, *, spec=None,
                 client=None, max_steps: int = DEFAULT_MAX_STEPS,
                 max_seconds: float = DEFAULT_MAX_SECONDS, thinking=None,
                 model_timeout: float = DEFAULT_MODEL_TIMEOUT,
                 on_event=None) -> dict:
    scenario = scenario.strip()
    if not scenario or len(scenario) > 20000:
        raise PkoError("Нужно краткое описание варианта сценария (до 20 000 символов).",
                       hint="укажите вариант, который требуется восстановить по коду")
    if not sources or len(sources) > 32:
        raise PkoError("Нужно от 1 до 32 источников кода.",
                       hint="передайте директории или ZIP-архивы через --source")
    if max_steps <= 0 or not all(math.isfinite(value) and value > 0 for value in (max_seconds, model_timeout)):
        raise PkoError("Лимиты исследования и ожидания модели должны быть конечными положительными числами.",
                       hint="проверьте --max-steps, --max-seconds и --model-timeout")
    spec = spec or researcher_spec(thinking=thinking)
    out_dir = Path(out_dir).resolve()
    # An output subtree can be excluded, but excluding an ancestor would silently
    # hide every file of a nested source (for example --source src --out .).
    for source in sources:
        if Path(source).resolve().is_relative_to(out_dir):
            raise PkoError("Каталог результата совпадает с источником кода или содержит его.",
                           hint="выберите для --out отдельную подпапку, не содержащую источники")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    with audit_run(out_dir / "audit" / f"run_{run_id}.txt", run_id):
        init_phoenix_tracing()
        try:
            with trace_span("scenario_map_run", run_id=run_id) as run_span:
                run_span.set_input({"scenario": scenario, "source_count": len(sources),
                                    "max_steps": max_steps, "max_seconds": max_seconds,
                                    "model_timeout_seconds": model_timeout})
                with tempfile.TemporaryDirectory(prefix="belka-scenario-") as temporary:
                    with trace_span("prepare_sources") as source_span:
                        workspace = build_workspace(sources, Path(temporary) / "sources",
                                                    exclude_paths=[out_dir])
                        source_span.set_output({"sources": workspace.sources,
                                                "notes": workspace.notes,
                                                "omissions": workspace.omissions})
                    record("scenario_sources", sources=workspace.sources, notes=workspace.notes)
                    if on_event:
                        on_event("sources_ready", {"sources": len(workspace.sources),
                                                   "files": len(workspace.tree.files)})

                    def checkpoint(model):
                        model["run"]["id"] = run_id
                        model["run"]["trace_id"] = run_span.trace_id
                        with trace_span("save_checkpoint", step=model["run"].get("steps")):
                            write_outputs(out_dir, {"checkpoint": ("scenario_map.checkpoint.json",
                                          json.dumps(model, ensure_ascii=False, indent=2))})

                    model = run_agent(workspace, scenario, spec, client=client,
                                      max_steps=max_steps, max_seconds=max_seconds,
                                      model_timeout=model_timeout,
                                      checkpoint=checkpoint, on_event=on_event)
                    model["run"].update(id=run_id, trace_id=run_span.trace_id,
                                        generated_at=datetime.now(timezone.utc).isoformat())
                    run_span.set_attribute("pko.run.status", model["run"]["status"])
                    with trace_span("render_map"):
                        html = render_scenario_map(model)
                    with trace_span("publish_outputs") as output_span:
                        write_outputs(out_dir, {
                            "scenario-model": ("scenario_map.json", json.dumps(model, ensure_ascii=False, indent=2)),
                            "scenario-map": ("scenario_map.html", html),
                        })
                        output_span.set_output({"files": ["scenario_map.json", "scenario_map.html"]})
                    run_span.set_output({"run": model["run"], "nodes": len(model.get("nodes", [])),
                                         "edges": len(model.get("edges", [])),
                                         "gaps": len(model.get("gaps", [])),
                                         "validation": model.get("validation", {})})
                    if model["run"]["status"] == "failed":
                        run_span.record_error(RuntimeError(model["run"].get("reason") or "Исследование не выполнено"))
                    record("scenario_result", status=model["run"]["status"],
                           nodes=len(model.get("nodes", [])), edges=len(model.get("edges", [])),
                           gaps=len(model.get("gaps", [])))
                    return model
        finally:
            flush_tracing()


def add_cli_command(subparsers):
    command = subparsers.add_parser("map-scenario", help="Построить карту варианта сценария по коду нескольких АС")
    scenario = command.add_mutually_exclusive_group(required=True)
    scenario.add_argument("--scenario-text", default="", help="Краткое описание выбранного варианта сценария")
    scenario.add_argument("--scenario-file", default="", help="UTF-8 файл с кратким описанием варианта")
    command.add_argument("--source", action="append", required=True, help="Директория или ZIP с кодом; можно повторять")
    command.add_argument("--out", default="scenario-map-out", help="Каталог HTML-карты и JSON")
    command.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    command.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS,
                         help="Общий мягкий лимит исследования в секундах, включая очередь и повторы (по умолчанию 7200)")
    command.add_argument("--model-timeout", type=float, default=DEFAULT_MODEL_TIMEOUT,
                         help="Таймаут ожидания модели в секундах (по умолчанию 900); ограничен оставшимся временем исследования")
    command.add_argument("--thinking", action="store_true", default=None)
    command.add_argument("--no-thinking", dest="thinking", action="store_false")
    command.add_argument("--quiet", action="store_true", help="Не печатать ход исследования")
    command.set_defaults(handler=command_main)


def command_main(args) -> int:
    scenario = args.scenario_text
    if args.scenario_file:
        try:
            path = Path(args.scenario_file)
            if path.stat().st_size > 80000:
                raise ValueError("описание слишком велико")
            scenario = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError, ValueError) as exc:
            raise PkoError(f"Не удалось прочитать описание сценария: {exc}",
                           hint="проверьте путь и кодировку UTF-8") from exc

    def event(name, data):
        if args.quiet:
            return
        if name == "sources_ready":
            print(f"Источников: {data['sources']}, файлов: {data['files']}")
        elif name == "tool" and (data["name"].startswith("upsert_") or data["name"] == "finish"):
            print(f"Исследование: шагов на карте {data['nodes']}, переходов {data['edges']}" +
                  ("; данные приняты" if data.get("ok") else "; требуется уточнение"))
        elif name == "model_retry":
            print(f"Связь с моделью временно недоступна. Попытка {data['next_attempt']} "
                  f"из {data['max_attempts']} через {data['delay_seconds']:g} с.", flush=True)

    model = run_scenario(scenario, args.source, Path(args.out), max_steps=args.max_steps,
                         max_seconds=args.max_seconds, model_timeout=args.model_timeout,
                         thinking=args.thinking, on_event=event)
    status = model["run"]["status"]
    labels = {"completed": "Исследование завершено", "partial": "Сохранён частичный результат",
              "failed": "Исследование не выполнено"}
    print(labels.get(status, status))
    if model["run"].get("reason"):
        print(model["run"]["reason"])
    print(f"Карта: {(Path(args.out).resolve() / 'scenario_map.html')}")
    print(f"Данные: {(Path(args.out).resolve() / 'scenario_map.json')}")
    if model["run"].get("interrupted"):
        return 130
    return 0 if status == "completed" else (2 if status == "partial" else 1)
