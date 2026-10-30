"""Resume envelopes reject corruption and configuration drift without credentials."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pko.scenario_map.checkpoint import (
    CheckpointError, checkpoint_scenario, compatibility, load_checkpoint,
    make_checkpoint, validate_checkpoint, validate_compatibility,
)
from pko.scenario_map.schema import MapState
from scenario_script import SPEC
from test_scenario_schema import workspace


class ResumeCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.state = MapState(workspace(1), "Один выбранный вариант")
        self.contract = compatibility(SPEC, "prompt-hash", "tools-hash", "standard", 8192)
        self.payload = make_checkpoint(self.state, self.contract)

    def validate(self, payload=None, spec=SPEC, **kwargs):
        return validate_compatibility(payload or self.payload, spec,
            kwargs.get("system_hash", "prompt-hash"), kwargs.get("tools_hash", "tools-hash"),
            kwargs.get("summary_mode", "standard"), kwargs.get("summary_max_tokens", 8192))

    def test_roundtrip_and_snapshot_are_detached(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "scenario_map.checkpoint.json"
            path.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")
            loaded = load_checkpoint(path)
        self.assertEqual(checkpoint_scenario(loaded), "Один выбранный вариант")
        self.validate(loaded)
        loaded['scenario']['description'] = 'Изменение'
        self.assertEqual(self.state.model['scenario']['description'], "Один выбранный вариант")

    def test_model_and_instruction_configuration_drift_is_rejected(self):
        for spec in (replace(SPEC, model='another'), replace(SPEC, base_url='https://another.invalid'),
                     replace(SPEC, extra_body={'mode':'another'}), replace(SPEC, context_window=12345)):
            with self.subTest(spec=spec.model), self.assertRaises(CheckpointError):
                self.validate(spec=spec)
        for kwargs in ({'system_hash':'new'}, {'tools_hash':'new'}, {'summary_mode':'inherit'}, {'summary_max_tokens':1024}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CheckpointError):
                self.validate(**kwargs)

    def test_credentials_can_rotate_and_are_not_persisted(self):
        self.validate(spec=replace(SPEC, api_key='sk-ROTATED-RESUME-SECRET', client_key='private-new.key', client_cert='renewed.crt'))
        spec = replace(SPEC, api_key='sk-SECRET-RESUME-KEY', base_url='https://user:SECRET-ENDPOINT@host.invalid',
                       extra_body={'api_key':'SECRET-OPTIONS', 'chat_template_kwargs':{'enable_thinking':True}})
        contract = compatibility(spec, 'p', 't', 'standard', 8192)
        serialized = json.dumps(make_checkpoint(self.state, contract))
        for secret in ('SECRET-ENDPOINT', 'SECRET-OPTIONS', 'SECRET-RESUME-KEY', 'enable_thinking'):
            self.assertNotIn(secret, serialized)

    def test_corruption_and_conflicting_public_copy_are_rejected(self):
        for mutate in (lambda p: p['_resume']['state']['counters'].update(step=1),
                       lambda p: p['scenario'].update(title='Different'),
                       lambda p: p['_resume'].update(state_sha256='0'*64),
                       lambda p: p['_resume'].update(version=True),
                       lambda p: p['_resume'].update(version=2)):
            item = deepcopy(self.payload)
            mutate(item)
            with self.assertRaises(CheckpointError): validate_checkpoint(item)

    def test_old_public_snapshot_is_not_a_resume_file(self):
        with self.assertRaises(CheckpointError) as raised:
            validate_checkpoint(self.state.snapshot())
        self.assertIn('--resume', raised.exception.hint)

    def test_file_read_is_bounded_and_truncated_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'checkpoint.json'
            path.write_text('{"unfinished":', encoding='utf-8')
            with self.assertRaises(CheckpointError): load_checkpoint(path)
            path.write_text(json.dumps(self.payload), encoding='utf-8')
            with patch('pko.scenario_map.checkpoint.MAX_CHECKPOINT_BYTES', 10), self.assertRaises(CheckpointError):
                load_checkpoint(path)


if __name__ == '__main__':
    unittest.main()
