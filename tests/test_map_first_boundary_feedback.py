"""Conditional repair guidance over unrelated Python/Java source; no source execution."""
from copy import deepcopy
import json
import unittest

from process_observability.logging_review import _repair_details, SYSTEM
from process_observability.proposal_placement import validate_placement
from process_observability.logging_contracts import LoggingReviewError
import test_map_first_context_accounting as accounting
from test_map_first_context_reuse import PYTHON, JAVA


class BoundaryFeedbackControls(unittest.TestCase):
    def fixture(self, java=False, code=None):
        packet, ctx, reply, ids = accounting.ContextAccountingControls().fixture(java, code)
        path = 'Library.java' if java else 'Clinic.py'
        line = 13 if java else 8
        reply['context_reviews'] = [
            {'context_call_id': e['context_call_id'], 'use': 'not_relevant', 'reason': 'Другой факт'}
            for e in packet['downstream_context']['delivered']]
        reply['changes'] = [{'id': 'proposed', 'kind': 'add_event', 'placement': 'after',
            'location': {'path': path, 'start_line': line, 'end_line': line},
            'guards': [{'condition': {'path': path, 'start_line': 10 if java else 8,
                                      'end_line': 10 if java else 8}, 'branch': 'false'}],
            'existing_site_ids': []}]
        return packet, ctx, reply, ids

    def conditional(self, packet, ctx, reply):
        return [i for i in _repair_details(reply, packet, ctx)['issues']
                if i['code'] == 'context_existing_site_links' and i.get('applies_when')]

    def test_incomplete_anchor_has_conditional_link_and_remains_rejected(self):
        for java in (False, True):
            with self.subTest(java=java):
                packet, ctx, reply, ids = self.fixture(java)
                before = deepcopy(reply)
                issues = _repair_details(reply, packet, ctx)['issues']
                self.assertIn('invalid_proposal_placement', {i['code'] for i in issues})
                hints = self.conditional(packet, ctx, reply)
                self.assertEqual(len(hints), 1)
                hint = hints[0]
                self.assertEqual(set(hint['site_ids']), ids)
                self.assertEqual(hint['candidate_location']['start_line'], 10 if java else 8)
                self.assertEqual(hint['candidate_location']['end_line'], 13 if java else 10)
                self.assertIn('column', hint['candidate_location'])
                self.assertEqual(reply, before)
                location = ctx.verify_ref(reply['changes'][0]['location'])
                with self.assertRaisesRegex(LoggingReviewError, 'anchor_not_statement_boundary'):
                    validate_placement(reply['changes'][0], location, ctx, {})

    def test_already_linked_site_needs_no_conditional_reminder(self):
        for java in (False, True):
            packet, ctx, reply, ids = self.fixture(java)
            reply['changes'][0]['existing_site_ids'] = sorted(ids)
            self.assertEqual(self.conditional(packet, ctx, reply), [])

    def test_unrelated_intervening_statement_does_not_get_adjacency_hint(self):
        cases = [
            (False, PYTHON.replace('journal', 'logger').replace(
                '    return appointment_details(item.key)', '    marker = 1\n    return appointment_details(item.key)')),
            (True, JAVA.replace('        describe(book.key);', '        int marker = 1;\n        describe(book.key);'))]
        for java, code in cases:
            packet, ctx, reply, _ = self.fixture(java, code)
            self.assertEqual(self.conditional(packet, ctx, reply), [])

    def test_no_delivered_context_does_not_fabricate_links(self):
        packet, ctx, reply, _ = self.fixture()
        packet['downstream_context']['delivered'] = []
        reply['context_reviews'] = []
        self.assertEqual(self.conditional(packet, ctx, reply), [])

    def test_valid_full_anchor_keeps_existing_unconditional_feedback(self):
        for java in (False, True):
            packet, ctx, reply, ids = self.fixture(java)
            reply['changes'][0]['location'].update(start_line=10 if java else 8, end_line=13 if java else 10)
            issues = _repair_details(reply, packet, ctx)['issues']
            self.assertNotIn('invalid_proposal_placement', {i['code'] for i in issues})
            hints = [i for i in issues if i['code'] == 'context_existing_site_links']
            self.assertEqual(len(hints), 1)
            self.assertEqual(set(hints[0]['site_ids']), ids)
            self.assertNotIn('applies_when', hints[0])

    def test_unreachable_anchor_is_not_relocated_by_feedback(self):
        for java in (False, True):
            packet, ctx, reply, _ = self.fixture(java)
            line = 12 if java else 10
            reply['changes'][0]['location'].update(start_line=line, end_line=line)
            reply['changes'][0]['guards'][0]['branch'] = 'true'
            issues = _repair_details(reply, packet, ctx)['issues']
            self.assertTrue(any('unreachable_insertion' in i.get('message', '') for i in issues))
            self.assertEqual(self.conditional(packet, ctx, reply), [])

    def test_binding_examples_do_not_preselect_null_adapter(self):
        for marker in ('{"site_id":"<id>","role":"request"', '{"site_id":"<id>","role":"result"'):
            example, _ = json.JSONDecoder().raw_decode(SYSTEM[SYSTEM.index(marker):])
            self.assertNotIn('source_contract', example)
        self.assertIn('kind=log_call', SYSTEM)
        self.assertIn('если исход не подтверждён другой привязкой, назови', SYSTEM)

    def test_packet_delivers_kind_for_the_prompt_selector_rule(self):
        for java in (False, True):
            packet, _, _, _ = self.fixture(java)
            self.assertTrue(packet['candidates'])
            self.assertTrue(all(isinstance(c.get('kind'), str) for c in packet['candidates']))


if __name__ == '__main__':
    unittest.main()
