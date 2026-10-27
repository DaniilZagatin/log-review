"""Local source snapshots -> one researcher -> offline map and durable audit."""
from __future__ import annotations

import json
import math
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pko.audit import AuditedClient, audit_run, record
from pko.errors import PkoError
from pko.llm.tracing import flush_tracing, init_phoenix_tracing, trace_client, trace_span
from pko.observability.export import build_handoff
from pko.observability.inputs import load_business_text, load_profile, parse_field_map, profile_names, profile_runtime_log
from pko.observability.model_stage import ReportBudget, StageModel
from pko.observability.pipeline import ReportOptions, run_observability
from pko.output.publisher import write_outputs
from pko.render.scenario_map import render_scenario_map
from pko.scenario_map.agent import DEFAULT_MAX_SECONDS, DEFAULT_MAX_STEPS, DEFAULT_MODEL_TIMEOUT, DeadlineClient, run_agent
from pko.scenario_map.config import researcher_spec
from pko.scenario_map.sources import build_workspace


def run_scenario(scenario: str, sources: list, out_dir: Path, *, spec=None,
                 client=None, max_steps: int = DEFAULT_MAX_STEPS,
                 max_seconds: float = DEFAULT_MAX_SECONDS, thinking=None,
                 model_timeout: float = DEFAULT_MODEL_TIMEOUT,
                 summary_max_tokens: int = 8192, summary_mode: str = "standard",
                 observability: bool = False, report_options: ReportOptions | None = None,
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
    if isinstance(summary_max_tokens, bool) or not isinstance(summary_max_tokens, int) or not 512 <= summary_max_tokens <= 32768:
        raise PkoError("Бюджет суммаризации должен быть целым числом от 512 до 32768 токенов.")
    if summary_mode not in ("standard", "inherit"):
        raise PkoError("Режим суммаризации: standard или inherit.")
    if observability:
        _check_report_options(report_options)
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
                                      summary_mode=summary_mode,
                                      checkpoint=checkpoint, on_event=on_event)
                    model["run"].update(id=run_id, trace_id=run_span.trace_id,
                                        generated_at=datetime.now(timezone.utc).isoformat())
                    journal.outcome = model['run']['status']
                    # A failed local journal must be visible in the result, not only on stderr.
                    model["run"]["audit_status"] = "partial" if journal.failed else "complete"
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
                    if observability:
                        _publish_report(model, workspace, out_dir, run_id=run_id, trace_id=run_span.trace_id,
                                        map_json=map_json, options=report_options or ReportOptions(),
                                        client=client, spec=spec, model_timeout=model_timeout)
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


def _publish_report(model: dict, workspace, out_dir: Path, *, run_id: str, trace_id, map_json: str,
                    options: ReportOptions, client, spec, model_timeout: float) -> dict | None:
    """Report stages, tabbed HTML and handoff after the map is published.

    A report failure leaves the published map; a handoff failure leaves both.
    Outcomes are recorded in ``model["run"]``.
    """
    try:
        with trace_span("process_observability") as report_span:
            report = run_observability(model, workspace.tree, options=options, run_id=run_id,
                                       stage_model=_report_model(options, client, spec, model_timeout))
            report_span.set_output({row["id"]: row["execution_status"] for row in report["stages"]})
        with trace_span("render_report"):
            html = render_scenario_map(model, report)
        with trace_span("publish_report") as output_span:
            report_json = json.dumps(report, ensure_ascii=False, indent=2)
            write_outputs(out_dir, {
                "process-report": ("process_report.json", report_json),
                "scenario-map": ("scenario_map.html", html),
            })
            output_span.set_output({"files": ["process_report.json", "scenario_map.html"]})
        model["run"]["process_report"] = "process_report.json"
    except Exception as exc:
        record("process_report_failed", error_type=type(exc).__name__, error=str(exc)[:500])
        model["run"]["process_report_error"] = f"{type(exc).__name__}: {exc}"[:500]
        return None
    try:
        with trace_span("export_handoff") as export_span:
            files = build_handoff(model, report, {"scenario_map.json": map_json, "process_report.json": report_json},
                                  {"run_id": run_id, "trace_id": trace_id or None,
                                   "local_audit": f"audit/run_{run_id}.txt"})
            write_outputs(out_dir / "handoff", files)
            export_span.set_output({"files": [name for name, _ in files.values()]})
        model["run"]["handoff"] = "handoff/manifest.json"
    except Exception as exc:
        record("handoff_failed", error_type=type(exc).__name__, error=str(exc)[:500])
        model["run"]["handoff_error"] = f"{type(exc).__name__}: {exc}"[:500]
    return report


def _check_report_options(options: ReportOptions | None) -> None:
    if options is not None and (
            isinstance(options.max_requests, bool) or not isinstance(options.max_requests, int)
            or not 1 <= options.max_requests <= 200
            or not (math.isfinite(options.max_seconds) and options.max_seconds > 0)):
        raise PkoError("Лимиты этапов отчёта: от 1 до 200 запросов и конечное положительное время.",
                       hint="проверьте --observability-max-requests и --observability-max-seconds")


