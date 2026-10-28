"""Local source snapshots -> one researcher -> offline map and durable audit."""
from __future__ import annotations

import json
import math
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pko.audit import AuditedClient, audit_run, current_audit, record, sanitize
from pko.build import build_identity
from pko.errors import PkoError
from pko.llm.tracing import flush_tracing, init_phoenix_tracing, trace_client, trace_span
from pko.output.publisher import write_outputs
from pko.render.scenario_map import render_scenario_map
from pko.scenario_map.agent import DEFAULT_MAX_SECONDS, DEFAULT_MAX_STEPS, DEFAULT_MODEL_TIMEOUT, DeadlineClient, run_agent
from pko.scenario_map.config import researcher_spec
from pko.scenario_map.sources import build_workspace, read_process_description
from pko.stages import grouping
from pko.stages.contracts import HierarchyError
from pko.stages.inputs import map_revision
from pko.stages.model import RequestBudget, StageModel

HIERARCHY_FILE = "map_hierarchy.json"
# One grouping request and one repair, with room for transient retries.
STAGES_MAX_REQUESTS = 4
STAGES_MAX_SECONDS = 1200.0
def run_scenario(scenario: str, sources: list, out_dir: Path, *, spec=None,
                 client=None, max_steps: int = DEFAULT_MAX_STEPS,
                 max_seconds: float = DEFAULT_MAX_SECONDS, thinking=None,
                 model_timeout: float = DEFAULT_MODEL_TIMEOUT,
                 summary_max_tokens: int = 8192, summary_mode: str = "standard",
                 stages: bool = True, on_event=None, demo: bool = False) -> dict:
    scenario = scenario.strip()
    if not scenario:
        raise PkoError("Нужно описание процесса: вариант сценария, который требуется восстановить по коду.",
                       hint="передайте файл через --process-file или текст через --scenario-text")
    if not sources or len(sources) > 32:
        raise PkoError("Нужно от 1 до 32 источников кода.",
                       hint="передайте директории или ZIP-архивы через --source")
    if max_steps <= 0 or not all(math.isfinite(value) and value > 0 for value in (max_seconds, model_timeout)):
        raise PkoError("Лимиты исследования и ожидания модели должны быть конечными положительными числами.",
                       hint="проверьте --max-steps, --max-seconds и --model-timeout")
    if isinstance(summary_max_tokens, bool) or not isinstance(summary_max_tokens, int) or not 512 <= summary_max_tokens <= 32768:
        raise PkoError("Бюджет суммаризации должен быть целым числом от 512 до 32768 токенов.")
    if summary_mode not in ("standard", "inherit"):
        raise PkoError("Режим суммаризации: standard или inherit.")
    spec = spec or researcher_spec(thinking=thinking)
    out_dir = Path(out_dir).resolve()
    # An output subtree can be excluded, but excluding an ancestor would silently
    # hide every file of a nested source (for example --source src --out .).
    for source in sources:
        if Path(source).resolve().is_relative_to(out_dir):
            raise PkoError("Каталог результата совпадает с источником кода или содержит его.",
                           hint="выберите для --out отдельную подпапку, не содержащую источники")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    with audit_run(out_dir / "audit" / f"run_{run_id}.txt", run_id) as journal:
        init_phoenix_tracing()
        try:
            with trace_span("scenario_map_run", run_id=run_id) as run_span:
                run_span.set_input({"scenario": scenario, "source_count": len(sources),
                                    "max_steps": max_steps, "max_seconds": max_seconds,
                                    "model_timeout_seconds": model_timeout,
                                    "summary_max_tokens": summary_max_tokens, "summary_mode": summary_mode})
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
                                      model_timeout=model_timeout, summary_max_tokens=summary_max_tokens,
                                      summary_mode=summary_mode, checkpoint=checkpoint, on_event=on_event)
                    model["run"].update(id=run_id, trace_id=run_span.trace_id,
                                        generated_at=datetime.now(timezone.utc).isoformat(), build=build_identity())
                    if demo:
                        model["run"]["demo"] = True
                    journal.outcome = model['run']['status']
                    # A failed local journal must be visible in the result, not only on stderr.
                    _refresh_audit_status(model)
                    run_span.set_attribute("pko.run.status", model["run"]["status"])
                    run_span.set_attribute("pko.model.mode", model['run'].get('model_mode', 'provider_default'))
                    with trace_span("render_map"):
                        html = render_scenario_map(model)
                    with trace_span("publish_outputs") as output_span:
                        map_json = json.dumps(model, ensure_ascii=False, indent=2)
                        write_outputs(out_dir, {
                            "scenario-model": ("scenario_map.json", map_json),
                            "scenario-map": ("scenario_map.html", html),
                        })
                        output_span.set_output({"files": ["scenario_map.json", "scenario_map.html"]})
                    hierarchy = None
                    if stages and model["run"]["status"] != "failed" and model.get("nodes"):
                        hierarchy = _publish_stages(model, out_dir, run_id=run_id, client=client, spec=spec,
                                                    model_timeout=model_timeout, on_event=on_event)
                    else:
                        _remove_hierarchy(out_dir)
                    # Grouping also uses the trace; persist failures observed after the first map save.
                    _refresh_audit_status(model)
                    with trace_span("publish_map"):
                        write_outputs(out_dir, {
                            "scenario-model": ("scenario_map.json", json.dumps(model, ensure_ascii=False, indent=2)),
                            "scenario-map": ("scenario_map.html", render_scenario_map(model, hierarchy)),
                        })
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


