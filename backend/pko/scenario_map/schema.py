"""Mutable, evidence-backed map of one scenario as atomic operations.

The model proposes business meaning; this module checks shape, references and
literal provenance. These checks do not prove semantic correctness or that all
relevant source code was supplied. Only lines actually returned by read_file may be
cited. Mutations are transactional, including edits of already linked objects.
"""
from __future__ import annotations

from collections import Counter
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
from pko.scenario_map.code_references import code_only, symbols

SCHEMA_VERSION = "2.0"
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
INSPECT_SECTIONS = ("summary", "scenario", "nodes", "edges", "gaps", "sources", "source_reviews", "execution_reviews", "operations", "read_coverage", "read_fragments", "research_history", "research_notes", "research_brief", "context_summary", "research_tasks", "semantic_review", "frontier", "validation")
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

    def __init__(self, message: str, code: str = "invalid_arguments"):
        super().__init__(message)
        self.code = code


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


class MapState(ResearchQuality):
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
            # The process description is bounded by the model window, checked before research.
            "scenario": {"description": _text(scenario_description, "scenario_description", required=True, limit=None),
                         "title": "", "process_name": "", "coverage": "", "start": "", "end": "", "exclusions": [], "assumptions": [], "entry_operation_ids": [], "terminal_operation_ids": []},
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
        result["validation"] = {"issues": self._finish_errors(), "semantic_verification": False}
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
            "set_scope": {"title", "process_name", "coverage", "start", "end", "exclusions", "assumptions", "entry_operation_ids", "terminal_operation_ids"},
            "upsert_step": {"id", "name", "kind", "description", "input", "output", "condition", "evidence_status", "evidence", "outcome", "basis", "code_explanations", "effect_kind", "continuation_mode", "join_mode", "external_interactions", "completion_condition"},
            "upsert_transition": {"id", "source", "target", "kind", "description", "condition", "data", "correlation", "evidence_status", "evidence", "sender_evidence", "receiver_evidence", "channel", "contract", "basis", "code_explanations"},
            "add_gap": {"description", "node_ids", "edge_ids", "task_ids", "category", "evidence"},
            "review_source": {"source_id", "disposition", "reason"},
            "review_execution": {"id", "name", "evidence", "operations", "checks"},
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
            errors = self._reference_errors()
            if errors:
                raise MapValidationError("; ".join(errors))
            if name != "finish":
                self.model["run"].update(status="running", reason="")
            hints = self._hints(name, result)
            return {"ok": True, **result, **({"hints": hints} if hints else {})}
        except (MapValidationError, TypeError, KeyError, ValueError) as exc:
            self.model = original
            self._counters = counters
            code = getattr(exc, "code", None) or ("quality_check" if isinstance(exc, QualityError) else "invalid_arguments")
            return {"ok": False, "code": code, "error": str(exc), **({"details": exc.details} if isinstance(exc, QualityError) else {})}

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

    BRANCH = re.compile(r"^\s*(?:\}\s*)?(?:else\s+)?if\b|^\s*(?:switch|when|match)\b|^\s*case\b|^\s*elif\b|^\s*default\s*:")
    # Too common to identify one executor across operations.
    GENERIC_SYMBOLS = frozenset({"execute", "run", "process", "handle", "call", "apply", "invoke", "main", "send",
                                 "get", "post", "put", "build", "create", "close", "update", "save", "init"})
    CODE_WORDS = re.compile(r"(?<![а-яё])(?:обработчик\w*|стратеги\w*|метод\w*|класс\w*|фасад\w*)", re.IGNORECASE)

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
        return (f"{node['id']}: основания охватывают {', '.join(p for p in parts if p)}, которые не показаны другими операциями. "
                "Каждый шаг процесса должен быть отдельной операцией: если здесь несколько проверок, вызовов, расчётов "
                "или результатов, раздели эту операцию и свяжи части переходами.")

    def _shared_executor_hints(self, node: dict[str, Any]) -> list[str]:
        """Advisory: the node's own code calls an executor through which another
        operation performs an external action. If this node performs it too,
        that action is a separate step; one implementation may serve several."""
        own = node.get("evidence", [])
        source = code_only("\n".join(self._reads.get(ref["path"], {}).get(line, "") for ref in own
                                     for line in range(ref["line_start"], ref["line_end"] + 1)))
        declared = symbols(source)
        systems = {row.get("name", "").casefold() for row in node.get("external_interactions", [])}
        hints = []
        for other in self.model["nodes"]:
            rows = other.get("external_interactions") or []
            if other["id"] == node["id"] or not rows or systems & {row.get("name", "").casefold() for row in rows}:
                continue
            refs = [ref for row in rows for ref in row["evidence"]]
            if any(self._overlap(a, b) for a in refs for b in own):
                continue
            names = {ref["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0] for ref in refs}
            names = {name for name in names if name[:1].isupper()}
            names |= {row["symbol"] for row in other.get("code_explanations", [])
                      if row.get("kind") == "function" and len(row.get("symbol", "")) >= 4}
            called = sorted(name for name in names - self.GENERIC_SYMBOLS - declared
                            if re.search(r"\b" + re.escape(name) + r"\s*[.(]", source))
            if called:
                system = next((row.get("name") for row in rows if row.get("name")), "внешняя система")
                hints.append(f"{node['id']}: основания вызывают {', '.join(called[:3])}; через это {other['id']} "
                             f"«{other['name']}» обращается к внешней системе ({system}). Если {node['id']} тоже выполняет "
                             "это действие, покажи его отдельной операцией в фактическом порядке вызовов; одна реализация "
                             "может участвовать в нескольких операциях карты.")
        return hints[:3]

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
            hints.extend(self._shared_executor_hints(node))
            words = self.CODE_WORDS.search(node.get("name", ""))
            if words:
                hints.append(f"{node['id']}: название описывает устройство кода («{words.group(0)}»), а не шаг процесса; "
                             "назови действие и его предмет, а имена реализаций оставь в основаниях.")
            if re.search(r"\s(?:и|или|а также)\s", node.get("name", "")):
                hints.append(f"{node['id']}: в названии несколько действий через «и»/«или»; если это отдельные шаги процесса, "
                             "сделай из них отдельные операции.")
            if (node.get("continuation_mode") == "choice" and node.get("outcome", "unknown") != "unknown"
                    and not (node['id'] in terminals and node.get('completion_condition'))):
                hints.append(f"{node['id']}: у операции с выбором продолжения нет единственного исхода; outcome не указывай, "
                             "исходы выражаются переходами к операциям, формирующим результат.")
        if name in ("set_scope", "upsert_step"):
            unresolved = [identifier for identifier in sorted(terminals) if identifier in nodes
                          and nodes[identifier].get('outcome', 'unknown') == 'unknown']
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
                    hints.extend(self._shared_executor_hints(nodes[operation["node_id"]]))
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
                "counts": counts, "validation": {"issues": self._finish_errors()},
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
            rows = self._finish_errors()
        elif section == "operations":
            rows = [{**operation, "review_id": review["id"]}
                    for review in self.model["execution_reviews"] for operation in review["operations"]]
        else:
            rows = self.model[section]
        page, size = [], 0
        for row in rows[offset - 1:offset - 1 + limit]:
            # Notes already have strict field limits. Do not lose the latter
            # half of a pending task when retrieving it after compaction.
            if section in {"research_notes", "research_tasks"}:
                item = deepcopy(row)
                item["evidence"] = [{k: v for k, v in ref.items() if k != "quote"}
                                    for ref in item["evidence"]]
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
        self.model["scenario"].update({
            "title": _text(args.get("title"), "title", required=True, limit=300),
            "process_name": _text(args.get("process_name", ""), "process_name", limit=300),
            # The header and overview show these to the process owner, so they are business text.
            "coverage": self.business_text(args["coverage"], "coverage", 1000) if args.get("coverage") else "",
            "start": self.business_text(args.get("start"), "start", MAX_TEXT),
            "end": self.business_text(args.get("end"), "end", MAX_TEXT),
            "exclusions": [self.business_text(value, "exclusions", 1000)
                           for value in _strings(args.get("exclusions", []), "exclusions")],
            "assumptions": _strings(args.get("assumptions", []), "assumptions"),
            **{key: _strings(args.get(key, self.model["scenario"][key]), key, limit=MAX_NODES) for key in ("entry_operation_ids", "terminal_operation_ids")},
        })
        return {"scenario": deepcopy(self.model["scenario"])}

    def _upsert_step(self, args: dict[str, Any]) -> dict[str, Any]:
        status, evidence = self._support(args)
        item = {"id": self._id("nodes", "step", args.get("id"), MAX_NODES),
                "name": self.business_text(args.get("name"), "name", 300),
                "kind": _choice(args.get("kind", "operation"), "kind", STEP_KINDS),
                "evidence_status": status, "evidence": evidence,
                "outcome": _choice(args.get("outcome") or "unknown", "outcome", OUTCOME_KINDS),
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
        # Never silently delete incident transitions or children. The caller can
        # amend them first, and failed removals leave all data untouched.
        self.model[collection] = [row for row in self.model[collection] if row["id"] != identifier]
        return {"removed": identifier}

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

    def _review_execution(self, args: dict[str, Any]) -> dict[str, Any]:
        """Record a code-anchored inventory, not an assertion of total coverage.

        Independent effects require independent node IDs. A review may span
        helper functions: functions are not automatically separate operations.
        No natural-language heuristic pretends to prove that an action is atomic.
        """
        identifier = self._id("execution_reviews", "review", args.get("id"), MAX_NODES)
        evidence = self._evidence(args.get("evidence", []))
        if not evidence:
            raise MapValidationError("review_execution требует прочитанные основания обследованного исполнения")
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
            if node_id not in nodes:
                raise MapValidationError(f"{node_id}: операция должна ссылаться на существующий узел")
            if node_id in used_nodes:
                raise MapValidationError(f"{node_id}: несколько действий нельзя свернуть в одну операцию; создайте отдельные узлы")
            used_nodes.add(node_id)
            citations = self._evidence(raw.get("evidence", []), "operations.evidence")
            if not citations:
                raise MapValidationError(f"{node_id}: нужны основания операции")
            if not any(self._overlap(a, b) for a in citations for b in nodes[node_id]["evidence"]):
                raise MapValidationError(f"{node_id}: основания операции не относятся к основаниям узла")
            self.require_coverage(node_id, "operations.evidence", citations, evidence)
            self.require_coverage(node_id, "node.evidence", nodes[node_id]["evidence"], evidence)
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
                "evidence": evidence, "operations": operations, "checks": checks,
                "node_fingerprints": {n: self._fingerprint(nodes[n]) for n in related_nodes if n in nodes},
                "edge_fingerprints": {e: self._fingerprint(edge_index[e]) for e in related_edges if e in edge_index}}
        result = self._put("execution_reviews", item)
        return {**result, "operations": [{"id": op["id"], "node_id": op["node_id"]} for op in operations]}

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
            reviewed = {op["node_id"] for op in review["operations"]}
            for op in review["operations"]:
                if op["node_id"] not in nodes:
                    errors.append(f"{op['id']}: запись обзора ссылается на отсутствующую операцию карты")
                if op["node_id"] in assigned:
                    errors.append(f"{op['node_id']}: несколько операций не могут быть скрыты в одном узле")
                assigned[op["node_id"]] = op["id"]
            for category, check in review["checks"].items():
                if set(check["node_ids"]) - set(nodes) or set(check["edge_ids"]) - set(edges) or set(check["gap_ids"]) - set(gaps):
                    errors.append(f"{review['id']}.{category}: неизвестные ссылки узлов, переходов или пробелов")
                    continue
                if set(check["node_ids"]) - reviewed:
                    errors.append(f"{review['id']}.{category}: узлы проверки не входят в перечень обследованных операций")
                if any(edges[e]["source"] not in reviewed and edges[e]["target"] not in reviewed for e in check["edge_ids"]):
                    errors.append(f"{review['id']}.{category}: переход не относится к обследованным операциям")
                if check["status"] == "unknown" and not any(
                    reviewed.intersection(gaps[g]["node_ids"]) or
                    any(edges[e]["source"] in reviewed or edges[e]["target"] in reviewed for e in gaps[g].get("edge_ids", []))
                    for g in check["gap_ids"]):
                    errors.append(f"{review['id']}.{category}: gap должен относиться к обследованным операциям")
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
        return {n["id"] for n in self.model["nodes"] if n["evidence_status"] != "unknown"}

    def _detail_errors(self) -> list[str]:
        """Coverage of an explicit inventory, never proof of discovery recall."""
        errors: list[str] = []
        reviews = self.model.get("execution_reviews", [])
        nodes = {node["id"]: node for node in self.model["nodes"]}
        edges = {edge["id"]: edge for edge in self.model["edges"]}
        operations = [op for review in reviews for op in review["operations"]]
        mapped = {op["node_id"] for op in operations}
        required = self._reviewable_operations()
        unknown_operations = {n["id"] for n in nodes.values() if n["kind"] in OPERATION_NODE_KINDS} - required
        if (required and (not reviews or not any(op["node_id"] in required for op in operations))) or (not required and not unknown_operations):
            errors.append("Детализация не проверена: вызовите review_execution с атомарными операциями; каждое самостоятельное действие требует своей операции")
        for identifier in sorted(required - mapped):
            errors.append(f"{identifier}: операция не сопоставлена с записью review_execution")
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
                    errors.append(f"{review['id']}: операция {identifier} изменена после обзора; обновите review_execution")
            for identifier, fingerprint in review["edge_fingerprints"].items():
                if identifier in edges and self._fingerprint(edges[identifier]) != fingerprint:
                    errors.append(f"{review['id']}: переход {identifier} изменён после обзора; обновите review_execution")
            for operation in review["operations"]:
                node = nodes.get(operation["node_id"])
                if node and not self._within(node["evidence"], review["evidence"]):
                    errors.append(f"{operation['id']}: основания узла выходят за обследованную область")
            present = {
                "branches": (any(sum(e["source"] == n and e["kind"] != "data" for e in incident) > 1 for n in review_nodes)
                             or any(e["kind"] == "branch" for e in incident)),
                "failures": "error_handling" in kinds or any(e["kind"] in ("error", "retry") for e in incident),
                "integrations": bool(kinds.intersection({"integration", "publish"})) or any(
                    e["kind"] in ("call", "event", "return") or
                    (e["source"] in nodes and bool(nodes[e["source"]].get("external_interactions")))
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
        errors = self._finish_errors()
        if errors:
            raise MapValidationError("Исправьте карту перед завершением: " + "; ".join(errors[:30]), "finish_blocked")
        partial = bool(self.model["gaps"] or self.model["source_omissions"] or self.model["scenario"]["assumptions"]
                       or self.model["scenario"].get("coverage")
                       or any(r["disposition"] == "unavailable" for r in self.model["source_reviews"])
                       or any(r["disposition"] == "out_of_scope" and not r.get("inspected", False)
                              for r in self.model["source_reviews"])
                       or any(row["evidence_status"] != "observed" for key in ("nodes", "edges") for row in self.model[key]))
        status = "partial" if partial else "completed"
        reason = ("Исследование завершено с предположениями, ограничениями или явно обозначенными пробелами."
                  if partial else "Исследование переданных материалов завершено; это не подтверждение семантической полноты или исполнения в рабочей среде.")
        self.model["summary"] = summary
        self.model["run"].update(status=status, reason=reason)
        branching = Counter(edge["source"] for edge in self.model["edges"] if edge.get("kind") != "data")
        if not self.model["research_tasks"] and any(count > 1 for count in branching.values()):
            # Not a gate: a minimum number of tasks would only invite placeholders.
            note = ("Реестр задач исследования пуст: раскрытие вызванных обработчиков и ветвей "
                    "не прослежено по задачам, полноту участков оценивает специалист.")
            if note not in self.model["limitations"]:
                self.model["limitations"].append(note)
        return {"status": status, "reason": reason, "limitations": list(self.model["limitations"])}