def load_saved_map(map_path) -> dict:
    """A published map of the current methodology; checkpoints and old maps are refused."""
    map_path = Path(map_path)
    try:
        model = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PkoError(f"Не удалось прочитать карту: {exc}", hint="укажите scenario_map.json прошлого запуска") from exc
    run = model.get("run") if isinstance(model, dict) else None
    if not isinstance(run, dict) or str(model.get("schema_version")) != "2.0":
        raise PkoError("Отчёт строится только по карте атомарных операций схемы 2.0.")
    if run.get("status") not in ("completed", "partial") or not model.get("nodes"):
        raise PkoError("Карта не содержит результата исследования: отчёт по ней не строится.",
                       hint="используйте scenario_map.json завершённого или частичного запуска, не checkpoint")
    return model


def run_report_from_map(map_path, sources: list, out_dir: Path, *, spec=None, thinking=None,
                        model_timeout: float = DEFAULT_MODEL_TIMEOUT,
                        report_options: ReportOptions | None = None, on_event=None) -> dict:
    """Rerun report stages over a saved map without new research.

    The sources must reproduce the map's snapshot digests, otherwise its cited
    lines may no longer exist. The rerun is a new run with its own trace that
    references the run which built the map.
    """
    _check_report_options(report_options)
    model = load_saved_map(map_path)
    if not sources or len(sources) > 32:
        raise PkoError("Нужно от 1 до 32 источников кода — те же, что при построении карты.")
    spec = spec or researcher_spec(thinking=thinking)
    out_dir = Path(out_dir).resolve()
    for source in sources:
        if Path(source).resolve().is_relative_to(out_dir):
            raise PkoError("Каталог результата совпадает с источником кода или содержит его.",
                           hint="выберите для --out отдельную подпапку, не содержащую источники")
    previous = model["run"].get("id")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
    with audit_run(out_dir / "audit" / f"run_{run_id}.txt", run_id) as journal:
        init_phoenix_tracing()
        try:
            with trace_span("scenario_report_run", run_id=run_id, resumed_from_run_id=previous) as run_span:
                with tempfile.TemporaryDirectory(prefix="belka-report-") as temporary:
                    with trace_span("prepare_sources") as source_span:
                        workspace = build_workspace(sources, Path(temporary) / "sources", exclude_paths=[out_dir])
                        source_span.set_output({"sources": workspace.sources, "omissions": workspace.omissions})
                    expected = {(row.get("id"), row.get("digest")) for row in model.get("sources") or []}
                    actual = {(row.get("id"), row.get("digest")) for row in workspace.sources}
                    if expected != actual:
                        raise PkoError("Источники не совпадают со снимком, по которому построена карта.",
                                       hint="передайте те же директории или архивы в том же порядке")
                    record("report_rerun", resumed_from_run_id=previous, sources=workspace.sources)
                    if on_event:
                        on_event("sources_ready", {"sources": len(workspace.sources), "files": len(workspace.tree.files)})
                    model["run"]["report_runs"] = [*model["run"].get("report_runs", []),
                                                   {"id": run_id, "trace_id": run_span.trace_id or None,
                                                    "generated_at": datetime.now(timezone.utc).isoformat()}]
                    map_json = json.dumps(model, ensure_ascii=False, indent=2)
                    write_outputs(out_dir, {"scenario-model": ("scenario_map.json", map_json)})
                    _publish_report(model, workspace, out_dir, run_id=run_id, trace_id=run_span.trace_id,
                                    map_json=map_json, options=report_options or ReportOptions(),
                                    client=None, spec=spec, model_timeout=model_timeout)
                    journal.outcome = "completed" if model["run"].get("process_report") else "failed"
                    model["run"]["report_runs"][-1]["audit_status"] = "partial" if journal.failed else "complete"
                    run_span.set_output({"process_report": model["run"].get("process_report"),
                                         "error": model["run"].get("process_report_error")})
                    return model
        finally:
            flush_tracing()


def _report_model(options: ReportOptions, research_client, spec, model_timeout: float) -> StageModel:
    """Report stages use the research provider with their own request and time budget."""
    deadline = time.monotonic() + options.max_seconds
    client = options.client or research_client
    if client is None:
        client = DeadlineClient(spec=spec, timeout=model_timeout)
        client.deadline = deadline
    client = trace_client(client, spec)
    audited = client if isinstance(client, AuditedClient) else AuditedClient(client)
    return StageModel(audited, ReportBudget(max_requests=options.max_requests, deadline=deadline))


