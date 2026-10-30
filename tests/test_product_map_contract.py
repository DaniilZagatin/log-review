"""Producer-consumer contract checks for current action maps and frozen sources."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pko.scenario_map.sources import build_workspace
from pko.stages.grouping import unavailable_hierarchy
from pko.stages.inputs import map_revision
from process_observability.map_input import MapView, MapInputError, load_hierarchy, snapshot_from_workspace
from process_observability.map_presentation.compat import map_revision as presentation_revision, validate_hierarchy


def action_map():
    return {'schema_version': '2.0', 'semantic_profile': 'variant-actions-v1',
            'scenario': {'title': 'Завершение заявки', 'entry_operation_ids': ['save'],
                         'terminal_operation_ids': ['save'], 'instance_unit': 'Заявка'},
            'sources': [{'id': 'source-1'}], 'nodes': [{'id': 'save', 'kind': 'operation',
            'name': 'Сохранить заявку', 'outcome': 'success', 'outcome_scope': 'branch'}],
            'edges': [], 'gaps': [], 'run': {'id': 'r1', 'status': 'completed'}}


class ProductMapContractTests(unittest.TestCase):
    def test_same_profile_aware_revision_at_all_boundaries(self):
        current = action_map()
        self.assertEqual(MapView.from_dict(current).revision, map_revision(current))
        self.assertEqual(presentation_revision(current), map_revision(current))
        legacy = deepcopy(current); legacy.pop('semantic_profile')
        self.assertNotEqual(map_revision(legacy), map_revision(current))
        self.assertNotIn('semantic_profile', MapView.from_dict(legacy).original)

    def test_unknown_profile_and_checkpoint_are_rejected(self):
        for key, value in [('semantic_profile', 'future-actions'), ('_resume', {})]:
            model = action_map(); model[key] = value
            with self.assertRaises(MapInputError): MapView.from_dict(model)

    def test_action_profile_has_real_boundaries_and_shared_outcomes(self):
        model = action_map(); model['scenario']['terminal_operation_ids'] = ['ghost']
        with self.assertRaises(MapInputError): MapView.from_dict(model)
        model = action_map(); model['nodes'][0]['outcome_scope'] = 'shared'
        with self.assertRaises(MapInputError): MapView.from_dict(model)
        model['nodes'][0]['outcome'] = 'unknown'
        self.assertEqual(MapView.from_dict(model).original, model)

    def test_current_hierarchy_accepted_and_wrong_profile_rejected(self):
        model = action_map(); view = MapView.from_dict(model)
        hierarchy = unavailable_hierarchy(model, 'Группировка не выполнялась')
        validate_hierarchy(hierarchy, model)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'map_hierarchy.json'
            path.write_text(json.dumps(hierarchy), encoding='utf-8')
            loaded, warnings = load_hierarchy(path, view)
            self.assertIsNotNone(loaded); self.assertFalse(warnings)
            hierarchy['semantic_profile'] = 'future-actions'
            path.write_text(json.dumps(hierarchy), encoding='utf-8')
            loaded, warnings = load_hierarchy(path, view)
            self.assertIsNone(loaded); self.assertTrue(warnings)

    def test_snapshot_adapter_never_reopens_changed_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root/'repo'; source.mkdir()
            (source/'app.py').write_text('print("first")\n', encoding='utf-8')
            workspace = build_workspace([source], root/'snapshot')
            (source/'app.py').write_text('print("changed")\n', encoding='utf-8')
            (root/'snapshot/source-1/app.py').write_text('print("also changed")\n', encoding='utf-8')
            snapshot = snapshot_from_workspace(workspace)
            text = workspace.tree.read('source-1/app.py')
            self.assertEqual(snapshot.files['source-1/app.py']._raw_text, text)
            self.assertEqual(snapshot.fingerprints['source-1/app.py'], hashlib.sha256(text.encode()).hexdigest())
            self.assertIn('first', snapshot.files['source-1/app.py'].text)

    def test_same_name_in_two_sources_stays_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); sources = [root/'one', root/'two']
            for index, source in enumerate(sources):
                source.mkdir(); (source/'app.py').write_text(f'value = {index}\n', encoding='utf-8')
            workspace = build_workspace(sources, root/'snapshot')
            snapshot = snapshot_from_workspace(workspace)
            self.assertEqual(set(snapshot.files), {'source-1/app.py', 'source-2/app.py'})
            self.assertNotEqual(*snapshot.fingerprints.values())

    def test_conditional_terminal_preserved_and_invalid_continuation_rejected(self):
        model = action_map()
        model['nodes'][0]['completion_condition'] = 'Заявка принята'
        model['edges'] = [{'id': 'retry', 'source': 'save', 'target': 'save', 'condition': 'Повторить'}]
        self.assertEqual(MapView.from_dict(model).original, model)
        del model['nodes'][0]['completion_condition']
        with self.assertRaises(MapInputError): MapView.from_dict(model)


if __name__ == '__main__':
    unittest.main()
