"""Developer handoff package built from a validated report.

Files: a readable process description, structured onboarding JSON, CSV tables
of events, changes and metrics, and a manifest with checksums. CSV cells that
a spreadsheet could execute are neutralised. Nothing here invents values: an
unagreed key stays a proposal, an unknown place stays unresolved, and metric
values are absent. No source code, raw runtime records or reasoning is exported.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone

from pko.observability.contracts import map_revision, validate_report

FORMAT = "belka-handoff/1"
_FORMULA = tuple("=+-@\t\r＝＋－＠")


def safe_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = "; ".join(str(item) for item in value)
    value = str(value)
    # A leading formula character is neutralised; the apostrophe is visible, not hidden.
    return "'" + value if value.startswith(_FORMULA) or value.startswith("'") else value


def _csv(columns: list[str], rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([safe_cell(row.get(column)) for column in columns])
    return "﻿" + buffer.getvalue()


def _names(model: dict) -> dict:
    return {node["id"]: node.get("name") or node["id"] for node in model.get("nodes") or []}


def _events(model: dict, report: dict) -> list[dict]:
    names = _names(model)
    sites = {site["id"]: site for site in report.get("logging", {}).get("sites") or []}
    rows = []
    for binding in report.get("logging", {}).get("event_bindings") or []:
        site = sites.get(binding["site_id"], {})
        rows.append({"binding_id": binding["id"], "status": binding["status"], "operation_id": binding.get("operation_id"),
                     "operation": names.get(binding.get("operation_id")), "path": site.get("path"),
                     "line_start": site.get("line_start"), "line_end": site.get("line_end"),
                     "event_key": site.get("event_key"), "key_status": site.get("key_status"),
                     "event_role": binding["event_role"], "lifecycle_moment": binding["lifecycle_moment"],
                     "repeat_semantics": binding["repeat_semantics"], "case_fields": binding["case_fields"],
                     "fields": site.get("fields"), "business_meaning": binding["business_meaning"],
                     "condition": binding.get("condition"), "question": binding.get("question")})
    return rows


def _changes(report: dict) -> list[dict]:
    rows = []
    for change in report.get("instrumentation") or []:
        location, event = change["location"], change["event"]
        rows.append({"change_id": change["id"], "action": change["action"], "fact_ids": change["fact_ids"],
                     "operation_ids": change["operation_ids"], "question": change["question"],
                     "location": (f"{location['path']}:{location['line_start']}-{location['line_end']}"
                                  if location["status"] == "anchored" else "не определено"),
                     "search_task": location.get("search_task"), "change": change["change"],
                     "event_key": event.get("key"), "key_status": event.get("key_status"),
                     "event_fields": [f"{field['name']}: {field.get('purpose', '')}" for field in event.get("fields") or []],
                     "lifecycle": event.get("lifecycle"), "timing": event.get("timing"),
                     "identity": event.get("identity"), "dedup": event.get("dedup"),
                     "depends_on": change["depends_on"], "validation": change["validation"]})
    return rows


def _metrics(report: dict) -> list[dict]:
    return [{"metric_id": metric["id"], "name": metric["name"], "purpose": metric["purpose"],
             "counting_unit": metric["counting_unit"], "measure_unit": metric["measure_unit"],
             "definition": metric["definition"], "population": metric["population"], "window": metric["window"],
             "open_cases": metric["rules"].get("open_cases"), "late_events": metric["rules"].get("late_events"),
             "retries": metric["rules"].get("retries"), "duplicates": metric["rules"].get("duplicates"),
             "fact_ids": metric["fact_ids"], "change_ids": metric["change_ids"],
             "readiness": metric["readiness"]["status"], "blockers": metric["readiness"]["blockers"],
             "value": "", "open_questions": metric["open_questions"]}
            for metric in report.get("metrics") or []]


def _markdown(model: dict, report: dict) -> str:
    names = _names(model)
    scenario = model.get("scenario") or {}
    context = report.get("process_context") or {}
    lines = [f"# {scenario.get('title') or 'Процесс'}", "",
             "Комплект подготовлен Белкой по исходникам и, если передано, бизнес-описанию. Все выводы — предложения для "
             "проверки специалистом: методика не согласована, значения метрик не вычислялись.", "",
             f"Ревизия карты: `{report['map_revision']}`. Запуск: `{report.get('run_id')}`.", ""]
    if context:
        lines += ["## Контекст", "", f"- Цель: {context.get('goal')}", f"- Начало: {context.get('start')}",
                  f"- Завершение: {context.get('finish')}",
                  f"- Экземпляр процесса: {context.get('case', {}).get('subject')}"]
        lines += [f"  - идентификатор `{row['name']}` ({row['source']})" for row in context.get("case", {}).get("identifiers") or []]
        lines += ["", "### Вопросы измерения", ""] + [f"- {row['id']}: {row['question']}" for row in context["measurement_questions"]]
        if context.get("open_questions"):
            lines += ["", "### Открытые вопросы", ""] + [f"- {value}" for value in context["open_questions"]]
        lines.append("")
    coverage = {row["fact_id"]: row for row in (report.get("coverage") or {}).get("facts") or []}
    if report.get("facts"):
        lines += ["## Необходимые факты", ""]
        for fact in report["facts"]:
            row = coverage.get(fact["id"], {})
            operations = ", ".join(names.get(ref, ref) for ref in fact["operation_ids"]) or "вне приложения"
            state = "наблюдается в коде" if row.get("observable_in_code") else \
                "пробелы: " + (", ".join(row.get("gaps", []) + row.get("unknowns", [])) or "нет данных")
            lines.append(f"- {fact['id']} — {fact['meaning']} ({operations}); {state}")
        lines.append("")
    if report.get("instrumentation"):
        lines += ["## Доработки логирования", ""]
        for change in report["instrumentation"]:
            location = change["location"]
            where = (f"`{location['path']}:{location['line_start']}-{location['line_end']}`" if location["status"] == "anchored"
                     else f"место не определено — {location.get('search_task')}")
            lines += [f"### {change['id']}: {change['change']}", "", f"- Вид: {change['action']}; факты: {', '.join(change['fact_ids'])}",
                      f"- Где: {where}", f"- Проверка: {change['validation']}", ""]
    if report.get("metrics"):
        lines += ["## Определения метрик", ""]
        for metric in report["metrics"]:
            lines += [f"### {metric['id']}: {metric['name']}", "", f"- Определение: {metric['definition']}",
                      f"- Единица счёта: {metric['counting_unit']}; измерение: {metric['measure_unit']}",
                      "- Не готово: " + " ".join(metric["readiness"]["blockers"]), ""]
    return "\n".join(lines) + "\n"


def build_handoff(model: dict, report: dict, published: dict[str, str], trace: dict) -> dict[str, tuple[str, str]]:
    """Return ``{kind: (file name, content)}`` including the manifest.

    ``published`` maps already written neighbour files (map, report) to their
    content so the manifest can fingerprint them.
    """
    validate_report(report, model)
    if report["map_revision"] != map_revision(model):
        raise ValueError("handoff must be built from the report of this map revision")
    events, changes, metrics = _events(model, report), _changes(report), _metrics(report)
    onboarding = {"format": FORMAT, "run_id": report.get("run_id"), "map_revision": report["map_revision"],
                  "report_version": report["report_version"], "process_context": report.get("process_context"),
                  "operations": [{"operation_id": node["id"], "name": node.get("name")}
                                 for node in model.get("nodes") or [] if node.get("kind") == "operation"],
                  "events": events, "facts": report.get("facts") or [],
                  "coverage": report.get("coverage"), "changes": report.get("instrumentation") or [],
                  "metrics": report.get("metrics") or [], "requested_facts": report.get("requested_facts") or [],
                  "stages": [{key: row[key] for key in ("id", "execution_status", "result_status", "reasons")}
                             for row in report["stages"]]}
    files = {
        "process": ("process.md", _markdown(model, report)),
        "onboarding": ("onboarding.json", json.dumps(onboarding, ensure_ascii=False, indent=2)),
        "events": ("events.csv", _csv(list(events[0]) if events else ["binding_id"], events)),
        "changes": ("changes.csv", _csv(list(changes[0]) if changes else ["change_id"], changes)),
        "metrics": ("metrics.csv", _csv(list(metrics[0]) if metrics else ["metric_id"], metrics)),
    }
    checksums = [{"name": name, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                  "bytes": len(content.encode("utf-8"))} for name, content in
                 [*files.values(), *((f"../{name}", content) for name, content in published.items())]]
    manifest = {"format": FORMAT, "generated_at": datetime.now(timezone.utc).isoformat(),
                "run_id": report.get("run_id"), "map_revision": report["map_revision"],
                "report_version": report["report_version"], "files": checksums,
                "stages": {row["id"]: row["execution_status"] for row in report["stages"]},
                "trace": trace,
                "limitations": ["Все факты, доработки и определения — предложения; методика не согласована.",
                                "Значения метрик не вычислялись.",
                                "Исходный код, сырые записи логов и скрытые рассуждения модели в комплект не входят."]}
    files["manifest"] = ("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return files
