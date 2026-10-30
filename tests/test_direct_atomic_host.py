"""Independent post-implementation regressions: source scope, cache boundaries and saved records."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from process_observability import direct_atomic as da
from process_observability.contracts import ProcessMap
from process_observability.onboarding_export import _atomic_accounting, ExportError
from test_direct_atomic import AtomicCase, Business, atomic_handlers, account, fact_by_meaning, finding, HEADER


class AtomicHostChecks(AtomicCase):
    def test_unrelated_shared_context_cannot_lend_result_to_an_operation(self):
        path = 'shop/checkout/actions.py'
        text = HEADER + ('def audit_state(checkout):\n    logger.info("audit.recorded", checkout_id=checkout.pk)\n'
                         '    return True\n\ndef save_cart(checkout):\n    audit_state(checkout)\n'
                         '    checkout.save()\n    return checkout\n')
        found = [finding('entry_point', 'Diagnostic audit outside save operation', path, 6, 8, 'audit_state'),
                 finding('entry_point', 'Save cart command', path, 10, 13, 'save_cart')]

        def reply(brief, attempt):
            [unit] = brief['units']
            if unit['symbol'] == 'audit_state':
                return {'operations': [], 'new_groups': [],
                        'unit_accounting': [account(unit['number'], 'outside_process', reason='Diagnostic audit')],
                        'event_mappings': [], 'operation_transitions': [], 'unknowns': []}
            [site] = [s for s in brief['sites'] if s['event_key'] == 'audit.recorded']
            known = [group['number'] for group in brief['working_groups']]
            return {'operations': [{'name': 'Save cart', 'description': None, 'actor': None, 'kind': 'operation',
                                    'group': known[0] if known else 1, 'initiation': 'api_call',
                                    'initiation_basis': None, 'exit': 'terminal', 'exit_question': None, 'refs': []}],
                    'new_groups': [] if known else [{'name': 'checkout', 'contract': 'Cart commands'}],
                    'unit_accounting': [account(unit['number'], 'realized', [1])],
                    'event_mappings': [{'site': site['number'], 'new_operation': 1, 'known_operation': None,
                                        'lifecycle_role': 'result', 'role_basis': 'Deliberately wrong audit attribution',
                                        'occurrence_rule': 'one per save', 'ambiguity': None,
                                        'refs': [{'path': site['path'], 'start_line': site['start_line'], 'end_line': site['end_line']}]}],
                    'operation_transitions': [], 'unknowns': []}
        business = Business(**atomic_handlers(map_atomic=reply))
        bundle, _ = self.run_case(business, files={path: text}, found=found, atomic_page_units=1, triage='off')
        audit = next(s for s in bundle['atomic_map']['unit_states'].values() if s['symbol'] == 'audit_state')
        # composition derives the boundary fact from the accepted (deliberately wrong) mapping's role
        result = fact_by_meaning(bundle, 'Save cart: result')
        self.assertEqual(audit['disposition'], 'outside_process')
        self.assertNotEqual(result['instrumentation'], 'found')
        self.assertEqual(bundle['process_map']['event_mappings'][0]['role_support'], 'emit_outside_operation_sources')

    def test_missing_business_text_cannot_be_labelled_as_a_business_requirement(self):
        bundle, _ = self.run_case(Business(**atomic_handlers()), business_text='')
        self.assertEqual(bundle['atomic_map']['required_scenarios'], [])  # none from text: the key stays empty
        led = bundle['atomic_map']['code_led']
        self.assertIsNone(led['obligations'])
        self.assertTrue(led['facts'])
        self.assertEqual({row['origin'] for row in led['facts']}, {'compose'})
        self.assertEqual({item['requirement']['state'] for item in bundle['log_coverage']['facts']}, {'proposed'})
        self.assertFalse(any('требование из бизнес-описания' in text for text in bundle['process_map']['unknowns']))

    def test_export_rejects_nested_record_changes_even_if_unit_identities_remain_valid(self):
        bundle, _ = self.run_case(Business(**atomic_handlers()))
        process = ProcessMap.from_dict(bundle['process_map'])
        snapshot_id = bundle['atomic_map']['catalog']['snapshot_id']
        _atomic_accounting(bundle, process, snapshot_id)
        edits = {'mapping role': lambda r: r['event_mappings'][0].update(lifecycle_role='duplicate'),
                 'operation name': lambda r: r['operations'][0].update(name='Wrong invented name'),
                 'attempts type': lambda r: r['requests'][0].update(attempts='not a list'),
                 'review count': lambda r: r['summary'].update(decisions=999999)}
        for label, edit in edits.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(bundle); edit(changed['atomic_map'])
                with self.assertRaises(ExportError):
                    _atomic_accounting(changed, process, snapshot_id)


    def test_commit_failure_preserves_an_exportable_failed_result(self):
        with patch.object(da._Atomic, 'commit', side_effect=ValueError('injected host commit failure')):
            bundle, _ = self.run_case(Business(**atomic_handlers()))
        self.assertIn(bundle['atomic_map']['status'], {'failed', 'partial'})
        self.assertTrue(any(r['outcome'] == 'commit_failed' for r in bundle['atomic_map']['requests']))
        _atomic_accounting(bundle, ProcessMap.from_dict(bundle['process_map']),
                           bundle['atomic_map']['catalog']['snapshot_id'])

    def test_saved_export_checks_the_complete_source_partition_and_source_hashes(self):
        from process_observability.cli import _write_bundle
        from process_observability.onboarding_export import load_run
        bundle, out = self.run_case(Business(**atomic_handlers()))
        bundle['diagnostics'] = {}
        _write_bundle(bundle, out)
        checked = load_run(out)
        self.assertIn('command_units.json', checked['raw'])
        self.assertIn('declaration_index.json', checked['raw'])
        units_file = out / 'command_units.json'
        original = units_file.read_text()
        altered = json.loads(original); altered['units'].pop()
        units_file.write_text(json.dumps(altered))
        with self.assertRaises(ExportError):
            load_run(out)
        units_file.write_text(original)
        index_file = out / 'declaration_index.json'
        index = json.loads(index_file.read_text())
        index['files'][next(iter(index['files']))]['sha256'] = '0' * 64
        index_file.write_text(json.dumps(da.safe_data(index)))
        with self.assertRaises(ExportError):
            load_run(out)


class AtomicCacheChecks(unittest.TestCase):
    def engine(self):
        engine = object.__new__(da._Atomic)
        engine.settings = SimpleNamespace(business_model_fingerprint='configured-business-A', resume_from=None)
        engine.settings_record = {'mode': 'atomic', 'validator': da.VALIDATOR_VERSION}
        engine.snapshot = SimpleNamespace(snapshot_id='s' * 64)
        engine.catalog = {'catalog_sha256': 'c' * 64}
        engine.units_doc = {'units_sha256': 'd' * 64}
        return engine

    def test_cache_identity_binds_actual_business_profile_and_frozen_unit_partition(self):
        engine = self.engine(); original, _ = engine.checkpoint_identity('atomic', 'a' * 64)
        changes = [('model', lambda: setattr(engine.settings, 'business_model_fingerprint', 'configured-business-B')),
                   ('snapshot', lambda: setattr(engine.snapshot, 'snapshot_id', 'f' * 64)),
                   ('units', lambda: engine.units_doc.update(units_sha256='e' * 64)),
                   ('catalog', lambda: engine.catalog.update(catalog_sha256='b' * 64))]
        for label, change in changes:
            with self.subTest(label=label):
                engine = self.engine(); change()
                altered, _ = engine.checkpoint_identity('atomic', 'a' * 64)
                self.assertNotEqual(altered, original)

    def test_prior_input_never_follows_a_symlink_parent(self):
        from process_observability.cli import _map_priors, UserInputError
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); outside=root/'outside'; outside.mkdir()
            (outside/'process_map.json').write_text('{}')
            (root/'link').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(UserInputError):
                _map_priors(str(root/'link'))

    def test_resume_never_reads_through_a_symlink_parent(self):
        engine = self.engine()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); resume = root / 'resume'; resume.mkdir()
            outside = root / 'outside'; outside.mkdir()
            identifier, fields = engine.checkpoint_identity('atomic', 'a' * 64)
            (outside / (identifier + '.json')).write_text('{}')
            folder = resume / da.CHECKPOINTS; folder.parent.mkdir(parents=True); folder.symlink_to(outside, target_is_directory=True)
            engine.settings.resume_from = resume
            read = []; original = Path.read_text
            def watch(path, *args, **kwargs):
                read.append(path); return original(path, *args, **kwargs)
            with patch.object(Path, 'read_text', watch):
                self.assertIsNone(engine.load_checkpoint(identifier, fields))
            self.assertEqual(read, [])