def _publish_stages(model: dict, out_dir: Path, *, run_id: str, client, spec, model_timeout: float,
                    on_event=None, map_text: str | None = None) -> dict:
    """Business stages over the published map, then the map HTML with both views.

    The grouping cannot change the atomic map: its revision is compared after
    the model answers. A failure still publishes ``map_hierarchy.json`` with
    every operation unassigned and the reason. ``map_text`` copies a saved map
    into another folder byte for byte.
    """
    revision = map_revision(model)
    stage_model = None
    try:
        with trace_span("business_stages") as span:
            stage_model = _stage_model(client, spec, model_timeout, STAGES_MAX_REQUESTS, STAGES_MAX_SECONDS)
            hierarchy = grouping.build_hierarchy(model, stage_model, run_id=run_id)
            if map_revision(model) != revision:
                raise HierarchyError("группировка изменила карту атомарных операций")
            span.set_output({"status": hierarchy["status"], "stages": len(hierarchy["stages"]),
                             "unassigned": len(hierarchy["unassigned"])})
    except Exception as exc:
        message = str(sanitize(f"{type(exc).__name__}: {exc}"))[:500]
        record("business_stages_failed", error_type=type(exc).__name__, error=message)
        hierarchy = grouping.unavailable_hierarchy(model, message, run_id=run_id,
                                                   budget=stage_model.budget.snapshot() if stage_model else None)
    with trace_span("render_map"):
        html = render_scenario_map(model, hierarchy)
    files = {"map-hierarchy": (HIERARCHY_FILE, json.dumps(hierarchy, ensure_ascii=False, indent=2)),
             "scenario-map": ("scenario_map.html", html)}
    if map_text is not None:
        files["scenario-model"] = ("scenario_map.json", map_text)
    write_outputs(out_dir, files)
    if on_event:
        on_event("business_stages", {"status": hierarchy["status"], "stages": len(hierarchy["stages"]),
                                     "collapsible": sum(stage["collapsible"] for stage in hierarchy["stages"]),
                                     "unassigned": len(hierarchy["unassigned"])})
    return hierarchy


def _remove_hierarchy(out_dir: Path) -> None:
    """Stages of an earlier map must not sit next to a map they do not describe."""
    try:
        (Path(out_dir) / HIERARCHY_FILE).unlink(missing_ok=True)
    except OSError as exc:
        record("stale_hierarchy_kept", error=str(sanitize(str(exc)))[:300])


def _refresh_audit_status(model: dict) -> None:
    """Health known immediately before publication, not a claim about future I/O."""
    journal = current_audit()
    model["run"]["trace_export"] = {**flush_tracing(), "scope": "до публикации результата"}
    if journal is not None:
        journal.flush()
    status = "partial" if journal is not None and journal.failed else "complete"
    model["run"]["audit_status"] = status


def load_saved_map(map_path) -> dict:
    """A published map of the current methodology; checkpoints and old maps are refused."""
    return _read_saved_map(map_path)[0]


