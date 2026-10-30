"""Model-facing choices remain distinct from cached source evidence and coverage."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import process_observability.logging_review as review
import test_map_first_takeover as adapters
import test_map_first_context_reuse as neighbors
from test_map_first_logging import Scripted


class PrimaryChoiceControls(unittest.TestCase):
    def run_adapter(self, resume=None, inspect_first=False, paste_choice=False):
        helper = adapters.TakeoverControls()
        helper.setUp(); self.addCleanup(helper.doCleanups)
        captured = []
        owner = self
        self.tool_messages = []
        class Capture(Scripted):
            def complete_json(self, system, user):
                payload = json.loads(user)
                captured.append(payload['packet'])
                owner.tool_messages.append(payload['tool_results'])
                if inspect_first and len(captured) == 1:
                    return {'action': 'inspect', 'reads': [
                        {'path': adapters.PORT, 'start_line': 1, 'end_line': 4}]}
                return super().complete_json(system, user)
        with patch.object(adapters, 'Scripted', Capture):
            def paste(row):
                site = next(s for s in captured[-1]['candidates'] if s['id'] == row['site_id'])
                row['source_contract'] = deepcopy(site['logging_chain']['adapter_candidates'][0]['binding_choice']['source_contract'])
            _, result = helper.run_java(resume=resume, mutate=paste if paste_choice else None)
        return captured, result

    def test_pasting_unfilled_choice_is_rejected_instead_of_becoming_an_interpretation(self):
        packets, result = self.run_adapter(paste_choice=True)
        self.assertEqual(result['operations'][0]['state'], 'X')
        self.assertFalse(result['operations'][0]['bindings'])
        rejected = [t for t in result['trace'] if t['event'] == 'validation_rejected']
        self.assertTrue(rejected)
        self.assertTrue(all('source_contract.meaning' in str(t) for t in rejected))

    def test_projection_preserves_reasons_for_other_candidate_statuses(self):
        for status in ('confirmed', 'ambiguous', 'unconfirmed_external'):
            choice = {'status': status, 'reason': 'Existing source-specific reason',
                      'source_contract': {'candidate_id': 'example-contract'}}
            packet = {'candidates': [{'logging_chain': {'adapter_candidates': [choice]}}]}
            visible = review._model_packet(packet)['candidates'][0]['logging_chain']['adapter_candidates'][0]
            self.assertEqual(visible['reason'], choice['reason'])
            self.assertNotIn('Назначение ещё не оценено', visible['binding_instruction'])
            self.assertEqual(choice['status'], status)

    def test_inspect_uses_same_choice_view_while_trace_keeps_host_tools(self):
        packets, result = self.run_adapter(inspect_first=True)
        self.assertEqual(len(packets), 2)
        visible = self.tool_messages[1][0]['result']['logging_calls']['candidates']
        stored = next(t for t in result['trace'] if t['event'] == 'inspection_completed')['result']['logging_calls']['candidates']
        choices = [c for s in visible for c in (s.get('logging_chain') or {}).get('adapter_candidates', [])]
        self.assertTrue(choices)
        self.assertTrue(all('contract_basis' in c and 'source_contract' not in c for c in choices))
        originals = [c for s in stored for c in (s.get('logging_chain') or {}).get('adapter_candidates', [])]
        self.assertTrue(all('source_contract' in c and 'contract_basis' not in c for c in originals))
        self.assertEqual(result['operations'][0]['state'], 'C')

    def test_wrapper_choice_is_visible_at_the_wire_but_saved_evidence_is_unchanged(self):
        packets, result = self.run_adapter()
        candidates = [choice for site in packets[0]['candidates']
                      for choice in (site.get('logging_chain') or {}).get('adapter_candidates', [])]
        self.assertTrue(candidates)
        for choice in candidates:
            self.assertIn('contract_basis', choice)
            self.assertNotIn('source_contract', choice)
            selector = choice['binding_choice']['source_contract']
            self.assertEqual(selector['candidate_id'], choice['contract_basis']['candidate_id'])
            self.assertIn('meaning', selector)
            self.assertIn('если', choice['binding_instruction'].lower())
        self.assertEqual(result['operations'][0]['state'], 'C')
        self.assertTrue(result['operations'][0]['bindings'][0]['chain']['source_contract']['declaration']['quote'])

    def test_projection_does_not_mutate_host_plan_or_turn_a_command_into_a_binding(self):
        basis = {'candidate_id': 'example-choice', 'authority': 'model_interpretation_of_source',
                 'declaration': {'path': 'Warehouse.java', 'start_line': 1, 'end_line': 1},
                 'implementation': {'path': 'Warehouse.java', 'start_line': 2, 'end_line': 4}}
        packet = {'candidates': [{'id': 'counter', 'kind': 'configured_call',
                  'logging_chain': {'status': 'unconfirmed_external', 'adapter_candidates': [
                      {'source_contract': basis, 'reason': 'Only a candidate', 'terminal': {'callee': 'counter.increment'}}]}}]}
        before = deepcopy(packet)
        projected = review._model_packet(packet)
        self.assertEqual(packet, before)
        self.assertNotIn('bindings', projected)
        self.assertEqual(projected['candidates'][0]['logging_chain']['status'], 'unconfirmed_external')
        choice = projected['candidates'][0]['logging_chain']['adapter_candidates'][0]
        choice['contract_basis']['declaration']['path'] = 'forged.java'
        self.assertEqual(packet, before)

    def test_direct_python_and_java_records_have_no_adapter_choice(self):
        for path, code, first, last in [('Clinic.py', neighbors.PYTHON, 7, 10),
                                        ('Library.java', neighbors.JAVA, 9, 13)]:
            packet = neighbors.ContextReuseControls().packet(path, code, first, last)
            wire = review._model_packet(packet)
            for candidate in wire['candidates']:
                if candidate['kind'] == 'log_call':
                    self.assertNotIn('binding_choice', candidate)
                    self.assertFalse((candidate.get('logging_chain') or {}).get('adapter_candidates'))

    def test_downstream_hint_allows_a_selected_prior_fact_without_auto_binding(self):
        packet = neighbors.ContextReuseControls().packet('Clinic.py', neighbors.PYTHON, 7, 10)
        before = deepcopy(packet)
        wire = review._model_packet(packet)
        authority = wire['downstream_context']['authority']
        self.assertNotIn('не доказательство результата операции', authority)
        self.assertIn('context_call_id', authority)
        self.assertIn('успех этого вызова', authority)
        self.assertEqual(wire['downstream_context']['delivered'], packet['downstream_context']['delivered'])
        self.assertNotIn('bindings', wire)
        self.assertEqual(packet, before)

    def test_uninvoked_neighbor_does_not_appear_through_projection(self):
        code = neighbors.PYTHON.replace('return appointment_details(item.key)', 'return item')
        packet = neighbors.ContextReuseControls().packet('Clinic.py', code, 7, 10)
        wire = review._model_packet(packet)
        self.assertFalse(wire['downstream_context']['delivered'])
        self.assertFalse(any(site['start_line'] == 4 for site in wire['candidates']))

    def test_wire_projection_participates_in_cache_identity(self):
        _, first = self.run_adapter()
        original = review._model_packet
        def changed(packet):
            result = original(packet)
            result['presentation_revision'] = 'independent synthetic cache negative control'
            return result
        with patch.object(review, '_model_packet', changed):
            packets, second = self.run_adapter(resume=first)
        self.assertEqual(len(packets), 1)
        self.assertEqual(second['model_requests'], 1)


if __name__ == '__main__':
    unittest.main()
