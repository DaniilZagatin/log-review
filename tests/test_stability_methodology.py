"""Small business results retain fine actions; no model semantics are claimed."""
import copy
import unittest

from pko.stages.inputs import ACTION_PROFILE, build_payload
from test_stability_grouping import ask


def document_map():
    actions = [
        ("step-1", "Получение документа", "Получен документ"),
        ("step-2", "Проверка формата документа", "Формат поддерживается"),
        ("step-3", "Проверка размера документа", "Размер допустим"),
        ("step-4", "Сохранение документа", "Документ сохранён"),
        ("step-5", "Печать документа", "Получен печатный экземпляр"),
        ("step-6", "Отказ в загрузке документа", "Загрузка отклонена")]
    edges = [("step-1", "step-2", ""), ("step-2", "step-3", "Формат поддерживается"),
             ("step-2", "step-6", "Формат не поддерживается"),
             ("step-3", "step-4", "Размер допустим"), ("step-3", "step-6", "Превышен размер"),
             ("step-4", "step-5", "Требуется печатный экземпляр")]
    return {"semantic_profile": ACTION_PROFILE, "schema_version": "2.0", "run": {"status": "partial"},
            "scenario": {"title": "Загрузка и печать документа", "instance_unit": "Один документ заявки",
                         "entry_operation_ids": ["step-1"], "terminal_operation_ids": ["step-5", "step-6"]},
            "nodes": [{"id": identifier, "name": name, "kind": "operation", "output": result}
                      for identifier, name, result in actions],
            "edges": [{"id": f"edge-{index}", "source": source, "target": target, "condition": condition}
                      for index, (source, target, condition) in enumerate(edges, 1)]}


class ActionDepthGroupingTests(unittest.TestCase):
    def test_loading_checks_and_printing_result_keep_their_distinct_scales(self):
        model = document_map()
        before = copy.deepcopy(model)
        answer = {"stages": [
            {"id": "stage-1", "name": "Загрузка документа", "goal": "Сохранить допустимый документ заявки",
             "result": "Документ сохранён либо загрузка отклонена", "boundary": "Получен результат сохранения или отказ",
             "operation_ids": ["step-1", "step-2", "step-3", "step-4", "step-6"]},
            {"id": "stage-2", "name": "Печать документа", "goal": "Получить печатный экземпляр",
             "result": "Документ напечатан", "boundary": "Получен печатный экземпляр",
             "operation_ids": ["step-5"]}], "unassigned": []}
        hierarchy, client = ask(model, answer)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(hierarchy["status"]["result"], "complete")
        self.assertEqual(hierarchy["membership"]["step-2"], hierarchy["membership"]["step-3"])
        self.assertNotEqual(hierarchy["membership"]["step-4"], hierarchy["membership"]["step-5"])
        self.assertEqual(model, before)
        for compact in (False, True):
            payload = build_payload(model, compact=compact)
            self.assertEqual(len(payload["operations"]), 6)
            self.assertEqual([row["condition"] for row in payload["transitions"]],
                             [row["condition"] for row in model["edges"]])


if __name__ == "__main__":
    unittest.main()
