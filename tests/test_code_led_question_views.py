"""New saved code-led gaps keep their exact owners in the existing inspector."""
import copy
import unittest

from process_observability.operation_view import operation_views, question_index


def own_bundle():
    question = 'Какой обработчик продолжает обработку после команды?'
    line = 'Разрыв gap-own (unresolved_handoff): ' + question
    return {
        'process_map': {'stages': [{'id': 'st-own', 'name': 'Обработка'}],
                        'operations': [{'id': 'op-own', 'stage_id': 'st-own', 'name': 'Передать команду',
                                        'kind': 'action', 'event_keys': [], 'evidence_ids': []}],
                        'operation_transitions': [], 'event_mappings': [], 'unknowns': [line]},
        'atomic_map': {'schema': 'atomic-map/2',
                       'operations': [{'id': 'op-own', 'stage_id': 'st-own', 'unit_ids': ['unit-own']}],
                       'unit_states': {'unit-own': {'state': 'accounted', 'disposition': 'realized',
                                                   'operation_ids': ['op-own']}},
                       'code_led': {'gaps': [{'id': 'gap-own', 'kind': 'unresolved_handoff',
                                             'operation_ids': ['op-own'], 'unit_ids': ['unit-own'],
                                             'question': question, 'source': 'model'}]}}}


class CodeLedQuestionViews(unittest.TestCase):
    def test_gap_is_addressed_to_its_operation_and_not_duplicated_as_process_unknown(self):
        bundle = own_bundle()
        before = copy.deepcopy(bundle)
        questions = question_index(bundle)
        [gap] = [q for q in questions if 'gap-own' in q['text']]
        self.assertEqual(gap['operation_ids'], ['op-own'])
        self.assertEqual(gap['subject_kind'], 'operation')
        self.assertEqual(gap['subject_id'], 'op-own')
        self.assertEqual(gap['source'], 'code_led_gap')
        views = operation_views(bundle)
        self.assertIn(gap, views['op-own']['questions'])
        self.assertEqual(bundle, before)

    def test_gap_cannot_create_an_owner_from_an_unknown_operation_id(self):
        bundle = own_bundle()
        bundle['atomic_map']['code_led']['gaps'][0]['operation_ids'] = ['foreign-operation']
        questions = question_index(bundle)
        self.assertTrue(questions)
        self.assertFalse(any('foreign-operation' in q['operation_ids'] for q in questions))
        self.assertFalse(any(q['subject_id'] == 'foreign-operation' for q in questions))

    def test_legacy_record_does_not_get_a_fabricated_code_led_owner(self):
        bundle = own_bundle()
        bundle['atomic_map']['schema'] = 'atomic-map/1'
        [gap] = question_index(bundle)
        self.assertEqual(gap['subject_kind'], 'process')
        self.assertEqual(gap['operation_ids'], [])


if __name__ == '__main__':
    unittest.main()
