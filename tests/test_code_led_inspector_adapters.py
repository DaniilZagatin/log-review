"""Code-led ownership and accepted provenance survive the presentation adapters."""
import copy
import unittest
from types import SimpleNamespace

from process_observability.operation_view import card_links, operation_views
from process_observability.operation_hierarchy import hierarchy_view
from process_observability.scenario_graph import _routes_section
from process_observability.scenario_graph import operation_status
from process_observability.report import _Run
from test_code_led_question_views import own_bundle
from test_operation_hierarchy import basket


def unlogged_fact():
    bundle = own_bundle()
    bundle['atomic_map']['code_led']['facts'] = [
        {'id': 'fact-own', 'stage_id': 'st-own', 'operation_ids': ['op-own']}]
    bundle['log_coverage'] = {'facts': [
        {'id': 'fact-own', 'stage_id': 'st-own', 'meaning': 'Команда передана',
         'instrumentation': 'missing', 'lifecycle_roles': [], 'event_keys': []}]}
    bundle['instrumentation_plan'] = {'cards': [
        {'id': 'card-own', 'fact_id': 'fact-own', 'action': 'add_event', 'location': 'location_unresolved'}]}
    return bundle


def derived_compose():
    bundle = basket()
    request = bundle['atomic_map']['requests'][0]
    request.update(outcome='accepted_after_local_revalidation',
                   guidance_revalidation={'schema': 'compose-guidance-omission/1', 'after_attempt': 2,
                                          'model_calls': 0, 'outcome': 'accepted', 'final': None,
                                          'api_request_sha256': 'a' * 64, 'raw_response_sha256': 'b' * 64,
                                          'sanitized_response_sha256': 'c' * 64},
                   accepted={'attempt': 2, 'api_request_sha256': 'a' * 64,
                             'raw_response_sha256': 'b' * 64, 'response_sha256': 'c' * 64,
                             'derivation': 'local_guidance_omission'})
    return bundle


class CodeLedInspectorAdapters(unittest.TestCase):
    def test_unlogged_fact_and_change_keep_the_exact_code_led_operation_owner(self):
        bundle = unlogged_fact()
        before = copy.deepcopy(bundle)
        view = operation_views(bundle)['op-own']
        self.assertEqual([f['id'] for f in view['facts']], ['fact-own'])
        self.assertEqual(view['facts'][0]['instrumentation'], 'missing')
        self.assertFalse(view['logs'])
        self.assertEqual(card_links(bundle)['card-own']['operation_ids'], ['op-own'])
        self.assertEqual(bundle, before)

    def test_foreign_operation_or_conflicting_stage_cannot_own_the_fact(self):
        for change in (lambda row: row.update(operation_ids=['foreign']),
                       lambda row: row.update(stage_id='foreign')):
            bundle = unlogged_fact()
            change(bundle['atomic_map']['code_led']['facts'][0])
            self.assertFalse(operation_views(bundle)['op-own']['facts'])
            self.assertFalse(card_links(bundle)['card-own']['operation_ids'])

    def test_legacy_schema_does_not_infer_unlogged_ownership(self):
        bundle = unlogged_fact()
        bundle['atomic_map']['schema'] = 'atomic-map/1'
        self.assertFalse(operation_views(bundle)['op-own']['facts'])

    def test_shared_found_fact_does_not_colour_an_incompatible_emit_as_covered(self):
        bundle = unlogged_fact()
        op = bundle['process_map']['operations'][0]
        op.update(evidence_ids=['ev-own'], event_keys=['shared.key'])
        bundle['evidence'] = [{'id': 'ev-own', 'kind': 'code', 'text_validation': 'verified'}]
        bundle['log_coverage']['facts'][0].update(instrumentation='found', event_keys=['shared.key'])
        self.assertEqual(operation_status(_Run(bundle), op), 'code_only')

    def test_unowned_change_retains_a_source_overlap_candidate_without_claiming_ownership(self):
        bundle = unlogged_fact()
        bundle['atomic_map']['code_led']['facts'][0]['operation_ids'] = []
        anchor = {'path': 'own.py', 'start_line': 1, 'end_line': 3}
        bundle['evidence'] = [{'id': 'ev-own', 'source_anchor': anchor}]
        bundle['process_map']['operations'][0]['evidence_ids'] = ['ev-own']
        bundle['instrumentation_plan']['cards'][0].update(location='anchored', source_anchor=anchor)
        links = card_links(bundle)['card-own']
        self.assertEqual(links['operation_ids'], [])
        self.assertEqual(links['candidates'], [{'operation_id': 'op-own', 'linkage': 'source_overlap'}])

    def test_unsupported_or_contradicted_owned_mapping_cannot_lend_green_colour(self):
        bundle = unlogged_fact()
        op = bundle['process_map']['operations'][0]
        op.update(evidence_ids=['ev-own'], event_keys=['shared.key'])
        bundle['evidence'] = [{'id': 'ev-own', 'kind': 'code', 'text_validation': 'verified'}]
        bundle['log_coverage']['facts'][0].update(instrumentation='found', event_keys=['shared.key'])
        mapping = {'operation_id': 'op-own', 'stage_id': 'st-own', 'event_key': 'shared.key',
                   'lifecycle_role': 'result', 'role_support': 'emit_within_operation_sources',
                   'evidence_ids': []}
        bundle['process_map']['event_mappings'] = [mapping]
        bundle['atomic_map']['event_mappings'] = [dict(mapping, path='own.py', start_line=2,
                                                      end_line=2, fact_ids=['fact-own'])]
        self.assertEqual(operation_status(_Run(bundle), op), 'code_logs')
        for edit in (lambda b: b['process_map']['event_mappings'][0].update(
                         role_support='emit_outside_operation_sources'),
                     lambda b: b['process_map']['event_mappings'][0].update(reconciliation='contradicted'),
                     lambda b: b.update(coverage_lifecycle={'sites': [
                         {'path': 'own.py', 'start_line': 2, 'end_line': 2,
                          'event_key': 'shared.key', 'status': 'unsupported'}]})):
            broken = copy.deepcopy(bundle)
            edit(broken)
            self.assertEqual(operation_status(_Run(broken), broken['process_map']['operations'][0]), 'code_only')

    def test_new_transition_bases_render_as_source_evidence(self):
        run = SimpleNamespace(_r08_inspection={'numbers': {}, 'views': {}})
        for basis, label in [('source_call', 'по вызову в коде'),
                             ('source_handoff', 'по передаче управления или данных в коде')]:
            with self.subTest(basis=basis):
                text = _routes_section(run, {'next': [{'operation_id': 'next', 'condition': None,
                                                       'basis': basis, 'basis_note': None,
                                                       'evidence_ids': []}]}, 'next')
                self.assertIn(label, text)
                self.assertNotIn('основание порядка не указано', text)

    def test_derived_accepted_composition_keeps_its_navigation_groups(self):
        self.assertEqual([g['id'] for g in hierarchy_view(derived_compose())['groups']], ['basket'])

    def test_rejected_or_unbound_derivation_cannot_create_a_group(self):
        for change in (lambda r: r['guidance_revalidation'].update(outcome='rejected'),
                       lambda r: r['accepted'].update(response_sha256='d' * 64),
                       lambda r: r['accepted'].update(attempt=1),
                       lambda r: r.pop('guidance_revalidation')):
            bundle = derived_compose()
            change(bundle['atomic_map']['requests'][0])
            result = hierarchy_view(bundle)
            self.assertFalse(result['groups'])
            self.assertEqual(len(result['nodes']), 5)


if __name__ == '__main__':
    unittest.main()
