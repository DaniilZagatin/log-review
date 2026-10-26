"""Research continuity keeps public findings without extending evidence access."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pko.scenario_map.agent import ResearchTools
from pko.scenario_map.research_memory import ResearchMemory
from pko.scenario_map.schema import MapState
from scenario_script import SCENARIO, sources


class FixtureTree:
    def __init__(self):
        self.content = {}
        for number, root in enumerate(sources(), 1):
            for path in root.rglob("*.py"):
                relative = path.relative_to(root).as_posix()
                self.content[f"source-{number}/{relative}"] = path.read_text(encoding="utf-8")
        self.files = list(self.content)

    def read(self, path):
        return self.content.get(path)


class ScenarioMemoryTests(unittest.TestCase):
    def setUp(self):
        self.workspace = SimpleNamespace(
            tree=FixtureTree(), notes=[], extraction=None,
            sources=[{"id": f"source-{number}", "name": root.name, "kind": "directory",
                      "digest": str(number), "file_count": 1}
                     for number, root in enumerate(sources(), 1)],
        )
        self.state = MapState(self.workspace, SCENARIO)
        self.tools = ResearchTools(self.state, self.workspace)
        self.path = "source-1/app.py"

    def call(self, name, **arguments):
        response = self.tools.answer({"id": "test-call", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}})
        self.assertEqual(response["role"], "tool")
        self.assertEqual(response["tool_call_id"], "test-call")
        return json.loads(response["content"])

    def cite(self, line=1):
        return [{"path": self.path, "line_start": line, "line_end": line,
                 "quote": self.workspace.tree.content[self.path].splitlines()[line - 1]}]

    def note(self, **arguments):
        return self.state.dispatch("save_research_note", arguments)

    def inspect(self, section, **arguments):
        response = self.state.dispatch("inspect_map", {"section": section, **arguments})
        self.assertTrue(response["ok"], response)
        return response

    def test_fact_requires_previously_read_literal_evidence(self):
        response = self.note(kind="fact", finding="Найдена точка входа", evidence=self.cite())
        self.assertFalse(response["ok"])
        self.assertEqual(self.state.model["research_notes"], [])
        self.call("read_file", path=self.path, offset=1, limit=1)
        self.assertFalse(self.note(kind="fact", finding="Найдена точка входа")["ok"])
        response = self.note(kind="fact", finding="Найден модуль мобильного канала", evidence=self.cite())
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["id"], "note-1")
        self.assertEqual(self.state.model["research_notes"][0]["evidence"], self.cite())

    def test_hypotheses_and_questions_can_be_saved_before_reading(self):
        for kind in ("hypothesis", "question"):
            response = self.note(kind=kind, finding="Проверить переход в АС заявок",
                                 next_action="Найти получателя запроса")
            self.assertTrue(response["ok"], response)
        notes = self.inspect("research_notes")["items"]
        self.assertEqual(len(notes), 2)
        self.assertTrue(all(note["status"] == "open" for note in notes))
        self.assertTrue(all(note["evidence"] == [] for note in notes))

    def test_same_kind_and_finding_deduplicate_but_update_next_action(self):
        first = self.note(kind="hypothesis", finding="Найти получателя", next_action="Поиск маршрута")
        repeated = self.note(kind="hypothesis", finding="Найти получателя", next_action="Проверить контракт", status="resolved")
        self.assertTrue(repeated["ok"], repeated)
        self.assertEqual(repeated["id"], first["id"])
        self.assertEqual(len(self.state.model["research_notes"]), 1)
        note = self.state.model["research_notes"][0]
        self.assertEqual(note["status"], "resolved")
        self.assertEqual(note["next_action"], "Проверить контракт")

    def test_explicit_id_updates_note_and_unknown_id_rejects(self):
        first = self.note(kind="question", finding="Как обрабатывается отказ?")
        updated = self.note(id=first["id"], kind="hypothesis", finding="Отказ возвращается в канал", status="resolved")
        self.assertTrue(updated["ok"], updated)
        self.assertEqual(updated["id"], first["id"])
        self.assertEqual(len(self.state.model["research_notes"]), 1)
        before = deepcopy(self.state.model["research_notes"])
        rejected = self.note(id="note-999", kind="question", finding="Новая заметка")
        self.assertFalse(rejected["ok"])
        self.assertEqual(self.state.model["research_notes"], before)

    def test_invalid_note_fields_are_rejected_without_changing_saved_notes(self):
        self.note(kind="question", finding="Что проверить?")
        before = deepcopy(self.state.model["research_notes"])
        invalid = [
            {"kind": "thought"}, {"status": "complete"}, {"finding": ""},
            {"finding": "x" * 1501}, {"next_action": "x" * 1001},
            {"reasoning_content": "private"}, {"evidence": "unsupported"},
        ]
        for extra in invalid:
            with self.subTest(extra=next(iter(extra))):
                result = self.note(**{"kind": "question", "finding": "Следующий вопрос", **extra})
                self.assertFalse(result["ok"], result)
                self.assertEqual(self.state.model["research_notes"], before)

    def test_forged_or_unread_evidence_cannot_be_added_to_a_note(self):
        self.call("read_file", path=self.path, offset=1, limit=1)
        forged = self.cite()
        forged[0]["quote"] = "return fabricated_result()"
        for evidence in (forged, self.cite(line=2)):
            result = self.note(kind="fact", finding="Неподтверждённое утверждение", evidence=evidence)
            self.assertFalse(result["ok"])
        self.assertEqual(self.state.model["research_notes"], [])

    def test_note_fields_are_sanitized_on_create_and_update(self):
        with patch.dict("os.environ", {"MODEL_API_KEY": "sensitive-model-token"}):
            first = self.note(kind="question", finding="Проверить sensitive-model-token",
                              next_action="password=hunter123 <think>private chain</think>")
            self.assertTrue(first["ok"], first)
            created = json.dumps(self.state.snapshot())
            self.assertNotIn("sensitive-model-token", created)
            self.assertNotIn("hunter123", created)
            self.assertNotIn("private chain", created)
            updated = self.note(id=first["id"], kind="question", finding="Bearer abcdefghijklmnop",
                                next_action="sensitive-model-token")
        self.assertTrue(updated["ok"], updated)
        saved = json.dumps(self.state.snapshot())
        self.assertNotIn("abcdefghijklmnop", saved)
        self.assertNotIn("sensitive-model-token", saved)

    def test_read_fragments_only_replays_given_lines_without_source_read(self):
        self.call("read_file", path=self.path, offset=1, limit=2)
        self.call("read_file", path=self.path, offset=5, limit=1)
        before = deepcopy(self.state._reads)
        with patch.object(self.workspace.tree, "read", side_effect=AssertionError("Source was reread")):
            first = self.inspect("read_fragments", path=self.path, offset=1, limit=2)
            second = self.inspect("read_fragments", path=self.path, offset=3, limit=2)
        self.assertEqual([line["line"] for line in first["items"]], [1, 2])
        self.assertEqual(first["next_offset"], 5)
        self.assertEqual(second["items"], [{"line": 5, "text": before[self.path][5]}])
        self.assertIsNone(second["next_offset"])
        self.assertEqual(second["total"], 3)
        self.assertEqual(self.state._reads, before)

    def test_replaying_read_fragments_does_not_authorize_unread_lines(self):
        self.call("read_file", path=self.path, offset=1, limit=1)
        replayed = self.inspect("read_fragments", path=self.path, offset=2)
        self.assertEqual(replayed["items"], [])
        self.assertFalse(self.note(kind="fact", finding="Вторая строка", evidence=self.cite(line=2))["ok"])
        self.assertTrue(self.note(kind="fact", finding="Первая строка", evidence=self.cite())["ok"])

    def test_read_fragment_arguments_cannot_access_unread_or_blocked_sources(self):
        self.call("read_file", path=self.path, offset=1, limit=1)
        for arguments in ({}, {"path": "source-2/app.py"}, {"path": "source-1/.env"},
                          {"path": self.path, "limit": 31}, {"path": self.path, "offset": 0},
                          {"path": self.path, "offset": True}):
            result = self.state.dispatch("inspect_map", {"section": "read_fragments", **arguments})
            self.assertFalse(result["ok"], result)
        self.assertFalse(self.state.dispatch("inspect_map", {"section": "research_notes", "path": self.path})["ok"])

    def test_history_recall_keeps_public_response_and_does_not_reopen_source(self):
        original = self.call("read_file", path=self.path, offset=1, limit=2)
        with patch.object(self.workspace.tree, "read", side_effect=AssertionError("Source was reread")):
            history = self.call("inspect_map", section="research_history")
        self.assertTrue(history["ok"], history)
        self.assertEqual(len(history["items"]), 1)
        item = history["items"][0]
        self.assertEqual(item["name"], "read_file")
        self.assertEqual(item["payload"]["content"], original["content"])
        self.assertNotIn("memory", item["payload"])
        self.assertEqual(self.state._reads[self.path].keys(), {1, 2})

    def test_repeated_tool_receipts_are_stable_and_inspection_does_not_record_itself(self):
        args = {"path": self.path, "offset": 1, "limit": 2}
        first = self.call("read_file", **args)
        second = self.call("read_file", **args)
        self.assertFalse(first["memory"]["seen_before"])
        self.assertTrue(second["memory"]["seen_before"])
        self.assertEqual(first["memory"]["observation_id"], second["memory"]["observation_id"])
        self.call("inspect_map", section="research_history")
        self.call("inspect_map", section="summary")
        snapshot = self.state.snapshot()
        self.assertEqual(len(snapshot["research_history"]), 1)
        self.assertEqual(snapshot["research_history"][0]["repeat_count"], 1)
        self.assertNotIn("payload", snapshot["research_history"][0])

    def test_failed_source_read_is_recorded_without_adding_evidence(self):
        result = self.call("read_file", path="source-1/missing.py")
        self.assertFalse(result["ok"])
        self.assertIn("memory", result)
        history = self.inspect("research_history")
        self.assertFalse(history["items"][0]["ok"])
        self.assertEqual(self.state._reads, {})

    def test_history_pages_preserve_source_namespace_and_unique_reads(self):
        for path in (self.path, "source-2/app.py", "source-3/worker.py"):
            self.call("read_file", path=path, offset=1, limit=1)
        first = self.inspect("research_history", offset=1, limit=2)
        second = self.inspect("research_history", offset=first["next_offset"], limit=2)
        self.assertEqual(first["total"], 3)
        self.assertEqual(first["next_offset"], 3)
        self.assertIsNone(second["next_offset"])
        paths = [item["arguments"]["path"] for item in first["items"] + second["items"]]
        self.assertEqual(paths, ["source-3/worker.py", "source-2/app.py", self.path])

    def test_cached_search_result_is_not_literal_evidence(self):
        self.state.research_memory.observe("search", {"pattern": "import"},
                                           {"ok": True, "content": self.cite()[0]["quote"]})
        recalled = self.inspect("research_history")
        self.assertTrue(recalled["items"][0]["payload"]["ok"])
        self.assertFalse(self.note(kind="fact", finding="Импорт найден", evidence=self.cite())["ok"])
        self.assertEqual(self.state._reads, {})

    def test_evicted_cached_payload_is_explicit_but_read_fragments_remain(self):
        self.state.research_memory = ResearchMemory(max_cache_chars=0)
        self.call("read_file", path=self.path, offset=1, limit=1)
        history = self.inspect("research_history")
        self.assertTrue(history["items"][0]["content_unavailable"])
        self.assertNotIn("payload", history["items"][0])
        self.assertEqual(self.inspect("read_fragments", path=self.path)["items"][0]["text"], self.cite()[0]["quote"])

    def test_snapshot_and_inspection_do_not_leak_mutable_memory(self):
        self.call("read_file", path=self.path, offset=1, limit=1)
        self.note(kind="fact", finding="Модуль изучен", evidence=self.cite())
        snapshot = self.state.snapshot()
        snapshot["research_notes"][0]["finding"] = "changed"
        snapshot["research_history"][0]["arguments"]["path"] = "changed"
        history = self.inspect("research_history")
        history["items"][0]["payload"]["content"] = "changed"
        fragments = self.inspect("read_fragments", path=self.path)
        fragments["items"][0]["text"] = "changed"
        self.assertEqual(self.state.model["research_notes"][0]["finding"], "Модуль изучен")
        self.assertEqual(self.state.research_memory.snapshot()[0]["arguments"]["path"], self.path)
        self.assertNotEqual(self.inspect("research_history")["items"][0]["payload"]["content"], "changed")
        self.assertEqual(self.inspect("read_fragments", path=self.path)["items"][0]["text"], self.cite()[0]["quote"])

    def test_inspection_recovers_full_long_note_but_quotes_stay_in_read_fragments(self):
        self.call("read_file", path=self.path, offset=1, limit=1)
        finding_suffix, action_suffix = " — важный итог факта", " — следующий запрос"
        finding = "Ф" * (1400 - len(finding_suffix)) + finding_suffix
        next_action = "Д" * (950 - len(action_suffix)) + action_suffix
        saved = self.note(kind="fact", finding=finding, next_action=next_action, evidence=self.cite())
        self.assertTrue(saved["ok"], saved)
        recovered = self.inspect("research_notes")["items"][0]
        self.assertEqual(recovered["finding"], finding)
        self.assertEqual(recovered["next_action"], next_action)
        self.assertEqual(len(recovered["evidence"]), 1)
        self.assertNotIn("quote", recovered["evidence"][0])
        self.assertEqual(recovered["evidence"][0]["path"], self.path)
        self.assertEqual(self.inspect("read_fragments", path=self.path)["items"][0]["text"], self.cite()[0]["quote"])


if __name__ == "__main__":
    unittest.main()