def add_cli_command(subparsers):
    command = subparsers.add_parser("map-scenario", help="Построить карту атомарных операций выбранного сценария по исходникам")
    scenario = command.add_mutually_exclusive_group(required=True)
    scenario.add_argument("--scenario-text", default="", help="Краткое описание выбранного варианта сценария")
    scenario.add_argument("--scenario-file", default="", help="UTF-8 файл с кратким описанием варианта")
    scenario.add_argument("--from-map", default="",
                          help="Перестроить отчёт по сохранённой scenario_map.json без нового исследования; нужны те же --source и --observability")
    command.add_argument("--source", action="append", required=True, help="Директория или ZIP с кодом; можно повторять")
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
    command.add_argument("--observability", action="store_true",
                         help="Дополнительно построить отчёт наблюдаемости процесса и комплект передачи (пилотный режим)")
    command.add_argument("--observability-max-requests", type=int, default=24,
                         help="Лимит запросов к модели для этапов отчёта, 1–200 (по умолчанию 24)")
    command.add_argument("--observability-max-seconds", type=float, default=1800,
                         help="Лимит времени этапов отчёта в секундах, отдельный от исследования (по умолчанию 1800)")
    command.add_argument("--business-file", default="",
                         help="Бизнес-описание процесса (UTF-8, до 200 КБ) для этапов отчёта; источник требований, не кода")
    command.add_argument("--runtime-log", default="",
                         help="Необязательный JSONL реальных логов (до 100 000 записей, 20 МБ); в отчёт и модель попадает только агрегированный профиль")
    command.add_argument("--domain-profile", default=None, choices=profile_names(),
                         help="Необязательный предметный профиль вопросов и метрик для этапов отчёта, например bank-process-kpi")
    command.add_argument("--runtime-fields", default="",
                         help="Имена полей JSONL: event_key=...,case_id=...,occurred_at=...,event_id=...")
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

    if (args.business_file or args.runtime_log or args.domain_profile) and not args.observability:
        raise PkoError("Бизнес-описание, файл логов и профиль используются только с --observability.")
    if args.from_map and not args.observability:
        raise PkoError("--from-map перестраивает отчёт наблюдаемости: добавьте --observability.")
    out = Path(args.out or (Path(args.from_map).parent if args.from_map else "scenario-map-out")).resolve()
    # Inputs are checked before research so a bad file does not waste the research budget.
    business = load_business_text(args.business_file) if args.business_file else None
    runtime = (profile_runtime_log(args.runtime_log, parse_field_map(args.runtime_fields))
               if args.runtime_log else None)

    def event(name, data):
        if args.quiet:
            return
        if name == "sources_ready":
            print(f"Источников: {data['sources']}, файлов: {data['files']}")
        elif name == "tool" and (data["name"].startswith("upsert_") or data["name"] == "finish"):
            print(f"Исследование: операций на карте {data['nodes']}, переходов {data['edges']}" +
                  ("; данные приняты" if data.get("ok") else "; требуется уточнение"))
        elif name == "model_retry":
            print(f"Связь с моделью временно недоступна. Попытка {data['next_attempt']} "
                  f"из {data['max_attempts']} через {data['delay_seconds']:g} с.", flush=True)

    options = ReportOptions(max_requests=args.observability_max_requests, max_seconds=args.observability_max_seconds,
                            business=business, runtime=runtime,
                            profile=load_profile(args.domain_profile) if args.domain_profile else None)
    if args.from_map:
        model = run_report_from_map(args.from_map, args.source, out, thinking=args.thinking,
                                    model_timeout=args.model_timeout, report_options=options, on_event=event)
        print(f"Отчёт перестроен по карте запуска {model['run'].get('id')}")
    else:
        model = run_scenario(scenario, args.source, out, max_steps=args.max_steps,
                             max_seconds=args.max_seconds, model_timeout=args.model_timeout,
                             thinking=args.thinking, summary_max_tokens=args.summary_max_tokens,
                             summary_mode=args.summary_mode, observability=args.observability,
                             report_options=options, on_event=event)
    status = model["run"]["status"]
    labels = {"completed": "Исследование завершено", "partial": "Сохранён частичный результат",
              "failed": "Исследование не выполнено"}
    print(labels.get(status, status))
    if model["run"].get("reason"):
        print(model["run"]["reason"])
    print(f"Карта: {(out / 'scenario_map.html')}")
    print(f"Данные: {(out / 'scenario_map.json')}")
    if model["run"].get("process_report"):
        print(f"Отчёт наблюдаемости: {(out / 'process_report.json')}")
        if model["run"].get("handoff"):
            print(f"Комплект передачи: {(out / 'handoff')}")
        elif model["run"].get("handoff_error"):
            print(f"Комплект передачи не построен: {model['run']['handoff_error']}")
    elif model["run"].get("process_report_error"):
        print(f"Отчёт наблюдаемости не построен: {model['run']['process_report_error']}")
    if model["run"].get("interrupted"):
        return 130
    return 0 if status == "completed" else (2 if status == "partial" else 1)
