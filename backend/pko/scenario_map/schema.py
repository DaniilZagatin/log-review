"""Mutable, evidence-backed map of one scenario as atomic operations.

The model proposes business meaning; this module checks shape, references and
literal provenance. These checks do not prove semantic correctness or that all
relevant source code was supplied. Only lines actually returned by read_file may be
cited. Mutations are transactional, including edits of already linked objects.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
import hashlib
import json
import re
from typing import Any

from pko.extractors.base import is_vendor
from pko.code_tools.tools import MAX_READ_LINES, _is_blocked, _mask_secrets
from pko.audit import sanitize
from pko.scenario_map.research_memory import ResearchMemory
from pko.scenario_map.quality import ResearchQuality, QualityError, EFFECT_KINDS
from pko.scenario_map.code_references import code_only
from pko.scenario_map.execution_contracts import ExecutionContracts, ExecutionContractError

SCHEMA_VERSION = "2.0"
SEMANTIC_PROFILE = "variant-actions-v1"
STEP_KINDS = ("operation",)
OUTCOME_KINDS = ("success", "refusal", "failure", "cancelled", "deferred", "no_decision", "unknown")
EDGE_KINDS = ("sequence", "branch", "call", "event", "return", "retry", "error")
EVIDENCE_STATUSES = ("observed", "inferred", "unknown")
MAX_NODES, MAX_EDGES, MAX_GAPS = 500, 1500, 500
MAX_EVIDENCE = 12
MAX_TEXT = 6000
OPERATION_KINDS = ("action", "validation", "read", "write", "integration", "publish", "wait", "calculation", "transform", "error_handling")
OPERATION_NODE_KINDS = ("operation",)
EXECUTION_CHECKS = ("branches", "failures", "integrations", "state_changes", "computations")
INSPECT_SECTIONS = ("summary", "scenario", "nodes", "edges", "gaps", "sources", "source_reviews", "execution_reviews", "operations", "route_expectations", "read_coverage", "read_fragments", "research_history", "research_notes", "research_brief", "context_summary", "research_tasks", "semantic_review", "frontier", "validation")
LIMITATIONS = [
    "Карта ограничена выбранным вариантом сценария и переданными материалами.",
    "Статический анализ и проверка цитат не доказывают исполнение в рабочей среде, полноту анализа или правильность бизнес-интерпретации.",
    "Проверка детализации подтверждает покрытие составленного перечня операций, а не полноту обнаружения всех операций в коде.",
]


class MapValidationError(ValueError):
    """An actionable rejection of a proposed map mutation.

    code is a stable category for audit and failure statistics; the message
    remains the model-readable remediation.
    """

    def __init__(self, message: str, code: str = "invalid_arguments", **details):
        super().__init__(message)
        self.code = code
        self.details = details


def _text(value: Any, field: str, *, required: bool = False, limit: int | None = MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise MapValidationError(f"{field}: ожидается строка")
    value = value.strip()
    if required and not value:
        raise MapValidationError(f"{field}: укажите содержательное значение")
    if limit is not None and len(value) > limit:
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


class MapState(ExecutionContracts, ResearchQuality):
    """Agent-facing state; JSON is deliberately independent from ProgressModel."""

    def __init__(self, workspace: Any, scenario_description: str):
        self.workspace = workspace
        self._files = set(workspace.tree.files)
        self._reads: dict[str, dict[int, str]] = {}
        self.research_memory = ResearchMemory()
        self._counters = { "group": 0, "step": 0, "edge": 0, "gap": 0, "review": 0, "op": 0, "note": 0, "task": 0}
        sources = [asdict(s) if is_dataclass(s) else deepcopy(s) for s in workspace.sources]
        source_ids = [s.get("id") for s in sources]
        if not source_ids or any(not isinstance(s, str) or not s for s in source_ids):
            raise MapValidationError("Источники должны иметь непустые строковые id")
        if len(set(source_ids)) != len(source_ids):
            raise MapValidationError("Идентификаторы источников повторяются")
        self._source_ids = set(source_ids)
        self.model: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "semantic_profile": SEMANTIC_PROFILE,
            # The process description is bounded by the model window, checked before research.
            "scenario": {"description": _text(scenario_description, "scenario_description", required=True, limit=None),
                         "title": "", "process_name": "", "variant_context": "", "instance_unit": "", "variant_factors": [], "start": "", "end": "", "exclusions": [], "assumptions": [], "entry_operation_ids": [], "terminal_operation_ids": []},
            "sources": sources, "nodes": [], "edges": [], "gaps": [],
            "source_reviews": [], "execution_reviews": [], "research_notes": [], "research_brief": None, "context_summary": None,
            "research_tasks": [], "semantic_review": None,
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
        self.discover_work(path)

    def snapshot(self) -> dict[str, Any]:
        result = deepcopy(self.model)
        result["read_coverage"] = [
            {"path": path, "source_id": self._source_for(path), "line_count": len(lines),
             "ranges": self._ranges(sorted(lines))}
            for path, lines in sorted(self._reads.items()) if lines
        ]
        result["validation"] = self.validation_signals()
        result["research_coverage"] = self.research_coverage()
        result["detail_review"] = self._detail_summary()
        result["research_history"] = self.research_memory.snapshot()
        result["research_history_evicted"] = self.research_memory.evicted_metadata_count
        return result

    def graph_frontier(self) -> dict[str, Any]:
        """Observed construction state, not a guessed execution order."""
        nodes = self.model["nodes"]
        outgoing = {n["id"]: set() for n in nodes}
        incoming = {n["id"]: set() for n in nodes}
        for edge in self.model["edges"]:
            if edge["kind"] == "data":
                continue
            if edge["source"] in outgoing and edge["target"] in incoming:
                outgoing[edge["source"]].add(edge["target"])
                incoming[edge["target"]].add(edge["source"])
        terminals = set(self.model["scenario"]["terminal_operation_ids"])
        gap_nodes = {n for gap in self.model["gaps"] for n in gap["node_ids"]}
        open_exits = []
        for node in nodes:
            identifier = node["id"]
            routes = [e for e in self.model["edges"] if e["source"] == identifier and e["kind"] != "data"]
            conditional = bool(node.get("completion_condition"))
            incomplete = not routes and (identifier not in terminals or (conditional and identifier not in gap_nodes))
            incomplete |= (len(routes) > 1 and node.get("continuation_mode", "sequence") == "sequence")
            incomplete |= bool((len(routes) > 1 or conditional) and any(not e.get("condition", "") for e in routes))
            if node.get("continuation_mode") == "choice":
                incomplete |= len(routes) + bool(conditional and identifier in terminals) < 2 and identifier not in gap_nodes
            if node.get("continuation_mode") == "all":
                incomplete |= len(routes) < 2 and identifier not in gap_nodes
            if incomplete:
                open_exits.append(identifier)
        return {
            "nodes": [{key: deepcopy(n.get(key)) for key in ("id", "name", "kind", "continuation_mode", "join_mode", "completion_condition", "outcome", "effect_kind")} for n in nodes],
            "starts": list(self.model["scenario"]["entry_operation_ids"]),
            "ends": list(terminals),
            "unlinked": [n["id"] for n in nodes if not outgoing[n["id"]] and not incoming[n["id"]]],
            "open_exits": open_exits,
            "pending_gaps": [{"id": g["id"], "node_ids": list(g["node_ids"]), "description": g["description"]}
                             for g in self.model["gaps"]],
            "pending_tasks": [{key: task[key] for key in ('id', 'kind', 'target')}
                              for task in self.model.get('research_tasks', []) if task['status'] in ('open', 'read', 'analyzed')],
        }

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

    def _evidence(self, value: Any, field: str = "evidence", *, limit: int = MAX_EVIDENCE) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            value = [value]
        if value is None:
            value = []
        if not isinstance(value, list) or len(value) > limit:
            raise MapValidationError(f"{field}: ожидается до {limit} цитат")
        accepted = []
        for item in value:
            if not isinstance(item, dict):
                raise MapValidationError(f"{field}: цитата должна быть объектом")
            if set(item) - {"path", "line_start", "line_end", "quote"}:
                raise MapValidationError(f"{field}: неизвестные поля цитаты")
            path = _text(item.get("path"), f"{field}.path", required=True)
            if path not in self._files or _is_blocked(path) or is_vendor(path):
                raise MapValidationError(f"{field}: недоступный путь {path}", "invalid_evidence")
            self._source_for(path)
            start, end = item.get("line_start"), item.get("line_end")
            if type(start) is not int or type(end) is not int or not 1 <= start <= end or end - start >= MAX_READ_LINES:
                raise MapValidationError(f"{field}: некорректный диапазон строк", "invalid_evidence")
            visible = self._reads.get(path, {})
            if any(line not in visible for line in range(start, end + 1)):
                raise MapValidationError(f"{field}: сначала прочитайте {path}:{start}-{end} через read_file", "invalid_evidence")
            literal = "\n".join(visible[line] for line in range(start, end + 1)).strip()
            if not literal or len(literal) > 24000:
                raise MapValidationError(f"{field}: выберите непустой прочитанный диапазон не длиннее 24000 символов")
            # New calls pass a reference only. Resolve the canonical literal
            # from delivered lines, never by reopening the file. Old explicit
            # quotes remain strict: do not silently accept a mismatching quote.
            if "quote" in item:
                quote = _text(item["quote"], f"{field}.quote", required=True, limit=24000)
                if quote != literal:
                    actual_lines, expected_lines = quote.splitlines(), literal.splitlines()
                    difference = next((i for i, (a, b) in enumerate(zip(actual_lines, expected_lines)) if a != b),
                                      min(len(actual_lines), len(expected_lines)))
                    raise MapValidationError(
                        f"{field}: цитата не совпадает с выданными маскированными строками {path}:{start}-{end}; "
                        f"первое несовпадение около строки {start + difference}. "
                        f"В диапазоне {end - start + 1} строк, в переданном тексте {len(actual_lines)}. "
                        "Передайте path, line_start и line_end без quote: точный текст подставит код из прочитанных строк. "
                        "Для уточнения диапазона используйте inspect_map(section='read_fragments', path=..., offset=...).", "invalid_evidence")
            normalized = {"path": path, "line_start": start, "line_end": end, "quote": literal}
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
                raise MapValidationError(f"id {value} не существует; для нового объекта не передавайте id", "unknown_id")
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

    # An edit by id names only the changed fields. The stored object supplies the
    # rest, and the merged object is validated as a whole like a new one.
    EDITABLE = {"upsert_step": "nodes", "upsert_transition": "edges",
                "save_research_task": "research_tasks", "save_research_note": "research_notes"}

    def _with_existing(self, name: str, args: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
        identifier = args.get("id")
        collection = self.EDITABLE.get(name)
        if collection and identifier not in (None, ""):
            old = next((row for row in self.model[collection] if row.get("id") == identifier), None)
            if old is None:
                raise MapValidationError(f"id {identifier} не существует; для нового объекта не передавайте id", "unknown_id")

            def public(value):
                if isinstance(value, dict):
                    return {key: public(item) for key, item in value.items() if key != "quote"}
                if isinstance(value, list):
                    return [public(item) for item in value]
                return value
            return {**{key: public(value) for key, value in old.items() if key in allowed and value is not None}, **args}
        return args

    def dispatch(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(args, (dict, type(None))):
            return {"ok": False, "error": "Аргументы должны быть объектом JSON"}
        args = args or {}
        allowed = {
            "set_scope": {"title", "process_name", "variant_context", "instance_unit", "variant_factors", "start", "end", "exclusions", "assumptions", "entry_operation_ids", "terminal_operation_ids", "amendment"},
            "patch_fragment": {"steps", "transitions", "remove_node_ids", "remove_edge_ids", "entry_operation_ids", "terminal_operation_ids", "gap_updates", "operation_mappings", "route_mappings"},
            "upsert_step": {"id", "name", "kind", "description", "input", "output", "condition", "evidence_status", "evidence", "outcome", "outcome_scope", "basis", "code_explanations", "effect_kind", "continuation_mode", "join_mode", "external_interactions", "completion_condition"},
            "upsert_transition": {"id", "source", "target", "kind", "description", "condition", "data", "correlation", "evidence_status", "evidence", "sender_evidence", "receiver_evidence", "channel", "contract", "basis", "code_explanations"},
            "add_gap": {"description", "node_ids", "edge_ids", "task_ids", "category", "evidence"},
            "review_source": {"source_id", "disposition", "reason"},
            "review_execution": {"id", "name", "evidence", "operations", "checks", "route_expectations", "discard_operations", "discard_route_expectations", "amendment"},
            "save_research_task": {"id", "kind", "target", "status", "resolution", "evidence", "node_ids", "gap_ids", "child_task_ids", "exclusion"},
            "review_semantics": {"checks", "route_cases"},
            "save_research_note": {"id", "kind", "status", "finding", "next_action", "evidence"},
            "save_research_brief": {"project_overview", "scenario_alignment", "confirmed_facts", "flow_hypothesis", "open_questions", "next_actions", "evidence"},
            "remove_element": {"kind", "id"}, "inspect_map": {"section", "offset", "limit", "path"}, "finish": {"summary"},
        }
        if name not in allowed:
            return {"ok": False, "code": "unknown_tool", "error": f"Неизвестный инструмент карты: {name}"}
        unexpected = set(args) - allowed[name]
        if unexpected:
            return {"ok": False, "code": "invalid_arguments", "error": f"Неизвестные аргументы: {', '.join(sorted(unexpected))}"}
        try:
            args = self._with_existing(name, args, allowed[name])
        except MapValidationError as exc:
            return {"ok": False, "code": exc.code, "error": str(exc)}
        if name == "inspect_map":
            try:
                return {"ok": True, **self._inspect_map(args)}
            except (MapValidationError, TypeError, ValueError) as exc:
                return {"ok": False, "code": getattr(exc, "code", "invalid_arguments"), "error": str(exc)}
        original, counters = deepcopy(self.model), self._counters.copy()
        created = name in self.REQUIRED_NEW and args.get("id") in (None, "")
        try:
            result = getattr(self, f"_{name}")(args)
            if created:
                self._require_new_fields(name, result["id"])
            errors = [] if getattr(self, '_patch_in_progress', False) else self._reference_errors()
            if errors:
                raise MapValidationError("; ".join(errors))
            if not getattr(self, '_patch_in_progress', False) and name in ('upsert_step', 'upsert_transition', 'patch_fragment', 'review_execution'):
                previous_issues = {json.dumps(issue, sort_keys=True, ensure_ascii=False)
                                   for review in original['execution_reviews'] for issue in self._projection_issues_for_model(original, review)}
                issues = [issue for review in self.model['execution_reviews'] for issue in self._projection_issues(review)
                          if json.dumps(issue, sort_keys=True, ensure_ascii=False) not in previous_issues]
                if issues:
                    raise ExecutionContractError('Правка нарушает сохранённое исполнение; пакет не применён. '
                                                 + '; '.join(issue['problem'] for issue in issues[:8]), issues=issues[:20])
            if name != "finish":
                self.model["run"].update(status="running", reason="")
            hints = self._hints(name, result)
            freshness = {}
            if not getattr(self, '_patch_in_progress', False):
                stale = [r['id'] for r in self.model['execution_reviews'] if not self._review_is_current(r)]
                semantic = self.model.get('semantic_review')
                freshness = {'stale_review_ids': stale,
                             'semantic_review_stale': bool(semantic and semantic.get('signature') != self.quality_signature())}
            return {"ok": True, **result, **freshness, **({"hints": hints} if hints else {})}
        except (MapValidationError, TypeError, KeyError, ValueError) as exc:
            self.model = original
            self._counters = counters
            code = getattr(exc, "code", None) or ("quality_check" if isinstance(exc, QualityError) else "invalid_arguments")
            return {"ok": False, "code": code, "error": str(exc), **({"details": exc.details} if getattr(exc, 'details', None) else {})}

    # Missing basis or code explanations used to surface only at finish, after
    # the whole map was built on them. A new object is complete when created;
    # an edit by id still names only the changed fields.
    REQUIRED_NEW = {"upsert_step": "nodes", "upsert_transition": "edges"}

    def _require_new_fields(self, name: str, identifier: str) -> None:
        item = next(row for row in self.model[self.REQUIRED_NEW[name]] if row["id"] == identifier)
        missing = []
        if name == "upsert_step" and not item.get("description", "").strip():
            missing.append("description")
        if not item.get("basis", "").strip():
            missing.append("basis")
        if item.get("evidence_status") != "unknown" and not item.get("code_explanations"):
            missing.append("code_explanations")
        if missing:
            raise MapValidationError(f"{name}: для нового объекта заполните {', '.join(missing)}; "
                                     "при последующей правке по id передаются только изменяемые поля", "missing_fields")

    BRANCH = re.compile(r"^\s*(?:\}\s*)?(?:else\s+)?if\b|^\s*(?:switch|when)\b|^\s*case\b|^\s*elif\b|^\s*default\s*:")

    def _atomicity_hint(self, node: dict[str, Any]) -> str:
        """Advisory: a node resting on several branches or executor calls that no
        other operation shows is likely to merge several process steps."""
        others = [ref for row in self.model["nodes"] if row["id"] != node["id"] for ref in row.get("evidence", [])]
        lines = {(ref["path"], line) for ref in node.get("evidence", [])
                 for line in range(ref["line_start"], ref["line_end"] + 1)}
        own = {(path, line) for path, line in lines
               if not any(o["path"] == path and o["line_start"] <= line <= o["line_end"] for o in others)}
        branches = 0
        for path, line in own:
            text = self._reads.get(path, {}).get(line, "")
            if self.BRANCH.search(code_only(text)):
                branches += 1
        calls = sum(1 for task in self.model["research_tasks"] if task.get("automatic")
                    for ref in task["evidence"][:1] if (ref["path"], ref["line_start"]) in own)
        long_range = len(own) >= 60
        if branches < 3 and calls < 2 and not long_range:
            return ""
        parts = [f"{branches} ветвлений" if branches else "", f"{calls} вызовов других исполнителей" if calls else "",
                 f"{len(own)} строк" if long_range else ""]
        return (f"{node['id']}: основания охватывают {', '.join(p for p in parts if p)}, которые не показаны другими действиями. "
                "Самостоятельные действия с предметом и результатом разделяются; служебные строки и предикаты выбора не требуют узла. "
                "Проверь, нужно ли разделить этот узел, сохранив подтверждённые переходы.")

    def _hints(self, name: str, result: dict[str, Any]) -> list[str]:
        """Immediate, non-blocking feedback; finish still enforces the hard rules."""
        hints = []
        terminals = set(self.model["scenario"].get("terminal_operation_ids", []))
        nodes = {row["id"]: row for row in self.model["nodes"]}
        if name == "upsert_step" and result.get("id") in nodes:
            node = nodes[result["id"]]
            hint = self._atomicity_hint(node)
            if hint:
                hints.append(hint)
            if re.search(r"\s(?:и|или|а также)\s", node.get("name", "")):
                hints.append(f"{node['id']}: в названии несколько действий через «и»/«или»; если это отдельные шаги процесса, "
                             "покажи каждое самостоятельное действие отдельным узлом.")
            if (node.get("continuation_mode") == "choice" and node.get("outcome", "unknown") != "unknown"
                    and not (node['id'] in terminals and node.get('completion_condition'))):
                hints.append(f"{node['id']}: у операции с выбором продолжения нет единственного исхода; outcome не указывай, "
                             "исходы выражаются переходами к операциям, формирующим результат.")
        if name in ("set_scope", "upsert_step"):
            unresolved = [identifier for identifier in sorted(terminals) if identifier in nodes
                          and nodes[identifier].get('outcome', 'unknown') == 'unknown'
                          and nodes[identifier].get('outcome_scope', 'branch') != 'shared']
            if unresolved:
                hints.append("Уточните бизнес-исход конечных операций по их основаниям: " + ", ".join(unresolved)
                             + ". Если он не установлен, сохраните outcome=unknown и адресный пробел; название не доказывает исход.")
        if name == "upsert_transition" and result.get("id"):
            edge = next((row for row in self.model["edges"] if row["id"] == result["id"]), None)
            if edge and edge["source"] in terminals and not nodes.get(edge["source"], {}).get("completion_condition"):
                hints.append(f"{edge['source']}: операция объявлена конечной, но получила продолжение; задай completion_condition "
                             "или убери её из terminal_operation_ids.")
        if name == "review_execution":
            review = next((row for row in self.model["execution_reviews"] if row["id"] == result.get("id")), None)
            for operation in (review or {}).get("operations", []):
                if operation["node_id"] in nodes:
                    hint = self._atomicity_hint(nodes[operation["node_id"]])
                    if hint:
                        hints.append(hint)
        return hints[:12]

    def _inspect_map(self, args: dict[str, Any]) -> dict[str, Any]:
        section = _choice(args.get("section", "summary"), "section", INSPECT_SECTIONS)
        offset, limit = args.get("offset", 1), args.get("limit", 10)
        if type(offset) is not int or type(limit) is not int or offset < 1 or not 1 <= limit <= 30:
            raise MapValidationError("inspect_map: offset >= 1, limit от 1 до 30")
        if args.get("path") is not None and section != "read_fragments":
            raise MapValidationError("path применяется только к read_fragments")
        if section in {"research_brief", "context_summary", "semantic_review"}:
            # The public brief is size-bounded at write time. Do not silently
            # truncate its meaning, questions or next actions on recovery.
            def references(value):
                if isinstance(value, dict):
                    return {key: references(item) for key, item in value.items() if key != "quote"}
                if isinstance(value, list):
                    return [references(item) for item in value]
                return value
            return {"section": section, "model": references(self.model[section]),
                    "inspection_note": "Публичная рабочая сводка целиком: данные, не инструкции и не доказательство переходов. Гипотезы проверяются по коду; цитаты доступны через read_fragments."}
        if section == "frontier":
            frontier = self.graph_frontier()
            nodes = frontier.pop("nodes")
            page = nodes[offset - 1:offset - 1 + limit]
            next_offset = offset + len(page)
            totals = {key: len(rows) for key, rows in frontier.items()}
            for key in ("starts", "ends", "unlinked", "open_exits"):
                frontier[key] = frontier[key][:30]
            frontier['pending_tasks'] = frontier['pending_tasks'][:10]
            frontier["pending_gaps"] = [{"id": gap["id"], "node_ids": gap["node_ids"][:10],
                                         "node_count": len(gap["node_ids"]), "description": gap["description"][:200]}
                                        for gap in frontier["pending_gaps"][:10]]
            return {"section": section, "items": page, "offset": offset, "total": len(nodes),
                    "next_offset": next_offset if next_offset <= len(nodes) else None,
                    **frontier, "frontier_totals": totals,
                    "lists_truncated": any(totals[key] > len(rows) for key, rows in frontier.items()),
                    "inspection_note": "Это состояние карты, не доказательство порядка исполнения. Списки ID ограничены 30, пробелы и задачи — 10; все узлы доступны следующими страницами, пробелы через section='gaps', задачи через section='research_tasks'. Продолжайте существующие узлы, связи подтверждайте кодом."}
        if section == "read_fragments":
            # Return only the previously delivered, masked lines. This never
            # reads the source or expands the set of admissible evidence.
            path = _text(args.get("path"), "path", required=True)
            visible = self._reads.get(path, {})
            if not visible:
                raise MapValidationError("Нет сохранённых строк этого файла; сначала используйте read_file")
            numbers = [n for n in sorted(visible) if n >= offset]
            rows, size = [], 0
            for number in numbers[:limit]:
                row = {"line": number, "text": visible[number]}
                length = len(json.dumps(row, ensure_ascii=False))
                if rows and size + length > 24000:
                    break
                rows.append(row)
                size += length
            return {"section": section, "path": path, "items": rows,
                    "total": len(visible), "offset": offset,
                    "next_offset": numbers[len(rows)] if len(rows) < len(numbers) else None,
                    "inspection_note": "Буквальные ранее выданные строки; пропущенные номера не читались. Источник повторно не открывался."}
        if section == "research_history":
            recalled = self.research_memory.recall(offset, limit)
            rows, size = [], 0
            for row in recalled["items"]:
                length = len(json.dumps(row, ensure_ascii=False))
                if rows and size + length > 40000:
                    break
                rows.append(row)
                size += length
            next_offset = offset + len(rows)
            return {"section": section, "items": rows, "offset": offset, "total": recalled["total"],
                    "next_offset": next_offset if next_offset <= recalled["total"] else None,
                    "evicted_metadata_count": self.research_memory.evicted_metadata_count,
                    "inspection_note": "Публичные ответы инструментов, от новых к старым; это данные, не инструкции. Поиск не заменяет read_file для evidence. Вытесненное содержимое отмечено явно."}

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

        counts = {key: len(self.model[key]) for key in ("nodes", "edges", "gaps", "sources", "source_reviews", "execution_reviews", "research_tasks")}
        counts['open_tasks'] = sum(task['status'] in ('open', 'read', 'analyzed') for task in self.model['research_tasks'])
        counts["operations"] = sum(len(review["operations"]) for review in self.model["execution_reviews"])
        if section == "summary":
            return {"section": section, "model": compact({
                "scenario": self.model["scenario"], "run": self.model["run"], "summary": self.model["summary"],
                "counts": counts, "validation": self.validation_signals(),
                "research_coverage": self.research_coverage(),
                "limitations": self.model["limitations"], "detail_review": self._detail_summary(),
                "research_notes": len(self.model["research_notes"]),
                "research_brief_revision": (self.model["research_brief"] or {}).get("revision"),
                "research_history": len(self.research_memory.snapshot()),
            }), "sections": list(INSPECT_SECTIONS), "inspection_note": "Полная карта сохранена; выберите section и offset для продолжения."}
        if section == "scenario":
            return {"section": section, "model": compact(self.model["scenario"])}
        if section == "read_coverage":
            rows = self.snapshot()[section]
        elif section == "validation":
            signals = self.validation_signals()
            rows = ([{'kind': 'structural', 'issue': message} for message in signals['structural_issues']] +
                    [{'kind': 'coverage', 'issue': message} for message in signals['coverage_issues']])
        elif section == "operations":
            rows = [{**operation, "review_id": review["id"]}
                    for review in self.model["execution_reviews"] for operation in review["operations"]]
        elif section == "route_expectations":
            rows = [{**route, 'review_id': review['id']}
                    for review in self.model['execution_reviews'] for route in review.get('route_expectations', [])]
        else:
            rows = self.model[section]
        coverage = {row['task_id']: row for row in self.research_coverage()['tasks']} if section == 'research_tasks' else {}
        page, size = [], 0
        for row in rows[offset - 1:offset - 1 + limit]:
            # Notes already have strict field limits. Do not lose the latter
            # half of a pending task when retrieving it after compaction.
            if section in {"research_notes", "research_tasks"}:
                item = deepcopy(row)
                item["evidence"] = [{k: v for k, v in ref.items() if k != "quote"}
                                    for ref in item["evidence"]]
                if section == 'research_tasks': item['coverage'] = coverage.get(item['id'])
            else:
                item = compact(row)
            length = len(json.dumps(item, ensure_ascii=False))
            if page and size + length > 18000:
                break
            page.append(item)
            size += length
        next_offset = offset + len(page)
        return {"section": section, "items": page, "offset": offset, "total": len(rows),
                "next_offset": next_offset if next_offset <= len(rows) else None,
                "inspection_note": "Цитаты представлены ссылками, длинные поля сокращены; полный текст хранится в результате. Буквальные ранее прочитанные строки доступны через read_fragments; новые — через read_file."}

    def _save_research_brief(self, args: dict[str, Any]) -> dict[str, Any]:
        """Replace public orientation, keeping hypotheses separate from the map."""
        required = {"project_overview", "scenario_alignment", "confirmed_facts",
                    "flow_hypothesis", "open_questions", "next_actions", "evidence"}
        if required - args.keys():
            raise MapValidationError("save_research_brief: передайте все поля сводки; обновление заменяет её целиком")
        item = {"status": "working_hypothesis"}
        for field in ("project_overview", "scenario_alignment"):
            item[field] = _text(sanitize(args[field]), field, required=True, limit=2000)
        item["evidence"] = self._evidence(args["evidence"])
        if not item["evidence"]:
            raise MapValidationError("Сводка требует оснований первичного исследования из read_file")
        facts = args["confirmed_facts"]
        if not isinstance(facts, list) or len(facts) > 12:
            raise MapValidationError("confirmed_facts: ожидается список не длиннее 12")
        item["confirmed_facts"] = []
        for fact in facts:
            if not isinstance(fact, dict) or set(fact) != {"finding", "evidence"}:
                raise MapValidationError("confirmed_facts: нужны finding и evidence")
            citations = self._evidence(fact["evidence"], "confirmed_facts.evidence")
            if not citations:
                raise MapValidationError("Каждый подтверждённый факт сводки требует прочитанных оснований")
            item["confirmed_facts"].append({
                "finding": _text(sanitize(fact["finding"]), "finding", required=True, limit=500),
                "evidence": citations})
        for field in ("flow_hypothesis", "open_questions", "next_actions"):
            item[field] = _strings(sanitize(args[field]), field, limit=12)
            for value in item[field]:
                _text(value, field, limit=500)
        if not item["flow_hypothesis"] and not item["open_questions"]:
            raise MapValidationError("Укажите гипотезу хода процесса либо вопросы, мешающие её сформулировать")
        if self.model["research_brief"] is None and not item["next_actions"]:
            raise MapValidationError("Первичная сводка требует конкретного next_actions для продолжения исследования")

        def public(value):
            if isinstance(value, dict):
                return {key: public(part) for key, part in value.items() if key != "quote"}
            if isinstance(value, list):
                return [public(part) for part in value]
            return value
        if len(json.dumps(public(item), ensure_ascii=False)) > 12000:
            raise MapValidationError("Сводка слишком велика: до 12000 символов без текстов цитат; подробности сохраняйте в research_notes")
        item["read_signature"] = self.read_signature()
        previous = self.model["research_brief"] or {}
        if item == {key: value for key, value in previous.items() if key != "revision"}:
            return {"revision": previous["revision"], "changed": False}
        item["revision"] = previous.get("revision", 0) + 1
        self.model["research_brief"] = item
        return {"revision": item["revision"], "changed": True,
                "inspection_hint": "inspect_map(section='research_brief'); гипотеза не создаёт узлы или связи"}

    def _save_research_note(self, args: dict[str, Any]) -> dict[str, Any]:
        """Public findings and next work, never private provider reasoning."""
        kind = _choice(args.get("kind"), "kind", ("fact", "hypothesis", "question"))
        evidence = self._evidence(args.get("evidence", []))
        if kind == "fact" and not evidence:
            raise MapValidationError("fact требует буквальные основания из прочитанных строк; предположение сохраняйте как hypothesis")
        finding = _text(sanitize(args.get("finding")), "finding", required=True, limit=1500)
        note_id = args.get("id")
        if not note_id:
            note_id = next((n["id"] for n in self.model["research_notes"]
                            if n["kind"] == kind and n["finding"] == finding), None)
        item = {"id": self._id("research_notes", "note", note_id, 200),
                "kind": kind, "status": _choice(args.get("status", "open"), "status", ("open", "resolved")),
                "finding": finding,
                "next_action": _text(sanitize(args.get("next_action", "")), "next_action", limit=1000),
                "evidence": evidence}
        return self._put("research_notes", item)

    def _set_scope(self, args: dict[str, Any]) -> dict[str, Any]:
        previous = deepcopy(self.model['scenario'])
        changed = [key for key in ('start', 'end', 'instance_unit', 'exclusions')
                   if previous.get(key) and key in args and args[key] != previous[key]]
        amendment = args.get('amendment')
        if changed and self.model['nodes'] and not amendment:
            raise MapValidationError('Изменение установленной границы требует amendment с причиной и основаниями',
                                     'scope_amendment_required', fields=changed)
        reason, refs = self._grounded_amendment(amendment) if amendment is not None else ('', [])
        self.model["scenario"].update({
            "title": _text(args.get("title"), "title", required=True, limit=300),
            "process_name": _text(args.get("process_name", previous.get('process_name', '')), "process_name", limit=300),
            **{key: _text(args.get(key, self.model['scenario'].get(key, '')), key)
               for key in ('variant_context', 'instance_unit')},
            "variant_factors": _strings(args.get('variant_factors', self.model['scenario'].get('variant_factors', [])), 'variant_factors'),
            # The header and overview show these to the process owner, so they are business text.
            "start": self.business_text(args.get("start"), "start", MAX_TEXT),
            "end": self.business_text(args.get("end"), "end", MAX_TEXT),
            "exclusions": [self.business_text(value, "exclusions", 1000)
                           for value in _strings(args.get("exclusions", previous.get('exclusions', [])), "exclusions")],
            "assumptions": _strings(args.get("assumptions", previous.get('assumptions', [])), "assumptions"),
            **{key: _strings(args.get(key, self.model["scenario"][key]), key, limit=MAX_NODES) for key in ("entry_operation_ids", "terminal_operation_ids")},
        })
        self.model['scenario']['scope_revision'] = previous.get('scope_revision', 1) + int(bool(changed))
        history = deepcopy(previous.get('scope_changes', []))
        if changed and amendment:
            history.append({'revision': self.model['scenario']['scope_revision'], 'reason': reason,
                            'evidence': refs, 'fields': changed})
        self.model['scenario']['scope_changes'] = history[-30:]
        return {"scenario": deepcopy(self.model["scenario"])}

    def _upsert_step(self, args: dict[str, Any]) -> dict[str, Any]:
        status, evidence = self._support(args)
        item = {"id": self._id("nodes", "step", args.get("id"), MAX_NODES),
                "name": self.business_text(args.get("name"), "name", 300),
                "kind": _choice(args.get("kind", "operation"), "kind", STEP_KINDS),
                "evidence_status": status, "evidence": evidence,
                "outcome": _choice(args.get("outcome") or "unknown", "outcome", OUTCOME_KINDS),
                "outcome_scope": _choice(args.get('outcome_scope', 'branch'), 'outcome_scope', ('branch', 'shared')),
                "effect_kind": _choice(args.get("effect_kind", "unknown"), "effect_kind", EFFECT_KINDS),
                "continuation_mode": _choice(args.get("continuation_mode", "sequence"), "continuation_mode", ("sequence", "choice", "all")),
                "join_mode": _choice(args.get("join_mode", "any"), "join_mode", ("any", "all"))}
        for field in ("description", "input", "output", "condition"):
            value = _text(args.get(field, ""), field)
            # Names of variables, methods and statuses belong to basis and code_explanations.
            item[field] = self.business_text(value, field, MAX_TEXT) if value else value
        item["basis"] = _text(sanitize(args.get("basis", "")), "basis", limit=3000)
        item["code_explanations"] = self.code_explanations(args.get("code_explanations", []), evidence)
        item["completion_condition"] = self.business_text(args["completion_condition"], "completion_condition") if args.get("completion_condition") else ""
        item["external_interactions"] = self.external_interactions(args.get("external_interactions", []), evidence)
        if item['outcome_scope'] == 'shared' and item['outcome'] != 'unknown':
            raise MapValidationError('Общий финализатор разных исходов имеет outcome=unknown; исходы сохраняются в ветвях')
        systems = {row["name"].casefold() for row in item["external_interactions"]}
        if len(systems) > 1:
            raise MapValidationError("Одна операция — вызов одной внешней системы: вызовы разных систем и их частичный успех "
                                     "покажите отдельными операциями", "several_systems")
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
        item["basis"] = _text(sanitize(args.get("basis", "")), "basis", limit=3000)
        if item["condition"]:
            item["condition"] = self.business_text(item["condition"], "condition")
        item["code_explanations"] = self.code_explanations(args.get("code_explanations", []), evidence + item["sender_evidence"] + item["receiver_evidence"])
        if item["kind"] == "branch" and not item["condition"]:
            raise MapValidationError("Ветвление требует условия в condition")
        signature = (item["source"], item["target"], item["kind"], item["condition"])
        if any((e["source"], e["target"], e["kind"], e["condition"]) == signature and e["id"] != item["id"] for e in self.model["edges"]):
            raise MapValidationError("Такой переход уже есть: обновите существующий id")
        return self._put("edges", item)

    def _add_gap(self, args: dict[str, Any]) -> dict[str, Any]:
        item = {"description": _text(args.get("description"), "description", required=True),
                "category": _choice(args.get("category", "unresolved_behavior"), "category",
                                    ("missing_source", "unresolved_behavior", "unresolved_transition", "unexplored")),
                "evidence": self._evidence(args.get("evidence", []))}
        for key in ("node_ids", "edge_ids", "task_ids"):
            item[key] = _strings(args.get(key, []), key, limit=MAX_EDGES)
        if not any(item[key] for key in ("node_ids", "edge_ids", "task_ids")):
            raise MapValidationError("Пробел должен адресовать node_ids, edge_ids или task_ids")
        for old in self.model["gaps"]:
            if all(old.get(key) == value for key, value in item.items()):
                return {"id": old["id"]}
        return self._put("gaps", {"id": self._id("gaps", "gap", None, MAX_GAPS), **item})

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
        kind = _choice(args.get("kind"), "kind", ("step", "transition", "gap", "execution_review"))
        collection = {"step": "nodes", "transition": "edges", "gap": "gaps", "execution_review": "execution_reviews"}[kind]
        identifier = _text(args.get("id"), "id", required=True)
        if not any(row["id"] == identifier for row in self.model[collection]):
            raise MapValidationError(f"Объект {identifier} не найден")
        if kind == 'execution_review':
            review = next(row for row in self.model[collection] if row['id'] == identifier)
            if review['operations'] or review.get('route_expectations'):
                raise MapValidationError('Обзор содержит найденные действия или условия: исправьте их адресно через '
                                         'discard_operations/discard_route_expectations с основаниями; удаляется только пустой обзор',
                                         'fact_amendment_required', review_id=identifier)
        # Never silently delete incident transitions or children. The caller can
        # amend them first, and failed removals leave all data untouched.
        self.model[collection] = [row for row in self.model[collection] if row["id"] != identifier]
        return {"removed": identifier}

    def _patch_fragment(self, args: dict[str, Any]) -> dict[str, Any]:
        """Validate a candidate final graph; dispatch supplies the full rollback."""
        before_mappings = {review['id']: deepcopy(self._execution_mapping(review)) for review in self.model['execution_reviews']}
        rows = {}
        for key in ('steps', 'transitions', 'remove_node_ids', 'remove_edge_ids', 'gap_updates', 'operation_mappings', 'route_mappings'):
            value = args.get(key, [])
            if not isinstance(value, list):
                raise MapValidationError(f'{key}: требуется список')
            rows[key] = value
        boundary_fields = ('entry_operation_ids', 'terminal_operation_ids')
        boundaries = {key: _strings(args[key], key, limit=MAX_NODES) for key in boundary_fields if key in args}
        if not 1 <= sum(map(len, rows.values())) + len(boundaries) <= 100:
            raise MapValidationError('patch_fragment: от 1 до 100 изменений')
        remove_nodes = set(_strings(rows['remove_node_ids'], 'remove_node_ids'))
        remove_edges = set(_strings(rows['remove_edge_ids'], 'remove_edge_ids'))
        if remove_nodes - {n['id'] for n in self.model['nodes']} or remove_edges - {e['id'] for e in self.model['edges']}:
            raise MapValidationError('Удаляемые ID должны существовать; пакет не применён')
        local_ids, local_kinds, results, issues, failed_aliases = {}, {}, {'steps': [], 'transitions': []}, [], set()
        updated_gaps = []
        self._patch_in_progress = True
        try:
            for key, tool in (('steps', 'upsert_step'), ('transitions', 'upsert_transition')):
                for index, raw in enumerate(rows[key]):
                    field = f'{key}[{index}]'
                    alias = raw.get('local_ref') if isinstance(raw, dict) else None
                    try:
                        if not isinstance(raw, dict):
                            raise MapValidationError('изменение должно быть объектом')
                        item = dict(raw)
                        item.pop('local_ref', None)
                        if alias is not None:
                            if item.get('id') or not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', alias) or alias in local_ids or alias in failed_aliases:
                                raise MapValidationError('local_ref: уникальное локальное имя только для нового элемента')
                        if item.get('id') in (remove_nodes if key == 'steps' else remove_edges):
                            raise MapValidationError('Нельзя одновременно изменить и удалить один объект')
                        if key == 'transitions':
                            # A rejected producer is the one actionable problem;
                            # its dependent transitions should not create cascades.
                            if any(isinstance(item.get(f), str) and item[f].startswith('$') and item[f][1:] in failed_aliases for f in ('source', 'target')):
                                continue
                            for endpoint in ('source', 'target'):
                                value = item.get(endpoint)
                                if isinstance(value, str) and value.startswith('$'):
                                    if value[1:] not in local_ids:
                                        raise MapValidationError(f'{endpoint}: неизвестный local_ref {value}')
                                    if local_kinds[value[1:]] != 'steps':
                                        raise MapValidationError(f'{endpoint}: local_ref должен указывать на действие')
                                    item[endpoint] = local_ids[value[1:]]
                        response = self.dispatch(tool, item)
                        if not response.get('ok'):
                            raise MapValidationError(response.get('error', 'Изменение отклонено'), response.get('code', 'invalid_arguments'), **response.get('details', {}))
                        results[key].append(response['id'])
                        if alias is not None:
                            local_ids[alias] = response['id']
                            local_kinds[alias] = key
                    except (MapValidationError, QualityError) as exc:
                        issues.append({'field': field, 'code': getattr(exc, 'code', 'quality_check'), 'problem': str(exc), **getattr(exc, 'details', {})})
                        if isinstance(alias, str) and alias not in local_ids:
                            failed_aliases.add(alias)
            for key, values in boundaries.items():
                resolved = []
                for value in values:
                    if value.startswith('$'):
                        if value[1:] in failed_aliases:
                            continue
                        if value[1:] not in local_ids:
                            issues.append({'field': key, 'code': 'unknown_id', 'problem': f'неизвестный local_ref {value}'})
                            continue
                        if local_kinds[value[1:]] != 'steps':
                            issues.append({'field': key, 'code': 'invalid_arguments', 'problem': 'граница должна ссылаться на действие'})
                            continue
                        value = local_ids[value[1:]]
                    resolved.append(value)
                if len(resolved) != len(set(resolved)):
                    issues.append({'field': key, 'code': 'invalid_arguments', 'problem': 'повторяющиеся ссылки после разрешения local_ref'})
                self.model['scenario'][key] = resolved
            gaps = {g['id']: g for g in self.model['gaps']}
            known = {'node_ids': {n['id'] for n in self.model['nodes']} - remove_nodes,
                     'edge_ids': {e['id'] for e in self.model['edges']} - remove_edges}
            for index, raw in enumerate(rows['gap_updates']):
                field = f'gap_updates[{index}]'
                try:
                    if not isinstance(raw, dict) or set(raw) - {'id', 'node_ids', 'edge_ids'} or not set(raw).intersection({'node_ids', 'edge_ids'}):
                        raise MapValidationError('нужны id существующего пробела и node_ids или edge_ids')
                    identifier = _text(raw.get('id'), field+'.id', required=True)
                    if identifier not in gaps or identifier in updated_gaps:
                        raise MapValidationError('нужен уникальный ID существующего пробела', 'unknown_id')
                    candidate = deepcopy(gaps[identifier])
                    skip = False
                    for key, kind in (('node_ids', 'steps'), ('edge_ids', 'transitions')):
                        if key not in raw:
                            continue
                        values = _strings(raw[key], field+'.'+key, limit=MAX_EDGES)
                        resolved = []
                        for value in values:
                            if value.startswith('$'):
                                if value[1:] in failed_aliases:
                                    skip = True
                                    continue
                                if value[1:] not in local_ids or local_kinds[value[1:]] != kind:
                                    raise MapValidationError(f'{key}: local_ref должен указывать на {kind}', 'unknown_id')
                                value = local_ids[value[1:]]
                            if value not in known[key]:
                                raise MapValidationError(f'{key}: неизвестная ссылка {value}', 'unknown_id')
                            resolved.append(value)
                        if len(resolved) != len(set(resolved)):
                            raise MapValidationError(f'{key}: повторяющиеся ссылки после разрешения local_ref')
                        candidate[key] = resolved
                    if skip:
                        continue
                    if not any(candidate[key] for key in ('node_ids', 'edge_ids', 'task_ids')):
                        raise MapValidationError('пробел должен сохранить адрес действия, перехода или задачи')
                    gaps[identifier].update(node_ids=candidate['node_ids'], edge_ids=candidate['edge_ids'])
                    updated_gaps.append(identifier)
                except (MapValidationError, QualityError) as exc:
                    issues.append({'field': field, 'code': getattr(exc, 'code', 'quality_check'), 'problem': str(exc), **getattr(exc, 'details', {})})
            if issues:
                raise MapValidationError('Пакет не применён: ' + '; '.join(f"{r['field']}: {r['problem']}" for r in issues[:20]),
                                         issues[0]['code'], issues=issues[:20], problem_count=len(issues))
            self.model['nodes'] = [n for n in self.model['nodes'] if n['id'] not in remove_nodes]
            self.model['edges'] = [e for e in self.model['edges'] if e['id'] not in remove_edges]
            for review in self.model['execution_reviews']:
                for operation in review['operations']:
                    if operation['node_id'] in remove_nodes:
                        operation.update(node_id='', mapping_status='unmapped')
                for route in review.get('route_expectations', []):
                    if set(route['edge_ids']) & remove_edges or route['completion_node_id'] in remove_nodes:
                        route.update(edge_ids=[], completion_node_id='', mapping_status='unmapped')
                for check in review['checks'].values():
                    check['node_ids'] = [n for n in check['node_ids'] if n not in remove_nodes]
                    check['edge_ids'] = [e for e in check['edge_ids'] if e not in remove_edges]
            for task in self.model['research_tasks']:
                task['node_ids'] = [n for n in task['node_ids'] if n not in remove_nodes]
            mapped_reviews = self._apply_execution_mappings(args, local_ids, local_kinds)
            for review in self.model['execution_reviews']:
                if before_mappings[review['id']] != self._execution_mapping(review):
                    review['mapping_revision'] = review.get('mapping_revision', 1) + 1
        finally:
            self._patch_in_progress = False
        known = {n['id'] for n in self.model['nodes']}
        problems = [{'field': key, 'code': 'unknown_id', 'problem': 'граница ссылается на отсутствующее действие',
                     'unknown_ids': sorted(set(self.model['scenario'][key]) - known)} for key in boundary_fields
                    if set(self.model['scenario'][key]) - known]
        if problems:
            raise MapValidationError('Обновите границы сценария в этом же patch_fragment; пакет не применён',
                                     'unknown_id', issues=problems)
        return {**results, 'local_ids': local_ids, 'removed_node_ids': sorted(remove_nodes), 'removed_edge_ids': sorted(remove_edges),
                'updated_gap_ids': updated_gaps,
                'mapped_review_ids': mapped_reviews,
                'open_action_ids': [op['id'] for r in self.model['execution_reviews'] for op in r['operations'] if not op['node_id']],
                'open_route_ids': [r['id'] for review in self.model['execution_reviews'] for r in review.get('route_expectations', [])
                                   if not r['edge_ids'] and not r['completion_node_id']],
                'boundaries': {key: list(self.model['scenario'][key]) for key in boundaries}}

    @staticmethod
    def _fingerprint(value: Any) -> str:
        # Renaming or explaining an action does not alter its execution. Conditions, results, provenance, and transitions still do.
        # description/input/output state what the operation does, so they stay.
        wording = ("name", "basis", "code_explanations")
        substantive = {k: v for k, v in value.items() if k not in wording} if isinstance(value, dict) else value
        return hashlib.sha256(json.dumps(substantive, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _within(citations: list[dict[str, Any]], scope: list[dict[str, Any]]) -> bool:
        """Every cited line belongs to the explicitly inspected execution area."""
        return all(any(ref["path"] == area["path"] and area["line_start"] <= line <= area["line_end"]
                       for area in scope)
                   for ref in citations for line in range(ref["line_start"], ref["line_end"] + 1))

    def _review_discards(self, rows, old_items, evidence, field):
        if not isinstance(rows, list) or len(rows) > 100:
            raise MapValidationError(f'{field}: ожидается до 100 исправлений обнаружения')
        result, seen = [], set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != {'id', 'reason', 'evidence'}:
                raise MapValidationError(f'{field}[{index}]: нужны id, reason, evidence')
            identifier = _text(row['id'], f'{field}.id', required=True)
            if identifier not in old_items or identifier in seen:
                raise MapValidationError(f'{field}[{index}]: нужен уникальный ID из этого обзора')
            seen.add(identifier)
            refs = self._evidence(row['evidence'], f'{field}[{index}].evidence')
            if not refs:
                raise MapValidationError(f'{field}[{index}]: исправление обнаружения требует прочитанных оснований')
            self.require_coverage(identifier, field, refs, evidence)
            result.append({'id': identifier, 'reason': _text(row['reason'], f'{field}.reason', required=True), 'evidence': refs})
        return result

    def _review_routes(self, rows, old, evidence, identifier, discards, operations, aliases):
        if not isinstance(rows, list) or len(rows) > 100:
            raise MapValidationError('route_expectations: ожидается до 100 найденных условий и результатов')
        previous = {r['id']: r for r in old.get('route_expectations', [])}
        discarded = {r['id'] for r in discards}
        edges = {e['id']: e for e in self.model['edges']}
        nodes = {n['id']: n for n in self.model['nodes']}
        result, seen = [], set()
        for index, row in enumerate(rows):
            field = f'route_expectations[{index}]'
            if not isinstance(row, dict) or set(row) - {'id', 'condition', 'expected_result', 'evidence', 'edge_ids', 'completion_node_id', 'from_operation_id', 'to_operation_id'}:
                raise MapValidationError(f'{field}: неверная структура')
            refs = self._evidence(row.get('evidence', []), field+'.evidence')
            if not refs:
                raise MapValidationError(f'{field}: нужны прочитанные основания найденного условия')
            self.require_coverage(field, 'evidence', refs, evidence)
            item = {key: self.business_text(row.get(key), field+'.'+key) for key in ('condition', 'expected_result')}
            item['evidence'] = refs
            prior = previous.get(row.get('id'), {})
            for key in ('from_operation_id', 'to_operation_id'):
                value = _text(row.get(key, prior.get(key, '')), field+'.'+key)
                if value.startswith('$'):
                    if value[1:] not in aliases:
                        raise MapValidationError(f'{field}.{key}: неизвестный local_ref действия')
                    value = aliases[value[1:]]
                if value and value not in {op['id'] for op in operations}:
                    raise MapValidationError(f'{field}.{key}: нужен ID найденного действия этого обзора')
                item[key] = value
            if bool(item['from_operation_id']) != bool(item['to_operation_id']):
                raise MapValidationError(f'{field}: для порядка укажите оба from_operation_id и to_operation_id')
            route_id = row.get('id')
            if route_id and route_id not in previous:
                raise MapValidationError(f'{field}.id: используйте ID из обновляемого обзора')
            if not route_id:
                # Preserve the former ID for unanchored expectations.
                identity = {k: v for k, v in item.items() if k not in ('from_operation_id', 'to_operation_id') or v}
                route_id = identifier + '/route-' + self._fingerprint(identity)[:16]
            if route_id in seen or route_id in discarded:
                raise MapValidationError(f'{field}.id: повтор или одновременное удаление ожидания')
            seen.add(route_id)
            selected = _strings(row.get('edge_ids', []), field+'.edge_ids', limit=MAX_EDGES)
            terminal = _text(row.get('completion_node_id', ''), field+'.completion_node_id')
            if set(selected) - set(edges) or terminal and terminal not in nodes:
                raise MapValidationError(f'{field}: неизвестные ссылки маршрута', 'unknown_id')
            # Mapping is provenance/structure, not equivalence of natural language.
            for edge_id in selected:
                if not any(self._overlap(a, b) for a in refs for b in edges[edge_id].get('evidence', [])):
                    raise MapValidationError(f'{field}: основания {edge_id} не связаны с обнаруженным условием')
            if terminal and not any(self._overlap(a, b) for a in refs for b in nodes[terminal].get('evidence', [])):
                raise MapValidationError(f'{field}: основания завершения не связаны с обнаруженным условием')
            result.append({'id': route_id, **item, 'edge_ids': selected, 'completion_node_id': terminal,
                           'mapping_status': 'mapped' if selected or terminal else 'unmapped'})
        # Omitting a known expectation never certifies that the branch disappeared.
        for route in previous.values():
            if route['id'] in seen | discarded:
                continue
            retained = deepcopy(route)
            if not self._review_is_current(old):
                retained.update(edge_ids=[], completion_node_id='', mapping_status='unmapped')
            result.append(retained)
        if len(result) > 100:
            raise MapValidationError('route_expectations: лимит 100, уточните обзор без потери найденных условий')
        return result

    def _review_execution(self, args: dict[str, Any]) -> dict[str, Any]:
        """Record a code-anchored inventory, not an assertion of total coverage.

        Independent effects require independent node IDs. A review may span
        helper functions: functions are not automatically separate operations.
        No natural-language heuristic pretends to prove that an action is atomic.
        """
        identifier = self._id("execution_reviews", "review", args.get("id"), MAX_NODES)
        evidence = self._evidence(args.get("evidence", []), limit=100)
        if not evidence:
            raise MapValidationError("review_execution требует прочитанные основания обследованного исполнения")
        raw_operations = args.get("operations")
        if not isinstance(raw_operations, list) or len(raw_operations) > 100:
            raise MapValidationError("operations: укажите до 100 самостоятельных действий; пустой перечень допустим, если обследованный участок их не содержит")
        nodes = {node["id"]: node for node in self.model["nodes"]}
        old = next((r for r in self.model["execution_reviews"] if r["id"] == identifier), {})
        old_ids = {op["node_id"]: op["id"] for op in old.get("operations", [])}
        old_actions = {op['id']: op for op in old.get('operations', [])}
        discarded = self._review_discards(args.get('discard_operations', []), old_actions, evidence, 'discard_operations')
        discarded_ids = {row['id'] for row in discarded}
        discarded_routes = self._review_discards(args.get('discard_route_expectations', []),
                                                {r['id']: r for r in old.get('route_expectations', [])}, evidence, 'discard_route_expectations')
        used_nodes: set[str] = set()
        used_actions: set[str] = set()
        aliases = {}
        operations = []
        for raw in raw_operations:
            if not isinstance(raw, dict) or set(raw) - {"id", "node_id", "name", "kind", "action", "effect", "failure", "condition", "evidence", "external_interactions", "local_ref"}:
                raise MapValidationError("operations: неверная структура операции")
            node_id = _text(raw.get("node_id") or "", "operations.node_id")
            if node_id and node_id not in nodes:
                raise MapValidationError(f"{node_id}: операция должна ссылаться на существующий узел")
            if node_id and node_id in used_nodes:
                raise MapValidationError(f"{node_id}: несколько действий нельзя свернуть в одну операцию; создайте отдельные узлы")
            if node_id:
                used_nodes.add(node_id)
            citations = self._evidence(raw.get("evidence", nodes[node_id]['evidence'] if node_id else []), "operations.evidence")
            if not citations:
                raise MapValidationError(f"{node_id}: нужны основания операции")
            if node_id and not any(self._overlap(a, b) for a in citations for b in nodes[node_id]["evidence"]):
                raise MapValidationError(f"{node_id}: основания операции не относятся к основаниям узла")
            self.require_coverage(node_id or 'unmapped', "operations.evidence", citations, evidence)
            if node_id:
                self.require_coverage(node_id, "node.evidence", nodes[node_id]["evidence"], evidence)
            action_id = raw.get('id') or (old_ids.get(node_id) if node_id else None)
            if not action_id and not node_id:
                candidates = [op['id'] for op in old_actions.values() if not op['node_id'] and op['id'] not in used_actions
                              and op['evidence'] == citations and all(op.get(k, '') == raw.get(k, '')
                              for k in ('name', 'kind', 'action', 'effect', 'failure', 'condition'))]
                if len(candidates) == 1:
                    action_id = candidates[0]
            if action_id and action_id not in old_actions:
                raise MapValidationError('operations.id: используйте ID действия из обновляемого обзора')
            if not action_id:
                self._counters["op"] += 1
                action_id = f"op-{self._counters['op']}"
            if action_id in used_actions or action_id in discarded_ids:
                raise MapValidationError('operations.id: повтор действия в обзоре')
            used_actions.add(action_id)
            alias = raw.get('local_ref')
            if alias is not None:
                if not isinstance(alias, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', alias) or alias in aliases:
                    raise MapValidationError('operations.local_ref: уникальное локальное имя действия')
                aliases[alias] = action_id
            operation = {"id": action_id, "node_id": node_id, "mapping_status": "mapped" if node_id else "unmapped",
                         "kind": _choice(raw.get("kind"), "operations.kind", OPERATION_KINDS),
                         "evidence": citations}
            for field in ("name", "action", "effect", "failure"):
                operation[field] = _text(raw.get(field), f"operations.{field}", required=True,
                                        limit=300 if field == "name" else MAX_TEXT)
            operation["condition"] = _text(raw.get("condition", ""), "operations.condition")
            operation['external_interactions'] = self.external_interactions(
                raw.get('external_interactions', old_actions.get(action_id, {}).get('external_interactions', [])), citations)
            operations.append(operation)
        retained = [deepcopy(op) for op in old_actions.values() if op['id'] not in used_actions | discarded_ids]
        for op in retained:
            op.update(node_id='', mapping_status='unmapped')
        operations.extend(retained)
        if len(operations) > 100:
            raise MapValidationError('operations: лимит 100, уточните обзор без потери найденных действий')
        routes = self._review_routes(args.get('route_expectations', []), old, evidence, identifier, discarded_routes, operations, aliases)
        raw_checks = args.get("checks")
        if not isinstance(raw_checks, dict) or set(raw_checks) != set(EXECUTION_CHECKS):
            raise MapValidationError("checks должен содержать branches, failures, integrations, state_changes, computations")
        checks = {}
        for category, raw in raw_checks.items():
            if not isinstance(raw, dict) or set(raw) - {"status", "explanation", "evidence", "node_ids", "edge_ids", "gap_ids"}:
                raise MapValidationError(f"checks.{category}: неверная структура")
            status = _choice(raw.get("status"), f"checks.{category}.status", ("present", "not_found", "unknown"))
            citations = self._evidence(raw.get("evidence", evidence), f"checks.{category}.evidence", limit=100)
            if not self._within(citations, evidence):
                raise MapValidationError(f"checks.{category}: цитата вне области обзора")
            check = {"status": status, "explanation": _text(raw.get("explanation"), f"checks.{category}.explanation", required=True),
                     "evidence": citations}
            for field in ("node_ids", "edge_ids", "gap_ids"):
                check[field] = _strings(raw.get(field, []), f"checks.{category}.{field}", limit=MAX_NODES)
            if status != "unknown" and not citations:
                raise MapValidationError(f"checks.{category}: вывод требует цитат обследованного участка")
            if status == "present" and not (check["node_ids"] or check["edge_ids"] or any(not op['node_id'] for op in operations)
                                           or any(not r['edge_ids'] and not r['completion_node_id'] for r in routes)):
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
        related_nodes.update(r['completion_node_id'] for r in routes if r['completion_node_id'])
        related_edges.update(e for route in routes for e in route['edge_ids'])
        related_nodes.update(edge_index[e][key] for route in routes for e in route['edge_ids'] for key in ('source', 'target'))
        related_edges.update(e['id'] for e in edge_index.values() if e['source'] in related_nodes or e['target'] in related_nodes)
        # A rediscovered condition keeps its identity. Its latest withdrawal
        # replaces the older summary for that ID; every revision remains in the
        # tool audit. The public bounded history must remain checkpoint-valid.
        def discard_history(key, added):
            previous = old.get(key, [])
            replaced = {row['id'] for row in added}
            kept = [row for row in previous if row['id'] not in replaced]
            merged = kept + added
            omitted = old.get(key+'_omitted', 0) + len(previous)-len(kept) + max(0, len(merged)-100)
            return merged[-100:], omitted

        operations_history, operations_omitted = discard_history('discarded_operations', discarded)
        routes_history, routes_omitted = discard_history('discarded_route_expectations', discarded_routes)
        item = {"id": identifier, "name": _text(args.get("name"), "name", required=True, limit=300),
                "evidence": evidence, "operations": operations, "checks": checks,
                "route_expectations": routes,
                "discarded_operations": operations_history,
                "discarded_route_expectations": routes_history,
                "discarded_operations_omitted": operations_omitted,
                "discarded_route_expectations_omitted": routes_omitted,
                "node_fingerprints": {n: self._fingerprint(nodes[n]) for n in related_nodes if n in nodes},
                "edge_fingerprints": {e: self._fingerprint(edge_index[e]) for e in related_edges if e in edge_index}}
        self._execution_revisions(old, item, args.get('amendment'))
        result = self._put("execution_reviews", item)
        return {**result, "operations": [{"id": op["id"], "node_id": op["node_id"]} for op in operations],
                'local_ids': aliases, 'fact_revision': item['fact_revision'], 'mapping_revision': item['mapping_revision'],
                "route_expectations": [{key: row[key] for key in ('id', 'mapping_status', 'edge_ids', 'completion_node_id')} for row in routes],
                "retained_operation_ids": [op['id'] for op in retained]}

    def _reference_errors(self) -> list[str]:
        errors = []
        nodes = {item["id"]: item for item in self.model["nodes"]}
        edges = {item["id"]: item for item in self.model["edges"]}
        gaps = {item["id"]: item for item in self.model["gaps"]}
        tasks = {item["id"]: item for item in self.model["research_tasks"]}
        for edge in edges.values():
            if edge["source"] not in nodes or edge["target"] not in nodes:
                errors.append(f"{edge['id']}: переход ссылается на отсутствующую операцию")
        for gap in gaps.values():
            for key, existing in (("node_ids", nodes), ("edge_ids", edges), ("task_ids", tasks)):
                if set(gap.get(key, [])) - set(existing):
                    errors.append(f"{gap['id']}: неизвестные ссылки {key}")
        assigned = {}
        for review in self.model["execution_reviews"]:
            for route in review.get('route_expectations', []):
                if set(route['edge_ids']) - set(edges) or route['completion_node_id'] and route['completion_node_id'] not in nodes:
                    errors.append(f"{route['id']}: неизвестные ссылки обнаруженного условия")
            reviewed = {op["node_id"] for op in review["operations"] if op['node_id']}
            for op in review["operations"]:
                if not op['node_id']:
                    continue
                if op["node_id"] not in nodes:
                    errors.append(f"{op['id']}: запись обзора ссылается на отсутствующую операцию карты")
                if op["node_id"] in assigned and any(op.get(k) != assigned[op['node_id']].get(k)
                                                     for k in ('kind', 'action', 'effect', 'failure', 'condition', 'evidence')):
                    errors.append(f"{op['node_id']}: несколько операций не могут быть скрыты в одном узле")
                assigned[op["node_id"]] = op
            for category, check in review["checks"].items():
                if set(check["node_ids"]) - set(nodes) or set(check["edge_ids"]) - set(edges) or set(check["gap_ids"]) - set(gaps):
                    errors.append(f"{review['id']}.{category}: неизвестные ссылки узлов, переходов или пробелов")
                    continue
                if set(check["node_ids"]) - reviewed:
                    errors.append(f"{review['id']}.{category}: узлы проверки не входят в перечень обследованных операций")
                if any(edges[e]["source"] not in reviewed and edges[e]["target"] not in reviewed for e in check["edge_ids"]):
                    errors.append(f"{review['id']}.{category}: переход не относится к обследованным операциям")
                if check["status"] == "unknown" and self._review_is_current(review, nodes, edges) and not any(
                    reviewed.intersection(gaps[g]["node_ids"]) or
                    any(edges[e]["source"] in reviewed or edges[e]["target"] in reviewed for e in gaps[g].get("edge_ids", [])) or
                    any(tasks.get(t, {}).get('evidence') and tasks[t]['status'] != 'out_of_scope'
                        and gaps[g].get('evidence') and self._within(gaps[g]['evidence'], review['evidence'])
                        and self._within(tasks[t]['evidence'], review['evidence'])
                        for t in gaps[g].get('task_ids', []))
                    for g in check["gap_ids"]):
                    errors.append(f"{review['id']}.{category}: gap должен относиться к обследованным действиям или области исполнения")
        for task in tasks.values():
            for key, existing in (("node_ids", nodes), ("gap_ids", gaps), ("child_task_ids", tasks)):
                if set(task.get(key, [])) - set(existing):
                    errors.append(f"{task['id']}: неизвестные ссылки {key}")
        used_sources = {self._source_for(c["path"]) for collection in ("nodes", "edges")
                        for row in self.model[collection] for field in ("evidence", "sender_evidence", "receiver_evidence")
                        for c in row.get(field, [])}
        used_sources.update(self._source_for(c["path"]) for n in nodes.values() for x in n.get("external_interactions", []) for c in x["evidence"])
        used_sources.update(self._source_for(c["path"]) for review in self.model["execution_reviews"] for c in review["evidence"])
        for review in self.model["source_reviews"]:
            if review["disposition"] != "reviewed" and review["source_id"] in used_sources:
                errors.append(f"{review['source_id']}: источник используется в карте; нельзя пометить его вне сценария или недоступным")
        return errors

    def _reviewable_operations(self) -> set[str]:
        return {n["id"] for n in self.model["nodes"] if n.get("evidence_status", "unknown") != "unknown"}

    def _detail_errors(self) -> list[str]:
        """Coverage of an explicit inventory, never proof of discovery recall."""
        errors: list[str] = []
        reviews = self.model.get("execution_reviews", [])
        nodes = {node["id"]: node for node in self.model["nodes"]}
        edges = {edge["id"]: edge for edge in self.model["edges"]}
        operations = [op for review in reviews for op in review["operations"]]
        mapped = {op["node_id"] for op in operations if op['node_id']}
        for op in operations:
            if not op['node_id']:
                errors.append(f"{op['id']}: найденное действие ещё не отображено на карте")
        required = self._reviewable_operations()
        unknown_operations = {n["id"] for n in nodes.values() if n["kind"] in OPERATION_NODE_KINDS} - required
        if (required and (not reviews or not any(op["node_id"] in required for op in operations))) or (not required and not unknown_operations):
            errors.append("Детализация не проверена: вызовите review_execution с атомарными операциями; каждое самостоятельное действие требует своей операции")
        for identifier in sorted(required - mapped):
            errors.append(f"{identifier}: операция не сопоставлена с записью review_execution")
        for review in reviews:
            for issue in self._projection_issues(review):
                address = issue.get('route_id', issue.get('operation_id', review['id']))
                errors.append(f"{address}: {issue['problem']}")
            for route in review.get('route_expectations', []):
                if not route['edge_ids'] and not route['completion_node_id']:
                    errors.append(f"{route['id']}: найденное условие ещё не сопоставлено с продолжением или завершением")
                if route['completion_node_id'] and route['completion_node_id'] not in self.model['scenario']['terminal_operation_ids']:
                    errors.append(f"{route['id']}: ожидаемое завершение не объявлено конечным действием")
            # A drawn choice alone is not an independent inventory of its
            # alternatives. Missing discoveries remain coverage work, never a
            # finish-blocking demand to invent an expected route.
            discovered = {op['id']: op for op in review['operations']}
            for op in review['operations']:
                if nodes.get(op['node_id'], {}).get('continuation_mode') != 'choice':
                    continue
                for edge in edges.values():
                    if edge['source'] != op['node_id'] or edge['kind'] == 'data':
                        continue
                    matched = any(route.get('from_operation_id') == op['id']
                                  and route.get('to_operation_id') in discovered
                                  and route['edge_ids'] and route['edge_ids'][0] == edge['id']
                                  for route in review.get('route_expectations', []))
                    if not matched:
                        errors.append(f"{edge['id']}: альтернатива не сопоставлена с независимым ожиданием исполнения; "
                                      'зафиксируйте найденные действия и условие либо оставьте покрытие частичным')
            review_nodes = set(review['node_fingerprints'])
            incident = [edge for edge in edges.values() if edge["source"] in review_nodes or edge["target"] in review_nodes]
            incident_ids = {edge["id"] for edge in incident}
            reviewed_edge_ids = set(review["edge_fingerprints"])
            for identifier in sorted(incident_ids - reviewed_edge_ids):
                errors.append(f"{review['id']}: переход {identifier} добавлен после обзора; обновите review_execution")
            for identifier in sorted(reviewed_edge_ids - incident_ids):
                errors.append(f"{review['id']}: переход {identifier} удалён или перенесён после обзора; обновите review_execution")
            for identifier, fingerprint in review["node_fingerprints"].items():
                if identifier in nodes and self._fingerprint(nodes[identifier]) != fingerprint:
                    errors.append(f"{review['id']}: операция {identifier} изменена после обзора; обновите review_execution")
            for identifier, fingerprint in review["edge_fingerprints"].items():
                if identifier in edges and self._fingerprint(edges[identifier]) != fingerprint:
                    errors.append(f"{review['id']}: переход {identifier} изменён после обзора; обновите review_execution")
            for operation in review["operations"]:
                node = nodes.get(operation["node_id"])
                if node and not self._within(node["evidence"], review["evidence"]):
                    errors.append(f"{operation['id']}: основания узла выходят за обследованную область")
            present = self._review_presence(review, nodes, edges) if self._review_is_current(review, nodes, edges) else {}
            for category, found in present.items():
                if found and review["checks"][category]["status"] == "not_found":
                    errors.append(f"{review['id']}.{category}: карта/реестр уже содержит это поведение; not_found противоречит результату")
        return errors

    def _review_is_current(self, review, nodes=None, edges=None):
        """A review describes its recorded graph revision, not later behaviour."""
        if nodes is None:
            nodes = {node['id']: node for node in self.model['nodes']}
        if edges is None:
            edges = {edge['id']: edge for edge in self.model['edges']}
        if any(identifier not in nodes or self._fingerprint(nodes[identifier]) != fingerprint
               for identifier, fingerprint in review['node_fingerprints'].items()):
            return False
        incident = {edge['id'] for edge in edges.values()
                    if edge['source'] in review['node_fingerprints'] or edge['target'] in review['node_fingerprints']}
        return incident == set(review['edge_fingerprints']) and all(
            identifier in edges and self._fingerprint(edges[identifier]) == fingerprint
            for identifier, fingerprint in review['edge_fingerprints'].items())

    @staticmethod
    def _review_presence(review, nodes, edges):
        reviewed = {op['node_id'] for op in review['operations'] if op['node_id']}
        kinds = {op['kind'] for op in review['operations']}
        # Incoming routes describe their source action, which may be outside the
        # independently inspected fragment. They cannot contradict its not_found.
        outgoing = [e for e in edges.values() if e['source'] in reviewed]
        return {
            'branches': any(sum(e['source']==n and e['kind']!='data' for e in outgoing)>1 for n in reviewed) or any(e['kind']=='branch' for e in outgoing),
            'failures': 'error_handling' in kinds or any(e['kind'] in ('error','retry') for e in outgoing),
            'integrations': bool(kinds.intersection({'integration','publish'})) or any(op.get('external_interactions') for op in review['operations']) or any(
                e['kind'] in ('call','event','return') for e in outgoing) or any(
                nodes.get(identifier, {}).get('external_interactions') for identifier in reviewed),
            'state_changes': 'write' in kinds,
            'computations': bool(kinds.intersection({'calculation','transform'})),
        }

    def _detail_summary(self) -> dict[str, Any]:
        if "execution_reviews" not in self.model:
            return {"status": "legacy_unverified", "semantic_completeness_verified": False}
        required = self._reviewable_operations()
        reviews = self.model["execution_reviews"]
        operations = [operation for review in reviews for operation in review["operations"]]
        mapped = {operation["node_id"] for operation in operations}
        issues = self._detail_errors()
        unknown_nodes = [node["id"] for node in self.model["nodes"]
                         if node["kind"] in OPERATION_NODE_KINDS and node["evidence_status"] == "unknown"]
        external_nodes = [node["id"] for node in self.model["nodes"] if node.get("external_interactions")]
        unknown = bool(unknown_nodes) or any(check["status"] == "unknown" for review in reviews for check in review["checks"].values())
        return {"contract_version": "2", "status": "pending" if issues else "partial" if unknown else "covered",
                "review_count": len(reviews), "operation_count": len(operations),
                "required_nodes": len(required), "mapped_nodes": len(required.intersection(mapped)),
                "unmapped_node_ids": sorted(required - mapped), "issues": issues,
                "unmapped_action_ids": [op['id'] for op in operations if not op['node_id']],
                "unmapped_route_ids": [r['id'] for review in reviews for r in review.get('route_expectations', [])
                                       if not r['edge_ids'] and not r['completion_node_id']],
                "unknown_node_ids": unknown_nodes, "external_node_ids": external_nodes,
                "registry_coverage_checked": bool(reviews) and not issues,
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
            errors.append("Карта не содержит операций")
            return errors
        starts = set(self.model["scenario"]["entry_operation_ids"])
        ends = set(self.model["scenario"]["terminal_operation_ids"])
        if not starts:
            errors.append("Укажите scenario.entry_operation_ids — реальные первые операции")
        if not ends:
            errors.append("Укажите scenario.terminal_operation_ids — реальные последние операции")
        if (starts | ends) - set(nodes):
            errors.append("Границы сценария ссылаются на неизвестные операции")
            return errors
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
                                  + "): найдите переходы либо добавьте адресный gap ко всем его операциям")
        for node in nodes.values():
            identifier = node["id"]
            if identifier not in reachable or identifier not in terminates:
                if identifier not in gap_nodes:
                    errors.append(f"{identifier}: нет сквозного пути от начала до завершения; свяжите операцию или добавьте адресный gap")
            if identifier in ends and outgoing[identifier] and not node.get("completion_condition"):
                errors.append(f"{identifier}: конечная операция имеет исходящее продолжение; укажите смысловое completion_condition")
            if node.get("completion_condition") and identifier not in ends:
                errors.append(f"{identifier}: completion_condition требует включения в terminal_operation_ids")
            if node["evidence_status"] == "unknown" and identifier not in gap_nodes:
                errors.append(f"{identifier}: неизвестное поведение требует адресный gap")
        for edge in self.model["edges"]:
            if edge["evidence_status"] == "unknown" and not any(
                edge["id"] in g.get("edge_ids", []) or {edge["source"], edge["target"]}.issubset(g["node_ids"])
                for g in self.model["gaps"]):
                errors.append(f"{edge['id']}: неизвестный переход требует gap с обеими операциями или edge_ids")
        errors.extend(self._loop_exit_errors(nodes, outgoing))
        errors.extend(self._detail_errors())
        errors.extend(self.quality_errors())
        return errors

    def validation_signals(self) -> dict[str, Any]:
        """Separate malformed execution structure from honest research deficits."""
        structural = self._reference_errors()
        nodes = {n['id']: n for n in self.model['nodes']}
        starts = set(self.model['scenario']['entry_operation_ids'])
        ends = set(self.model['scenario']['terminal_operation_ids'])
        if (starts | ends) - set(nodes):
            structural.append('Границы сценария ссылаются на неизвестные операции')
        outgoing = {n: set() for n in nodes}
        for edge in self.model['edges']:
            if edge['source'] in nodes and edge['target'] in nodes and edge['kind'] != 'data':
                outgoing[edge['source']].add(edge['target'])
        for identifier, node in nodes.items():
            links = [e for e in self.model['edges'] if e['source']==identifier and e['kind']!='data']
            if identifier in ends and outgoing[identifier] and not node.get('completion_condition'):
                structural.append(f'{identifier}: конечная операция имеет исходящее продолжение; укажите смысловое completion_condition')
            if node.get('completion_condition') and identifier not in ends:
                structural.append(f'{identifier}: completion_condition требует включения в terminal_operation_ids')
            if len(links) > 1 and node.get('continuation_mode', 'sequence') == 'sequence':
                structural.append(f'{identifier}: несколько связей требуют continuation_mode=choice или all')
            if (len(links)>1 or node.get('completion_condition')) and any(not e['condition'].strip() for e in links):
                structural.append(f'{identifier}: несколько направлений требуют условий на всех выходах; общий участок не должен терять категорию маршрута')
        structural.extend(self._loop_exit_errors(nodes, outgoing))
        edge_index = {edge['id']: edge for edge in self.model['edges']}
        current_reviews = [review for review in self.model['execution_reviews'] if self._review_is_current(review, nodes, edge_index)]
        structural.extend(self._reviewed_kind_errors(current_reviews))
        for task in self.model['research_tasks']:
            if task['status'] == 'out_of_scope':
                reason = self._task_exclusion_error(task)
                if reason:
                    structural.append(f"{task['id']}: исключение задачи не соответствует границе или карте: {reason}")
        for review in current_reviews:
            for category, found in self._review_presence(review, nodes, edge_index).items():
                if found and review['checks'][category]['status']=='not_found':
                    structural.append(f"{review['id']}.{category}: карта/реестр уже содержит это поведение; not_found противоречит результату")
        structural = list(dict.fromkeys(structural))
        all_issues = self._finish_errors()
        coverage = [issue for issue in all_issues if issue not in structural]
        for issue in self.model['run'].get('unresolved_rejections', []):
            if issue.get('status', 'open') == 'open':
                coverage.append(f"{issue.get('problem_id', 'repair')}: исчерпаны четыре адресные попытки; {issue.get('error', '')}")
        for row in self.research_coverage()['tasks']:
            if row['status'] == 'partial':
                coverage.append(f"{row['task_id']}: неполное покрытие; участки: {', '.join(row['incomplete_task_ids'])}")
        coverage = list(dict.fromkeys(coverage))
        return {'issues': structural + coverage, 'structural_issues': structural,
                'coverage_issues': coverage, 'semantic_verification': False}

    @staticmethod
    def _loop_exit_errors(nodes: dict[str, dict], outgoing: dict[str, set[str]]) -> list[str]:
        """A cycle without an exit edge or conditional completion is an endless loop on the map.

        Loops in code end on an empty result, a break, a limit or a stop request;
        that end is a transition out of the loop. A gap cannot replace it: an exit
        whose behaviour is unknown is drawn with evidence_status unknown.
        """
        index, low, stack, on_stack, counter, cycles = {}, {}, [], set(), [0], []
        for root in nodes:
            if root in index:
                continue
            work = [(root, iter(sorted(outgoing[root])))]
            index[root] = low[root] = counter[0]
            counter[0] += 1
            stack.append(root)
            on_stack.add(root)
            while work:
                node, successors = work[-1]
                advanced = False
                for successor in successors:
                    if successor not in index:
                        index[successor] = low[successor] = counter[0]
                        counter[0] += 1
                        stack.append(successor)
                        on_stack.add(successor)
                        work.append((successor, iter(sorted(outgoing[successor]))))
                        advanced = True
                        break
                    if successor in on_stack:
                        low[node] = min(low[node], index[successor])
                if advanced:
                    continue
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    component = set()
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.add(member)
                        if member == node:
                            break
                    if len(component) > 1 or node in outgoing[node]:
                        cycles.append(component)
        errors = []
        for component in cycles:
            exits = any(target not in component for member in component for target in outgoing[member])
            if not exits and not any(nodes[member].get("completion_condition") for member in component):
                members = ", ".join(sorted(component, key=lambda value: (len(value), value)))
                errors.append(f"Цикл без выхода ({members}): покажите переход, по которому исполнение выходит из цикла "
                              "(пустой результат, break, лимит, остановка), либо условное завершение completion_condition")
        return errors

    def _finish(self, args: dict[str, Any]) -> dict[str, Any]:
        summary = _text(args.get("summary"), "summary", required=True, limit=20000)
        validation = self.validation_signals()
        errors = validation['structural_issues']
        if errors:
            raise MapValidationError("Исправьте карту перед завершением: " + "; ".join(errors[:30]), "finish_blocked")
        partial = bool(validation['coverage_issues'] or self.model["gaps"] or self.model["source_omissions"] or self.model["scenario"]["assumptions"]
                       or any(r["disposition"] == "unavailable" for r in self.model["source_reviews"])
                       or any(r["disposition"] == "out_of_scope" and not r.get("inspected", False)
                              for r in self.model["source_reviews"])
                       or any(row["evidence_status"] != "observed" for key in ("nodes", "edges") for row in self.model[key]))
        status = "partial" if partial else "completed"
        reason = ("Исследование завершено с предположениями, ограничениями или явно обозначенными пробелами."
                  if partial else "Исследование переданных материалов завершено; это не подтверждение семантической полноты или исполнения в рабочей среде.")
        self.model["summary"] = summary
        self.model["run"].update(status=status, reason=reason, termination_kind='normal')
        return {"status": status, "reason": reason, "limitations": list(self.model["limitations"])}
