"""Mutable, evidence-backed map of one scenario across several source systems.

The model proposes business meaning; this module checks shape, references and
literal provenance. These checks do not prove semantic correctness or that all
bank systems were supplied. Only lines actually returned by read_file may be
cited. Mutations are transactional, including edits of already linked objects.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
import hashlib
import json
from typing import Any

from pko.extractors.base import is_vendor
from pko.code_tools.tools import MAX_READ_LINES, _is_blocked, _mask_secrets

SCHEMA_VERSION = "1.1"
STEP_KINDS = ("start", "action", "decision", "fork", "join", "wait", "end", "external")
EDGE_KINDS = ("sequence", "branch", "call", "event", "return", "retry", "error", "data")
EVIDENCE_STATUSES = ("observed", "inferred", "unknown")
MAX_SYSTEMS, MAX_NODES, MAX_EDGES, MAX_GAPS = 100, 500, 1500, 500
MAX_EVIDENCE = 12
MAX_TEXT = 6000
OPERATION_KINDS = ("action", "validation", "read", "write", "integration", "publish", "wait", "calculation", "transform", "error_handling")
OPERATION_NODE_KINDS = ("action", "decision", "wait", "external")
EXECUTION_CHECKS = ("branches", "failures", "integrations", "state_changes", "computations")
INSPECT_SECTIONS = ("summary", "scenario", "systems", "nodes", "edges", "gaps", "sources", "source_reviews", "execution_reviews", "operations", "read_coverage", "validation")
LIMITATIONS = [
    "Карта ограничена выбранным вариантом сценария и переданными материалами.",
    "Статический анализ и проверка цитат не доказывают исполнение в рабочей среде, полноту анализа или правильность бизнес-интерпретации.",
    "Проверка детализации подтверждает покрытие составленного перечня операций, а не полноту обнаружения всех операций в коде.",
]


class MapValidationError(ValueError):
    """An actionable rejection of a proposed map mutation."""


def _text(value: Any, field: str, *, required: bool = False, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise MapValidationError(f"{field}: ожидается строка")
    value = value.strip()
    if required and not value:
        raise MapValidationError(f"{field}: укажите содержательное значение")
    if len(value) > limit:
        raise MapValidationError(f"{field}: максимум {limit} символов")
    return value


def _strings(value: Any, field: str, *, limit: int = 100) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise MapValidationError(f"{field}: ожидается список не длиннее {limit}")
    result = [_text(item, field, required=True) for item in value]
    if len(result) != len(set(result)):
        raise MapValidationError(f"{field}: повторяющиеся значения")
    return result


def _choice(value: Any, field: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise MapValidationError(f"{field}: допустимы {', '.join(choices)}")
    return value


class MapState:
    """Agent-facing state; JSON is deliberately independent from ProgressModel."""

    def __init__(self, workspace: Any, scenario_description: str):
        self.workspace = workspace
        self._files = set(workspace.tree.files)
        self._reads: dict[str, dict[int, str]] = {}
        self._counters = {"sys": 0, "step": 0, "edge": 0, "gap": 0, "review": 0, "op": 0}
        sources = [asdict(s) if is_dataclass(s) else deepcopy(s) for s in workspace.sources]
        source_ids = [s.get("id") for s in sources]
        if not source_ids or any(not isinstance(s, str) or not s for s in source_ids):
            raise MapValidationError("Источники должны иметь непустые строковые id")
        if len(set(source_ids)) != len(source_ids):
            raise MapValidationError("Идентификаторы источников повторяются")
        self._source_ids = set(source_ids)
        self.model: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "scenario": {"description": _text(scenario_description, "scenario_description", required=True, limit=20000),
                         "title": "", "start": "", "end": "", "exclusions": [], "assumptions": []},
            "sources": sources, "systems": [], "nodes": [], "edges": [], "gaps": [],
            "source_reviews": [], "execution_reviews": [],
            "run": {"status": "running", "reason": "", "steps": 0},
            "summary": "", "limitations": list(LIMITATIONS),
            "source_notes": deepcopy(getattr(workspace, "notes", [])),
            "source_omissions": deepcopy(getattr(workspace, "omissions", [])),
        }

    def observe_read(self, path: str, offset: int = 1, limit: int = MAX_READ_LINES) -> None:
        """Register exactly the masked read_file window, after a successful read.

        search/show_facts do not register evidence: the investigator must inspect
        the actual code before citing it. The snapshot stores coverage, not the
        whole source body. Blocked files never become accepted citation sources.
        """
        if path not in self._files or _is_blocked(path) or is_vendor(path):
            return
        content = self.workspace.tree.read(path)
        if content is None:
            return
        offset = max(1, int(offset))
        limit = max(1, min(int(limit), MAX_READ_LINES))
        lines = content.splitlines()
        visible = self._reads.setdefault(path, {})
        for number in range(offset, min(offset + limit, len(lines) + 1)):
            visible[number] = _mask_secrets(lines[number - 1])

    def snapshot(self) -> dict[str, Any]:
        result = deepcopy(self.model)
        result["read_coverage"] = [
            {"path": path, "source_id": self._source_for(path), "line_count": len(lines),
             "ranges": self._ranges(sorted(lines))}
            for path, lines in sorted(self._reads.items()) if lines
        ]
        result["validation"] = {"issues": self._finish_errors(), "semantic_verification": False}
        result["detail_review"] = self._detail_summary()
        return result

    @staticmethod
    def _ranges(numbers: list[int]) -> list[list[int]]:
        ranges: list[list[int]] = []
        for number in numbers:
            if ranges and ranges[-1][1] + 1 == number:
                ranges[-1][1] = number
            else:
                ranges.append([number, number])
        return ranges

    def _source_for(self, path: str) -> str:
        source = path.split("/", 1)[0]
        if source not in self._source_ids:
            raise MapValidationError(f"Путь не относится к известному источнику: {path}")
        return source

    def _evidence(self, value: Any, field: str = "evidence") -> list[dict[str, Any]]:
        if isinstance(value, dict):
            value = [value]
        if value is None:
            value = []
        if not isinstance(value, list) or len(value) > MAX_EVIDENCE:
            raise MapValidationError(f"{field}: ожидается до {MAX_EVIDENCE} цитат")
        accepted = []
        for item in value:
            if not isinstance(item, dict):
                raise MapValidationError(f"{field}: цитата должна быть объектом")
            if set(item) - {"path", "line_start", "line_end", "quote"}:
                raise MapValidationError(f"{field}: неизвестные поля цитаты")
            path = _text(item.get("path"), f"{field}.path", required=True)
            if path not in self._files or _is_blocked(path) or is_vendor(path):
                raise MapValidationError(f"{field}: недоступный путь {path}")
            self._source_for(path)
            start, end = item.get("line_start"), item.get("line_end")
            if type(start) is not int or type(end) is not int or not 1 <= start <= end or end - start >= MAX_READ_LINES:
                raise MapValidationError(f"{field}: некорректный диапазон строк")
            visible = self._reads.get(path, {})
            if any(line not in visible for line in range(start, end + 1)):
                raise MapValidationError(f"{field}: сначала прочитайте {path}:{start}-{end} через read_file")
            quote = _text(item.get("quote"), f"{field}.quote", required=True, limit=24000)
            literal = "\n".join(visible[line] for line in range(start, end + 1)).strip()
            if quote != literal:
                raise MapValidationError(f"{field}: цитата не совпадает с выданными маскированными строками {path}:{start}-{end}")
            normalized = {"path": path, "line_start": start, "line_end": end, "quote": quote}
            if normalized not in accepted:
                accepted.append(normalized)
        return accepted

    def _support(self, args: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        status = _choice(args.get("evidence_status", "unknown"), "evidence_status", EVIDENCE_STATUSES)
        citations = self._evidence(args.get("evidence", []))
        if status != "unknown" and not citations:
            raise MapValidationError(f"{status} требует буквальные основания в evidence")
        return status, citations

    def _id(self, collection: str, prefix: str, value: Any, maximum: int) -> str:
        if value is not None and value != "":
            value = _text(value, "id", required=True)
            if not any(item["id"] == value for item in self.model[collection]):
                raise MapValidationError(f"id {value} не существует; для нового объекта не передавайте id")
            return value
        if len(self.model[collection]) >= maximum:
            raise MapValidationError(f"Достигнут лимит {collection}: {maximum}; сохраните частичный результат и укажите ограничение, не скрывая самостоятельные операции")
        self._counters[prefix] += 1
        return f"{prefix}-{self._counters[prefix]}"

    def _put(self, collection: str, item: dict[str, Any]) -> dict[str, Any]:
        rows = self.model[collection]
        for index, old in enumerate(rows):
            if old["id"] == item["id"]:
                rows[index] = item
                break
        else:
            rows.append(item)
        return {"id": item["id"]}

    def dispatch(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(args, (dict, type(None))):
            return {"ok": False, "error": "Аргументы должны быть объектом JSON"}
        args = args or {}
        allowed = {
            "set_scope": {"title", "start", "end", "exclusions", "assumptions"},
            "upsert_system": {"id", "name", "source_ids", "description", "evidence_status", "evidence"},
            "upsert_step": {"id", "name", "kind", "system_id", "description", "input", "output", "condition", "evidence_status", "evidence", "parent_id"},
            "upsert_transition": {"id", "source", "target", "kind", "description", "condition", "data", "correlation", "evidence_status", "evidence", "sender_evidence", "receiver_evidence", "channel", "contract"},
            "add_gap": {"description", "node_ids", "system_ids"},
            "review_source": {"source_id", "disposition", "reason"},
            "review_execution": {"id", "name", "system_id", "evidence", "operations", "checks"},
            "remove_element": {"kind", "id"}, "inspect_map": {"section", "offset", "limit"}, "finish": {"summary"},
        }
        if name not in allowed:
            return {"ok": False, "error": f"Неизвестный инструмент карты: {name}"}
        unexpected = set(args) - allowed[name]
        if unexpected:
            return {"ok": False, "error": f"Неизвестные аргументы: {', '.join(sorted(unexpected))}"}
        if name == "inspect_map":
            try:
                return {"ok": True, **self._inspect_map(args)}
            except (MapValidationError, TypeError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
        original, counters = deepcopy(self.model), self._counters.copy()
        try:
            result = getattr(self, f"_{name}")(args)
            errors = self._reference_errors()
            if errors:
                raise MapValidationError("; ".join(errors))
            if name != "finish":
                self.model["run"].update(status="running", reason="")
            return {"ok": True, **result}
        except (MapValidationError, TypeError, KeyError, ValueError) as exc:
            self.model = original
            self._counters = counters
            return {"ok": False, "error": str(exc)}

    def _inspect_map(self, args: dict[str, Any]) -> dict[str, Any]:
        section = _choice(args.get("section", "summary"), "section", INSPECT_SECTIONS)
        offset, limit = args.get("offset", 1), args.get("limit", 10)
        if type(offset) is not int or type(limit) is not int or offset < 1 or not 1 <= limit <= 30:
            raise MapValidationError("inspect_map: offset >= 1, limit от 1 до 30")

        def compact(value: Any, field: str = "") -> Any:
            if isinstance(value, str):
                return value if len(value) <= 800 else value[:800] + "… [поле сокращено в выдаче]"
            if isinstance(value, list):
                # The complete model stays in the artifact. Runtime inspection
                # is navigable and cannot expand one row into an unbounded turn.
                tail = [f"[ещё элементов: {len(value) - 30}]"] if len(value) > 30 else []
                return [compact(v, field) for v in value[:30]] + tail
            if isinstance(value, dict):
                return {k: compact(v, k) for k, v in value.items() if k != "quote"}
            return value

        counts = {key: len(self.model[key]) for key in ("systems", "nodes", "edges", "gaps", "sources", "source_reviews", "execution_reviews")}
        counts["operations"] = sum(len(review["operations"]) for review in self.model["execution_reviews"])
        if section == "summary":
            return {"section": section, "model": compact({
                "scenario": self.model["scenario"], "run": self.model["run"], "summary": self.model["summary"],
                "counts": counts, "validation": {"issues": self._finish_errors()},
                "limitations": self.model["limitations"], "detail_review": self._detail_summary(),
            }), "sections": list(INSPECT_SECTIONS), "inspection_note": "Полная карта сохранена; выберите section и offset для продолжения."}
        if section == "scenario":
            return {"section": section, "model": compact(self.model["scenario"])}
        if section == "read_coverage":
            rows = self.snapshot()[section]
        elif section == "validation":
            rows = self._finish_errors()
        elif section == "operations":
            rows = [{**operation, "review_id": review["id"], "system_id": review["system_id"]}
                    for review in self.model["execution_reviews"] for operation in review["operations"]]
        else:
            rows = self.model[section]
        page, size = [], 0
        for row in rows[offset - 1:offset - 1 + limit]:
            item = compact(row)
            length = len(json.dumps(item, ensure_ascii=False))
            if page and size + length > 18000:
                break
            page.append(item)
            size += length
        next_offset = offset + len(page)
        return {"section": section, "items": page, "offset": offset, "total": len(rows),
                "next_offset": next_offset if next_offset <= len(rows) else None,
                "inspection_note": "Цитаты представлены ссылками, длинные поля сокращены; полный текст хранится в результате. Для проверки цитат используйте read_file."}

    def _set_scope(self, args: dict[str, Any]) -> dict[str, Any]:
        self.model["scenario"].update({
            "title": _text(args.get("title"), "title", required=True, limit=300),
            "start": _text(args.get("start"), "start", required=True),
            "end": _text(args.get("end"), "end", required=True),
            "exclusions": _strings(args.get("exclusions", []), "exclusions"),
            "assumptions": _strings(args.get("assumptions", []), "assumptions"),
        })
        return {"scenario": deepcopy(self.model["scenario"])}

    def _upsert_system(self, args: dict[str, Any]) -> dict[str, Any]:
        status, evidence = self._support(args)
        sources = _strings(args.get("source_ids", []), "source_ids")
        if set(sources) - self._source_ids:
            raise MapValidationError("source_ids содержит неизвестный источник")
        if status != "unknown" and not sources:
            raise MapValidationError("Подтверждённая или выведенная из кода АС требует source_ids")
        item = {"id": self._id("systems", "sys", args.get("id"), MAX_SYSTEMS),
                "name": _text(args.get("name"), "name", required=True, limit=300),
                "source_ids": sources, "description": _text(args.get("description", ""), "description"),
                "evidence_status": status, "evidence": evidence}
        if any(s["name"].casefold() == item["name"].casefold() and s["id"] != item["id"] for s in self.model["systems"]):
            raise MapValidationError("АС с таким именем уже есть: обновите существующий id")
        return self._put("systems", item)

    def _upsert_step(self, args: dict[str, Any]) -> dict[str, Any]:
        status, evidence = self._support(args)
        item = {"id": self._id("nodes", "step", args.get("id"), MAX_NODES),
                "name": _text(args.get("name"), "name", required=True, limit=300),
                "kind": _choice(args.get("kind", "action"), "kind", STEP_KINDS),
                "system_id": _text(args.get("system_id", "unknown"), "system_id", required=True),
                "evidence_status": status, "evidence": evidence,
                "parent_id": args.get("parent_id") or None}
        for field in ("description", "input", "output", "condition"):
            item[field] = _text(args.get(field, ""), field)
        return self._put("nodes", item)

    def _upsert_transition(self, args: dict[str, Any]) -> dict[str, Any]:
        status, evidence = self._support(args)
        item = {"id": self._id("edges", "edge", args.get("id"), MAX_EDGES),
                "source": _text(args.get("source"), "source", required=True),
                "target": _text(args.get("target"), "target", required=True),
                "kind": _choice(args.get("kind", "sequence"), "kind", EDGE_KINDS),
                "evidence_status": status, "evidence": evidence,
                "sender_evidence": self._evidence(args.get("sender_evidence", []), "sender_evidence"),
                "receiver_evidence": self._evidence(args.get("receiver_evidence", []), "receiver_evidence")}
        for field in ("description", "condition", "data", "correlation", "channel", "contract"):
            item[field] = _text(args.get(field, ""), field)
        if item["kind"] == "branch" and not item["condition"]:
            raise MapValidationError("Ветвление требует условия в condition")
        signature = (item["source"], item["target"], item["kind"], item["condition"])
        if any((e["source"], e["target"], e["kind"], e["condition"]) == signature and e["id"] != item["id"] for e in self.model["edges"]):
            raise MapValidationError("Такой переход уже есть: обновите существующий id")
        return self._put("edges", item)

    def _add_gap(self, args: dict[str, Any]) -> dict[str, Any]:
        description = _text(args.get("description"), "description", required=True)
        nodes = _strings(args.get("node_ids", []), "node_ids", limit=MAX_NODES)
        systems = _strings(args.get("system_ids", []), "system_ids", limit=MAX_SYSTEMS)
        for old in self.model["gaps"]:
            if (old["description"] == description and set(old["node_ids"]) == set(nodes)
                    and set(old.get("system_ids", [])) == set(systems)):
                return {"id": old["id"]}
        return self._put("gaps", {"id": self._id("gaps", "gap", None, MAX_GAPS),
                                  "description": description, "node_ids": nodes, "system_ids": systems})

    def _review_source(self, args: dict[str, Any]) -> dict[str, Any]:
        source = _text(args.get("source_id"), "source_id", required=True)
        if source not in self._source_ids:
            raise MapValidationError("Неизвестный source_id")
        disposition = _choice(args.get("disposition"), "disposition", ("reviewed", "out_of_scope", "unavailable"))
        reason = _text(args.get("reason"), "reason", required=True)
        read_files = sum(bool(rows) for path, rows in self._reads.items() if path.startswith(source + "/"))
        if disposition == "reviewed" and not read_files:
            raise MapValidationError("Нельзя отметить источник изученным до чтения хотя бы одного файла")
        # A short name/description is enough to propose exclusion, but not to
        # present it as examined. Even one read is only a minimum inspection,
        # never semantic proof that the source is irrelevant to this scenario.
        review = {"source_id": source, "disposition": disposition, "reason": reason,
                  "inspected": bool(read_files), "read_files": read_files}
        self.model["source_reviews"] = [r for r in self.model["source_reviews"] if r["source_id"] != source] + [review]
        return review

    def _remove_element(self, args: dict[str, Any]) -> dict[str, Any]:
        kind = _choice(args.get("kind"), "kind", ("system", "step", "transition", "gap", "execution_review"))
        collection = {"system": "systems", "step": "nodes", "transition": "edges", "gap": "gaps", "execution_review": "execution_reviews"}[kind]
        identifier = _text(args.get("id"), "id", required=True)
        if not any(row["id"] == identifier for row in self.model[collection]):
            raise MapValidationError(f"Объект {identifier} не найден")
        # Never silently delete incident transitions or children. The caller can
        # amend them first, and failed removals leave all data untouched.
        self.model[collection] = [row for row in self.model[collection] if row["id"] != identifier]
        return {"removed": identifier}

    @staticmethod
    def _fingerprint(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _within(citations: list[dict[str, Any]], scope: list[dict[str, Any]]) -> bool:
        """Every cited line belongs to the explicitly inspected execution area."""
        return all(any(ref["path"] == area["path"] and area["line_start"] <= line <= area["line_end"]
                       for area in scope)
                   for ref in citations for line in range(ref["line_start"], ref["line_end"] + 1))

    def _review_execution(self, args: dict[str, Any]) -> dict[str, Any]:
        """Record a code-anchored inventory, not an assertion of total coverage.

        Independent effects require independent node IDs. A review may span
        helper functions: functions are not automatically separate operations.
        No natural-language heuristic pretends to prove that an action is atomic.
        """
        identifier = self._id("execution_reviews", "review", args.get("id"), MAX_NODES)
        system_id = _text(args.get("system_id"), "system_id", required=True)
        systems = {row["id"]: row for row in self.model["systems"]}
        if system_id not in systems:
            raise MapValidationError("review_execution: нужна существующая АС; неизвестную реализацию сохраняйте в gap")
        evidence = self._evidence(args.get("evidence", []))
        if not evidence:
            raise MapValidationError("review_execution требует прочитанные основания обследованного исполнения")
        if any(self._source_for(ref["path"]) not in systems[system_id]["source_ids"] for ref in evidence):
            raise MapValidationError("review_execution: область обследования относится к другой АС")
        raw_operations = args.get("operations")
        if not isinstance(raw_operations, list) or not 1 <= len(raw_operations) <= 100:
            raise MapValidationError("operations: укажите от 1 до 100 атомарных операций; большой обзор разделите на несколько исполнителей")
        nodes = {node["id"]: node for node in self.model["nodes"]}
        old = next((r for r in self.model["execution_reviews"] if r["id"] == identifier), {})
        old_ids = {op["node_id"]: op["id"] for op in old.get("operations", [])}
        used_nodes: set[str] = set()
        operations = []
        for raw in raw_operations:
            if not isinstance(raw, dict) or set(raw) - {"node_id", "name", "kind", "action", "effect", "failure", "condition", "evidence"}:
                raise MapValidationError("operations: неверная структура операции")
            node_id = _text(raw.get("node_id"), "operations.node_id", required=True)
            if node_id not in nodes or nodes[node_id]["system_id"] != system_id:
                raise MapValidationError(f"{node_id}: операция должна ссылаться на существующий шаг этой АС")
            if node_id in used_nodes:
                raise MapValidationError(f"{node_id}: несколько операций нельзя свернуть в один крупный шаг; создайте отдельные узлы")
            used_nodes.add(node_id)
            citations = self._evidence(raw.get("evidence", []), "operations.evidence")
            if not citations or not self._within(citations, evidence):
                raise MapValidationError(f"{node_id}: основания операции должны входить в прочитанную область обзора")
            if not self._within(nodes[node_id]["evidence"], evidence):
                raise MapValidationError(f"{node_id}: обзор не покрывает все основания шага; изучите исполнение целиком")
            if node_id not in old_ids:
                self._counters["op"] += 1
            operation = {"id": old_ids.get(node_id, f"op-{self._counters['op']}"), "node_id": node_id,
                         "kind": _choice(raw.get("kind"), "operations.kind", OPERATION_KINDS),
                         "evidence": citations}
            for field in ("name", "action", "effect", "failure"):
                operation[field] = _text(raw.get(field), f"operations.{field}", required=True,
                                        limit=300 if field == "name" else MAX_TEXT)
            operation["condition"] = _text(raw.get("condition", ""), "operations.condition")
            operations.append(operation)
        raw_checks = args.get("checks")
        if not isinstance(raw_checks, dict) or set(raw_checks) != set(EXECUTION_CHECKS):
            raise MapValidationError("checks должен содержать branches, failures, integrations, state_changes, computations")
        checks = {}
        for category, raw in raw_checks.items():
            if not isinstance(raw, dict) or set(raw) - {"status", "explanation", "evidence", "node_ids", "edge_ids", "gap_ids"}:
                raise MapValidationError(f"checks.{category}: неверная структура")
            status = _choice(raw.get("status"), f"checks.{category}.status", ("present", "not_found", "unknown"))
            citations = self._evidence(raw.get("evidence", []), f"checks.{category}.evidence")
            if not self._within(citations, evidence):
                raise MapValidationError(f"checks.{category}: цитата вне области обзора")
            check = {"status": status, "explanation": _text(raw.get("explanation"), f"checks.{category}.explanation", required=True),
                     "evidence": citations}
            for field in ("node_ids", "edge_ids", "gap_ids"):
                check[field] = _strings(raw.get(field, []), f"checks.{category}.{field}", limit=MAX_NODES)
            if status != "unknown" and not citations:
                raise MapValidationError(f"checks.{category}: вывод требует цитат обследованного участка")
            if status == "present" and not (check["node_ids"] or check["edge_ids"]):
                raise MapValidationError(f"checks.{category}: обнаруженное поведение должно быть показано узлами или переходами")
            if status == "not_found" and (check["node_ids"] or check["edge_ids"]):
                raise MapValidationError(f"checks.{category}: not_found не может одновременно ссылаться на найденное поведение")
            if status == "unknown" and not check["gap_ids"]:
                raise MapValidationError(f"checks.{category}: неизвестное требует gap_ids")
            checks[category] = check
        related_nodes = used_nodes | {n for check in checks.values() for n in check["node_ids"]}
        edge_index = {edge["id"]: edge for edge in self.model["edges"]}
        # Sequence/data edges need not appear in category checks, but changing
        # their order or endpoints still invalidates the execution review.
        related_edges = {edge["id"] for edge in edge_index.values()
                         if edge["source"] in used_nodes or edge["target"] in used_nodes}
        item = {"id": identifier, "name": _text(args.get("name"), "name", required=True, limit=300),
                "system_id": system_id, "evidence": evidence, "operations": operations, "checks": checks,
                "node_fingerprints": {n: self._fingerprint(nodes[n]) for n in related_nodes if n in nodes},
                "edge_fingerprints": {e: self._fingerprint(edge_index[e]) for e in related_edges if e in edge_index}}
        result = self._put("execution_reviews", item)
        return {**result, "operations": [{"id": op["id"], "node_id": op["node_id"]} for op in operations]}

    def _reference_errors(self) -> list[str]:
        errors: list[str] = []
        systems = {item["id"]: item for item in self.model["systems"]}
        nodes = {item["id"]: item for item in self.model["nodes"]}

        def check_sources(citations: list[dict[str, Any]], sources: list[str], label: str) -> None:
            if any(self._source_for(c["path"]) not in sources for c in citations):
                errors.append(f"{label}: цитата относится к другому источнику/АС")

        for system in systems.values():
            check_sources(system["evidence"], system["source_ids"], system["id"])
        for node in nodes.values():
            system = node["system_id"]
            if system not in systems and system != "unknown":
                errors.append(f"{node['id']}: неизвестная АС {system}")
            elif system in systems:
                check_sources(node["evidence"], systems[system]["source_ids"], node["id"])
            parent = node.get("parent_id")
            seen = {node["id"]}
            while parent:
                if not isinstance(parent, str) or parent not in nodes:
                    errors.append(f"{node['id']}: неизвестный parent_id")
                    break
                if parent in seen:
                    errors.append(f"{node['id']}: цикл группировки parent_id")
                    break
                seen.add(parent)
                parent = nodes[parent].get("parent_id")
        for edge in self.model["edges"]:
            if edge["source"] not in nodes or edge["target"] not in nodes:
                errors.append(f"{edge['id']}: переход ссылается на отсутствующий шаг")
                continue
            sender, receiver = nodes[edge["source"]], nodes[edge["target"]]
            left, right = sender["system_id"], receiver["system_id"]
            if left in systems:
                check_sources(edge["sender_evidence"], systems[left]["source_ids"], f"{edge['id']}.sender_evidence")
            if right in systems:
                check_sources(edge["receiver_evidence"], systems[right]["source_ids"], f"{edge['id']}.receiver_evidence")
            if left in systems and right in systems:
                check_sources(edge["evidence"], systems[left]["source_ids"] + systems[right]["source_ids"], edge["id"])
            # Switching the edge label must not bypass the handoff evidence
            # gate: even sequence/data/return claims a relation between systems.
            if edge["evidence_status"] == "observed" and left != right:
                if left not in systems or right not in systems:
                    errors.append(f"{edge['id']}: для observed межсистемного перехода обе АС должны быть определены")
                if not edge["sender_evidence"] or not edge["receiver_evidence"]:
                    errors.append(f"{edge['id']}: observed межсистемный переход требует sender_evidence и receiver_evidence")
                for field in ("channel", "contract"):
                    if len(edge[field]) < 5 or edge[field].lower() in ("unknown", "неизвестно", "нет данных", "same name"):
                        errors.append(f"{edge['id']}: опишите {field}; совпадение имён не подтверждает связь")
        for gap in self.model["gaps"]:
            if set(gap["node_ids"]) - set(nodes):
                errors.append(f"{gap['id']}: пробел ссылается на отсутствующий шаг")
            if set(gap.get("system_ids", [])) - set(systems):
                errors.append(f"{gap['id']}: пробел ссылается на отсутствующую АС")
        edges = {edge["id"]: edge for edge in self.model["edges"]}
        gaps = {gap["id"]: gap for gap in self.model["gaps"]}
        assigned_operations: dict[str, str] = {}
        for review in self.model.get("execution_reviews", []):
            system_id = review["system_id"]
            if system_id not in systems:
                errors.append(f"{review['id']}: обзор ссылается на отсутствующую АС")
                continue
            check_sources(review["evidence"], systems[system_id]["source_ids"], review["id"])
            reviewed_nodes = {op["node_id"] for op in review["operations"]}
            for operation in review["operations"]:
                node_id = operation["node_id"]
                if node_id not in nodes or nodes[node_id]["system_id"] != system_id:
                    errors.append(f"{operation['id']}: операция ссылается на отсутствующий шаг или другую АС")
                if node_id in assigned_operations:
                    errors.append(f"{node_id}: несколько операций ({assigned_operations[node_id]}, {operation['id']}) не могут быть скрыты в одном узле")
                assigned_operations[node_id] = operation["id"]
            for category, check in review["checks"].items():
                if set(check["node_ids"]) - set(nodes) or set(check["edge_ids"]) - set(edges) or set(check["gap_ids"]) - set(gaps):
                    errors.append(f"{review['id']}.{category}: неизвестные ссылки узлов, переходов или пробелов")
                    continue
                if any(nodes[n]["system_id"] != system_id for n in check["node_ids"]):
                    errors.append(f"{review['id']}.{category}: проверка относится к узлу другой АС")
                if any(edges[e]["source"] not in reviewed_nodes and edges[e]["target"] not in reviewed_nodes for e in check["edge_ids"]):
                    errors.append(f"{review['id']}.{category}: переход не относится к обследованным операциям")
                if check["status"] == "unknown" and not any(
                    reviewed_nodes.intersection(gaps[g]["node_ids"]) or system_id in gaps[g].get("system_ids", [])
                    for g in check["gap_ids"]
                ):
                    errors.append(f"{review['id']}.{category}: gap должен относиться к обследованной АС или её операциям")
        used_sources = {self._source_for(c["path"]) for collection in ("systems", "nodes", "edges")
                        for row in self.model[collection] for field in ("evidence", "sender_evidence", "receiver_evidence")
                        for c in row.get(field, [])}
        used_sources.update(self._source_for(c["path"]) for review in self.model.get("execution_reviews", []) for c in review["evidence"])
        for review in self.model["source_reviews"]:
            if review["disposition"] != "reviewed" and review["source_id"] in used_sources:
                errors.append(f"{review['source_id']}: источник используется в карте; нельзя пометить его вне сценария или недоступным")
        return errors

    def _detail_errors(self) -> list[str]:
        """Coverage of an explicit inventory, never proof of discovery recall."""
        errors: list[str] = []
        reviews = self.model.get("execution_reviews", [])
        nodes = {node["id"]: node for node in self.model["nodes"]}
        edges = {edge["id"]: edge for edge in self.model["edges"]}
        operations = [op for review in reviews for op in review["operations"]]
        mapped = {op["node_id"] for op in operations}
        required = {n["id"] for n in nodes.values()
                    if n["kind"] in OPERATION_NODE_KINDS and n["evidence_status"] != "unknown"}
        unknown_operations = {n["id"] for n in nodes.values() if n["kind"] in OPERATION_NODE_KINDS and n["evidence_status"] == "unknown"}
        if (required and (not reviews or not any(op["node_id"] in required for op in operations))) or (not required and not unknown_operations):
            errors.append("Детализация не проверена: вызовите review_execution с атомарными операциями; служебные start/end/fork/join не заменяют исполняемые действия")
        for identifier in sorted(required - mapped):
            errors.append(f"{identifier}: шаг не сопоставлен с атомарной операцией в review_execution")
        for review in reviews:
            review_nodes = {op["node_id"] for op in review["operations"]}
            kinds = {op["kind"] for op in review["operations"]}
            incident = [edge for edge in edges.values() if edge["source"] in review_nodes or edge["target"] in review_nodes]
            incident_ids = {edge["id"] for edge in incident}
            reviewed_edge_ids = set(review["edge_fingerprints"])
            for identifier in sorted(incident_ids - reviewed_edge_ids):
                errors.append(f"{review['id']}: переход {identifier} добавлен после обзора; обновите review_execution")
            for identifier in sorted(reviewed_edge_ids - incident_ids):
                errors.append(f"{review['id']}: переход {identifier} удалён или перенесён после обзора; обновите review_execution")
            for identifier, fingerprint in review["node_fingerprints"].items():
                if identifier in nodes and self._fingerprint(nodes[identifier]) != fingerprint:
                    errors.append(f"{review['id']}: шаг {identifier} изменён после обзора; обновите review_execution")
            for identifier, fingerprint in review["edge_fingerprints"].items():
                if identifier in edges and self._fingerprint(edges[identifier]) != fingerprint:
                    errors.append(f"{review['id']}: переход {identifier} изменён после обзора; обновите review_execution")
            for operation in review["operations"]:
                node = nodes.get(operation["node_id"])
                if node and not self._within(node["evidence"], review["evidence"]):
                    errors.append(f"{operation['id']}: основания узла выходят за обследованную область")
            present = {
                "branches": (any(nodes[n]["kind"] in ("decision", "fork", "join") for n in review_nodes if n in nodes)
                             or any(e["kind"] == "branch" for e in incident)),
                "failures": "error_handling" in kinds or any(e["kind"] in ("error", "retry") for e in incident),
                "integrations": bool(kinds.intersection({"integration", "publish"})) or any(
                    e["kind"] in ("call", "event", "return") or
                    (e["source"] in nodes and e["target"] in nodes and nodes[e["source"]]["system_id"] != nodes[e["target"]]["system_id"])
                    for e in incident),
                "state_changes": "write" in kinds,
                "computations": bool(kinds.intersection({"calculation", "transform"})),
            }
            for category, found in present.items():
                if found and review["checks"][category]["status"] == "not_found":
                    errors.append(f"{review['id']}.{category}: карта/реестр уже содержит это поведение; not_found противоречит результату")
        return errors

    def _detail_summary(self) -> dict[str, Any]:
        if "execution_reviews" not in self.model:
            return {"status": "legacy_unverified", "semantic_completeness_verified": False}
        required = {node["id"] for node in self.model["nodes"]
                    if node["kind"] in OPERATION_NODE_KINDS and node["evidence_status"] != "unknown"}
        reviews = self.model["execution_reviews"]
        operations = [operation for review in reviews for operation in review["operations"]]
        mapped = {operation["node_id"] for operation in operations}
        issues = self._detail_errors()
        unknown_nodes = [node["id"] for node in self.model["nodes"]
                         if node["kind"] in OPERATION_NODE_KINDS and node["evidence_status"] == "unknown"]
        unknown = bool(unknown_nodes) or any(check["status"] == "unknown" for review in reviews for check in review["checks"].values())
        return {"contract_version": "1", "status": "pending" if issues else "partial" if unknown else "covered",
                "review_count": len(reviews), "operation_count": len(operations),
                "required_nodes": len(required), "mapped_nodes": len(required.intersection(mapped)),
                "unmapped_node_ids": sorted(required - mapped), "issues": issues,
                "unknown_node_ids": unknown_nodes, "registry_coverage_checked": bool(reviews) and not issues,
                "semantic_completeness_verified": False}

    def _finish_errors(self) -> list[str]:
        errors = self._reference_errors()
        for field in ("title", "start", "end"):
            if not self.model["scenario"][field]:
                errors.append(f"Не определено scenario.{field}: вызовите set_scope")
        reviews = {r["source_id"] for r in self.model["source_reviews"]}
        missing = self._source_ids - reviews
        if missing:
            errors.append("Не рассмотрены источники: " + ", ".join(sorted(missing)))
        nodes = {n["id"]: n for n in self.model["nodes"]}
        if not nodes:
            errors.append("Карта не содержит шагов")
            return errors
        starts = {n["id"] for n in nodes.values() if n["kind"] == "start"}
        ends = {n["id"] for n in nodes.values() if n["kind"] == "end"}
        if not starts:
            errors.append("Нужен хотя бы один шаг kind=start")
        if not ends:
            errors.append("Нужен хотя бы один шаг kind=end")
        outgoing: dict[str, set[str]] = {n: set() for n in nodes}
        incoming: dict[str, set[str]] = {n: set() for n in nodes}
        for edge in self.model["edges"]:
            if edge["kind"] != "data" and edge["source"] in nodes and edge["target"] in nodes:
                outgoing[edge["source"]].add(edge["target"])
                incoming[edge["target"]].add(edge["source"])

        def walk(seeds: set[str], adjacency: dict[str, set[str]]) -> set[str]:
            visited, todo = set(seeds), list(seeds)
            while todo:
                for nxt in adjacency[todo.pop()]:
                    if nxt not in visited:
                        visited.add(nxt)
                        todo.append(nxt)
            return visited

        reachable = walk(starts, outgoing)
        terminates = walk(ends, incoming)
        # Unknown runtime fragments may remain unconnected, but only with an
        # explicit gap tied to the affected nodes; they yield a partial result.
        gap_nodes = {n for gap in self.model["gaps"] for n in gap["node_ids"]}
        gap_systems = {system for gap in self.model["gaps"] for system in gap.get("system_ids", [])}
        # Several independent start-to-end chains are not one connected variant.
        # Preserve unknown islands only as explicitly documented partial findings.
        adjacency = {identifier: outgoing[identifier] | incoming[identifier] for identifier in nodes}
        components: list[set[str]] = []
        covered: set[str] = set()
        for identifier in nodes:
            if identifier not in covered:
                component = walk({identifier}, adjacency)
                components.append(component)
                covered.update(component)
        if len(components) > 1:
            main_component = max(components, key=len)
            for component in components:
                if component is not main_component and not component.issubset(gap_nodes):
                    errors.append("Несвязанный фрагмент карты (" + ", ".join(sorted(component))
                                  + "): найдите переходы либо добавьте адресный gap ко всем его шагам")
        for node in nodes.values():
            identifier = node["id"]
            if identifier not in reachable or identifier not in terminates:
                if identifier not in gap_nodes:
                    errors.append(f"{identifier}: нет сквозного пути от начала до завершения; свяжите шаг или добавьте адресный gap")
            if node["kind"] == "start" and incoming[identifier]:
                errors.append(f"{identifier}: входящий переход в начальный шаг; выделите повторяемое действие отдельно")
            if node["kind"] == "end" and outgoing[identifier]:
                errors.append(f"{identifier}: исходящий переход из конечного шага")
            if node["kind"] in ("decision", "fork") and len(outgoing[identifier]) < 2 and identifier not in gap_nodes:
                errors.append(f"{identifier}: развилка должна иметь минимум два направления либо адресный gap")
            if node["kind"] == "join" and len(incoming[identifier]) < 2 and identifier not in gap_nodes:
                errors.append(f"{identifier}: join требует минимум два входа либо адресный gap")
            if (node["evidence_status"] == "unknown" or node["system_id"] == "unknown") and identifier not in gap_nodes:
                errors.append(f"{identifier}: неизвестное поведение/АС требует адресный gap")
        for edge in self.model["edges"]:
            if edge["evidence_status"] == "unknown" and not {edge["source"], edge["target"]}.issubset(gap_nodes):
                errors.append(f"{edge['id']}: неизвестный переход требует gap с обоими шагами")
        unknown_systems = {s["id"] for s in self.model["systems"] if s["evidence_status"] == "unknown"}
        for system_id in unknown_systems:
            affected_nodes = {node["id"] for node in nodes.values() if node["system_id"] == system_id}
            if system_id not in gap_systems and (not affected_nodes or not affected_nodes.issubset(gap_nodes)):
                errors.append(f"{system_id}: неподтверждённая АС требует gap с system_ids либо со всеми её шагами")
        errors.extend(self._detail_errors())
        return errors

    def _finish(self, args: dict[str, Any]) -> dict[str, Any]:
        summary = _text(args.get("summary"), "summary", required=True, limit=20000)
        errors = self._finish_errors()
        if errors:
            raise MapValidationError("Исправьте карту перед завершением: " + "; ".join(errors[:30]))
        partial = bool(self.model["gaps"] or self.model["source_omissions"] or self.model["scenario"]["assumptions"]
                       or any(r["disposition"] == "unavailable" for r in self.model["source_reviews"])
                       or any(r["disposition"] == "out_of_scope" and not r.get("inspected", False)
                              for r in self.model["source_reviews"])
                       or any(row["evidence_status"] != "observed" for key in ("systems", "nodes", "edges") for row in self.model[key]))
        status = "partial" if partial else "completed"
        reason = ("Исследование завершено с предположениями, ограничениями или явно обозначенными пробелами."
                  if partial else "Исследование переданных материалов завершено; это не подтверждение полноты реализации во всех АС банка.")
        self.model["summary"] = summary
        self.model["run"].update(status=status, reason=reason)
        return {"status": status, "reason": reason, "limitations": list(self.model["limitations"])}