def _read_saved_map(map_path) -> tuple[dict, str]:
    """Validate once, retaining the exact UTF-8 text for a rebuild in another folder."""
    map_path = Path(map_path)
    try:
        text = map_path.read_bytes().decode("utf-8")
        model = json.loads(text)
    except (OSError, UnicodeError, ValueError) as exc:
        raise PkoError(f"Не удалось прочитать карту: {exc}", hint="укажите scenario_map.json прошлого запуска") from exc
    run = model.get("run") if isinstance(model, dict) else None
    if not isinstance(run, dict) or str(model.get("schema_version")) != "2.0":
        raise PkoError("Перестроение возможно только по карте атомарных операций схемы 2.0.")
    if run.get("status") not in ("completed", "partial") or not model.get("nodes"):
        raise PkoError("Карта не содержит результата исследования: этапы по ней не строятся.",
                       hint="используйте scenario_map.json завершённого или частичного запуска, не checkpoint")
    return model, text


def rebuild_from_map(map_path, out_dir: Path, *, client=None, spec=None, thinking=None,
                     model_timeout: float = DEFAULT_MODEL_TIMEOUT, on_event=None) -> dict:
    """Rebuild business stages over a saved map without new research.

    The atomic map stays as saved; in another folder it is copied byte for
    byte. The rebuild is a new run with its own trace that references the run
    which built the map.
    """
    model, map_text = _read_saved_map(map_path)
    spec = spec or researcher_spec(thinking=thinking)
    out_dir = Path(out_dir).resolve()
    copy_saved_map = Path(map_path).resolve() != out_dir / "scenario_map.json"
    previous = model["run"].get("id")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    with audit_run(out_dir / "audit" / f"run_{run_id}.txt", run_id) as journal:
        init_phoenix_tracing()
        try:
            with trace_span("scenario_rebuild_run", run_id=run_id, resumed_from_run_id=previous) as run_span:
                record("map_rebuild", resumed_from_run_id=previous)
                hierarchy = _publish_stages(model, out_dir, run_id=run_id, client=client, spec=spec,
                                            model_timeout=model_timeout, on_event=on_event,
                                            map_text=map_text if copy_saved_map else None)
                journal.outcome = {"complete": "completed", "partial": "partial"}.get(
                    hierarchy["status"]["result"], "failed")
                run_span.set_output({"business_stages": hierarchy["status"]})
                return model
        finally:
            flush_tracing()


def _stage_model(client, spec, model_timeout: float, max_requests: int, max_seconds: float) -> StageModel:
    """A bounded model for grouping after research; without a client, the research provider."""
    deadline = time.monotonic() + max_seconds
    if client is None:
        client = DeadlineClient(spec=spec, timeout=model_timeout)
        client.deadline = deadline
    client = trace_client(client, spec)
    audited = client if isinstance(client, AuditedClient) else AuditedClient(client)
    return StageModel(audited, RequestBudget(max_requests=max_requests, deadline=deadline))


def add_cli_command(subparsers):
    command = subparsers.add_parser("map-scenario", help="Построить карту атомарных операций выбранного сценария по исходникам")
    scenario = command.add_mutually_exclusive_group(required=True)
    scenario.add_argument("--process-file", "--scenario-file", dest="process_file", default="",
                          help="Текстовый файл с описанием варианта процесса и полезными сведениями о нём (UTF-8)")
    scenario.add_argument("--scenario-text", default="", help="Описание варианта сценария строкой")
    scenario.add_argument("--from-map", default="",
                          help="Перестроить бизнес-этапы по сохранённой scenario_map.json без нового исследования")
    command.add_argument("--source", action="extend", nargs="+", default=[],
                         help="ZIP-архивы или директории с кодом: несколько через пробел, флаг можно повторять")
    command.add_argument("--out", default="", help="Каталог HTML-карты и JSON (по умолчанию scenario-map-out; с --from-map — папка карты)")
    command.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    command.add_argument("--max-seconds", type=float, default=DEFAULT_MAX_SECONDS,
                         help="Общий мягкий лимит исследования в секундах, включая очередь и повторы (по умолчанию 7200)")
    command.add_argument("--model-timeout", type=float, default=DEFAULT_MODEL_TIMEOUT,
                         help="Таймаут ожидания модели в секундах (по умолчанию 900); ограничен оставшимся временем исследования")
    command.add_argument("--summary-max-tokens", type=int, default=8192,
                         help="Бюджет ответа суммаризации, 512–32768 токенов (по умолчанию 8192)")
    command.add_argument("--summary-mode", choices=("standard", "inherit"), default="standard",
                         help="Суммаризация: standard отключает настроенный thinking, inherit наследует режим исследования")
    command.add_argument("--no-stages", dest="stages", action="store_false",
                         help="Не строить бизнес-этапы: карта будет только в виде атомарных операций")
    command.add_argument("--thinking", action="store_true", default=None)
    command.add_argument("--no-thinking", dest="thinking", action="store_false")
    command.add_argument("--quiet", action="store_true", help="Не печатать ход исследования")
    command.set_defaults(handler=command_main)


