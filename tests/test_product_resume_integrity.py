"""Resume binds accepted artifacts to exact saved bytes before model dispatch."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from process_observability.product_state import record_artifact, verify_resume
from process_observability.map_input import MapView


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8', newline='\n')


def map_data():
    return {'schema_version': '2.0', 'scenario': {}, 'sources': [{'id': 'source-1'}],
            'nodes': [{'id': 'save', 'kind': 'operation', 'name': 'Сохранить'}], 'edges': [],
            'run': {'status': 'completed', 'stop_kind': 'finish'}}


class ResumeIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.map = map_data()
        self.manifest = {'artifact_hashes': {}, 'stages': {'map': {'status': 'completed'}},
                         'map_revision': MapView.from_dict(self.map).revision}
        write_json(self.root/'scenario_map.json', self.map)
        record_artifact(self.manifest, self.root, 'scenario_map.json')

    def final(self):
        write_json(self.root/'product_run.json', self.manifest)
        (self.root/'report.html').write_text('<html>Отчёт</html>', encoding='utf-8')
        hashes = {name: hashlib.sha256((self.root/name).read_bytes()).hexdigest()
                  for name in ('scenario_map.json', 'product_run.json', 'report.html')}
        write_json(self.root/'product_checksums.json', hashes)

    def test_complete_and_in_progress_artifacts_verify(self):
        verify_resume(self.root, self.manifest)
        self.final()
        verify_resume(self.root, self.manifest)

    def test_corrupted_published_map_is_rejected_before_reuse(self):
        self.final()
        self.map['nodes'][0]['name'] = 'Чужое действие'
        write_json(self.root/'scenario_map.json', self.map)
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            verify_resume(self.root, self.manifest)

    def test_in_progress_checkpoint_requires_accepted_hash(self):
        path = self.root/'belka/scenario_map.checkpoint.json'
        write_json(path, {'state': 'accepted'})
        with self.assertRaisesRegex(ValueError, 'Unrecorded'):
            verify_resume(self.root, self.manifest)
        record_artifact(self.manifest, self.root, 'belka/scenario_map.checkpoint.json')
        verify_resume(self.root, self.manifest)
        write_json(path, {'state': 'changed'})
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            verify_resume(self.root, self.manifest)

    def test_legacy_incomplete_manifest_is_explicitly_rejected(self):
        self.manifest.pop('artifact_hashes')
        with self.assertRaisesRegex(ValueError, 'legacy'):
            verify_resume(self.root, self.manifest)

    def test_map_revision_must_agree_even_when_hash_was_updated(self):
        self.map['nodes'][0]['name'] = 'Чужое действие'
        write_json(self.root/'scenario_map.json', self.map)
        record_artifact(self.manifest, self.root, 'scenario_map.json')
        with self.assertRaisesRegex(ValueError, 'map revision'):
            verify_resume(self.root, self.manifest)

    def test_missing_declared_artifact_fails(self):
        self.manifest['artifact_hashes']['logging_contracts.json'] = 'a'*64
        with self.assertRaises(ValueError): verify_resume(self.root, self.manifest)

    def test_path_escape_and_secret_names_rejected_before_open(self):
        for name in ('../scenario_map.json', '.env', 'belka/../../scenario_map.json', 'source/app.py'):
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                record_artifact(self.manifest, self.root, name)
        self.manifest['artifact_hashes']['../scenario_map.json'] = 'a'*64
        with self.assertRaisesRegex(ValueError, 'hash entry'):
            verify_resume(self.root, self.manifest)

    def test_final_manifest_is_bound_to_its_checksums(self):
        self.final()
        self.manifest['map_revision'] = 'sha256:forged'
        with self.assertRaisesRegex(ValueError, 'manifest differs'):
            verify_resume(self.root, self.manifest)


class ProducerDurableCallbackTests(unittest.TestCase):
    def test_checkpoint_callback_observes_durable_file_and_halt_propagates(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tests_belka'))
        from scenario_script import SPEC, SCENARIO, ScriptedClient, sources
        from pko.scenario_map.runner import run_scenario
        from pko.scenario_map.checkpoint import load_checkpoint
        class Halt(BaseException): pass
        observed = []
        def callback(path):
            observed.append(load_checkpoint(path))
            raise Halt('publication stopped')
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'PKO_PHOENIX_ENABLED': 'false'}):
            with self.assertRaises(Halt):
                run_scenario(SCENARIO, sources(), Path(folder)/'out', spec=SPEC, client=ScriptedClient(),
                             stages=False, max_steps=1, on_checkpoint_saved=callback)
        self.assertEqual(len(observed), 1)
        self.assertIn('_resume', observed[0])


if __name__ == '__main__': unittest.main()
