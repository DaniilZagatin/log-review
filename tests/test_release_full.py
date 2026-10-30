"""Standalone release: no baseline, private reads, symlink traversal or overwrite."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/release_full.py'


def api():
    spec=importlib.util.spec_from_file_location('full_release',SCRIPT)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class FullReleaseTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.tmp=Path(temp.name).resolve();self.root=self.tmp/'source';self.root.mkdir()
        self.files={'run.py':'print("own app")\n','pyproject.toml':'[project]\nname="example"\n',
                    'README.md':'# Standalone\n','AGENT-START.md':'# Start here\n',
                    'src/process_observability/__init__.py':'','tests/test_demo.py':'# own test\n',
                    'tests/__init__.py':'',
                    'src/process_observability/map_presentation/__init__.py':'# renderer package\n',
                    'src/process_observability/map_presentation/scenario_map.py':'# renderer\n',
                    'src/process_observability/map_presentation/assets/dpss/SBSansDisplay-400.woff2':'fixture font',
                    'src/process_observability/map_presentation/assets/horizon/tokens.css':':root{}',
                    'guidance/process-observability/SKILL.md':'# Method\n',
                    'examples/synthetic/business.md':'Synthetic business\n'}
        for rel,text in self.files.items():
            p=self.root/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        for folder in ('config','certs','inputs','materials','references','runs','.local','.git'):
            p=self.root/folder/'PRIVATE';p.parent.mkdir(parents=True,exist_ok=True);p.write_text('DO-NOT-READ')

    def test_standalone_payload_and_hashes_without_baseline_or_private_reads(self):
        m=api();original=Path.read_bytes
        def guarded(path):
            if path.is_relative_to(self.root) and path.relative_to(self.root).parts[0] in {'config','certs','inputs','materials','references','runs','.local','.git'}:
                self.fail('private payload read')
            return original(path)
        out=self.tmp/'full.zip'
        with patch.object(Path,'read_bytes',guarded):
            receipt=m.build(self.root,out)
        self.assertTrue(out.is_file());self.assertEqual(receipt['kind'],'standalone')
        checked=m.verify(out)
        self.assertEqual(checked['files'],receipt['files'])
        self.assertNotIn('baseline_sha256',checked)
        with zipfile.ZipFile(out) as archive:
            names=set(archive.namelist());prefix=m.PREFIX
            self.assertTrue({prefix+p for p in self.files}<=names)
            self.assertTrue({prefix+'RELEASE-MANIFEST.json',prefix+'SHA256SUMS',prefix+'config/model.example.json'}<=names)
            for name in names:
                self.assertNotIn('PRIVATE',name)
                self.assertNotIn(b'DO-NOT-READ',archive.read(name))
            config=json.loads(archive.read(prefix+'config/model.example.json'))
            self.assertEqual(set(config),{'code','business'})
            self.assertTrue(all(role['base_url'].endswith('.invalid/v1') for role in config.values()))

    def test_refuses_output_overwrite_and_is_reproducible(self):
        m=api();a=self.tmp/'one.zip';b=self.tmp/'two.zip'
        m.build(self.root,a);m.build(self.root,b);self.assertEqual(a.read_bytes(),b.read_bytes())
        before=a.read_bytes()
        with self.assertRaises(m.ReleaseError):m.build(self.root,a)
        self.assertEqual(a.read_bytes(),before)

    def test_rejects_symlink_in_selected_source_and_tampered_zip(self):
        m=api();target=self.tmp/'outside.py';target.write_text('must not copy')
        (self.root/'src/process_observability/link.py').symlink_to(target)
        with self.assertRaises(m.ReleaseError):m.build(self.root,self.tmp/'bad.zip')
        (self.root/'src/process_observability/link.py').unlink()
        out=self.tmp/'good.zip';m.build(self.root,out)
        changed=self.tmp/'tampered.zip'
        with zipfile.ZipFile(out) as original,zipfile.ZipFile(changed,'w') as dest:
            for item in original.infolist():
                data=original.read(item)
                dest.writestr(item,b'changed' if item.filename==m.PREFIX+'run.py' else data)
        with self.assertRaises(m.ReleaseError):m.verify(changed)

    def test_refuses_extra_zip_paths_and_incomplete_runtime(self):
        m=api();out=self.tmp/'full.zip';m.build(self.root,out)
        with zipfile.ZipFile(out,'a') as archive:archive.writestr(m.PREFIX+'../escape.py','bad')
        with self.assertRaises(m.ReleaseError):m.verify(out)
        (self.root/'run.py').unlink()
        with self.assertRaises(m.ReleaseError):m.build(self.root,self.tmp/'missing.zip')