def command_main(args) -> int:
    if args.from_map and not args.stages:
        raise PkoError("Перестраивать нечего: по сохранённой карте строятся только бизнес-этапы.",
                       hint="уберите --no-stages")
    if args.from_map and args.source:
        raise PkoError("Перестроение этапов использует только сохранённую карту.", hint="уберите --source")
    if not args.from_map and not args.source:
        raise PkoError("Нужны источники кода.", hint="передайте ZIP-архивы или директории через --source")
    # Inputs are checked before research so a bad file does not waste the research budget.
    scenario = read_process_description(args.process_file) if args.process_file else args.scenario_text
    out = Path(args.out or (Path(args.from_map).parent if args.from_map else "scenario-map-out")).resolve()

    if not args.quiet:
        build = build_identity()
        print(f"Белка {build['version']} · код {build['code_sha256'][:12]}"
              + (f" · git {build['git_head'][:10]}{' с изменениями' if build['git_dirty'] else ''}" if build['git_head'] else ""))

    stages = {}

    def event(name, data):
        if name == "business_stages":
            stages.update(data)
        if args.quiet:
            return
        if name == "sources_ready":
            print(f"Источников: {data['sources']}, файлов: {data['files']}")
        elif name == "tool" and (data["name"].startswith("upsert_") or data["name"] == "finish"):
            print(f"Исследование: операций на карте {data['nodes']}, переходов {data['edges']}" +
                  ("; данные приняты" if data.get("ok") else "; требуется уточнение"))
        elif name == "business_stages":
            print(f"Бизнес-этапы: {data['stages']}, из них показываются блоком {data['collapsible']}; "
                  f"вне этапов операций {data['unassigned']}" if data["stages"] else
                  "Бизнес-этапы не построены: " + "; ".join(data["status"]["reasons"][:1]), flush=True)
        elif name == "model_retry":
            print(f"Связь с моделью временно недоступна. Попытка {data['next_attempt']} "
                  f"из {data['max_attempts']} через {data['delay_seconds']:g} с.", flush=True)

    if args.from_map:
        model = rebuild_from_map(args.from_map, out, thinking=args.thinking,
                                 model_timeout=args.model_timeout, on_event=event)
        print(f"Перестроено по карте запуска {model['run'].get('id')}")
    else:
        model = run_scenario(scenario, args.source, out, max_steps=args.max_steps,
                             max_seconds=args.max_seconds, model_timeout=args.model_timeout,
                             thinking=args.thinking, summary_max_tokens=args.summary_max_tokens,
                             summary_mode=args.summary_mode, stages=args.stages, on_event=event)
    status = model["run"]["status"]
    labels = {"completed": "Исследование завершено", "partial": "Сохранён частичный результат",
              "failed": "Исследование не выполнено"}
    print(labels.get(status, status))
    if model["run"].get("reason"):
        print(model["run"]["reason"])
    print(f"Карта: {(out / 'scenario_map.html')}")
    print(f"Данные: {(out / 'scenario_map.json')}")
    if stages:
        print(f"Бизнес-этапы: {(out / HIERARCHY_FILE)}")
    if model["run"].get("interrupted"):
        return 130
    if args.from_map:
        # A rebuild is judged by what it rebuilt; the saved research keeps its own status.
        result = stages.get("status", {}).get("result", "complete")
        if result == "none":
            return 1
        return 2 if result == "partial" else 0
    return 0 if status == "completed" else (2 if status == "partial" else 1)
