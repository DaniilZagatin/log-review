"""Process-context loading and actual model-boundary wiring, using synthetic text only."""

import hashlib
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch

from process_observability.process_inputs import load_process_preset
from process_observability.llm import TransportResponse
from test_cli import ROOT,EXAMPLE,invoke,review_reply


class ProcessInputTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.folder=self.root/'inputs/processes/p3283';self.folder.mkdir(parents=True)
        self.text='# П3283\n\nПРОЦЕСС-ТЕСТ: описание только для проверки передачи.\nКонец полного текста.\n'
        self.raw=self.text.encode('utf-8')
        self.description=self.folder/'description.md';self.description.write_bytes(self.raw)
        self.metadata={'schema_version':'process-input/1','process_id':'П3283','process_name':'Искусственный процесс',
                       'source_kind':'derived_upstream','authority':'proposal','source_file':'artificial.md',
                       'sha256':hashlib.sha256(self.raw).hexdigest(),'bytes':len(self.raw)}
        self.write_metadata()
        location=patch('process_observability.process_inputs.ROOT',self.root)
        location.start();self.addCleanup(location.stop)

    def write_metadata(self):
        (self.folder/'source.json').write_text(json.dumps(self.metadata,ensure_ascii=False),encoding='utf-8')

    def test_reads_full_utf8_description_and_preserves_provenance(self):
        text,meta=load_process_preset('p3283')
        self.assertEqual(text,self.text)
        self.assertEqual(meta['sha256'],hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(meta['source_kind'],'derived_upstream')
        self.assertEqual(meta['authority'],'proposal')
        self.assertEqual(meta['path'],'inputs/processes/p3283/description.md')

    def test_modified_description_is_rejected(self):
        self.description.write_bytes(self.raw+b'changed')
        with self.assertRaisesRegex(ValueError,'SHA-256'):load_process_preset('p3283')

    def test_missing_or_symlinked_description_is_rejected(self):
        self.description.unlink()
        with self.assertRaisesRegex(ValueError,'missing or unsafe'):load_process_preset('p3283')
        other=self.root/'outside.md';other.write_bytes(self.raw);self.description.symlink_to(other)
        with self.assertRaisesRegex(ValueError,'missing or unsafe'):load_process_preset('p3283')

    def test_upstream_context_cannot_be_promoted_to_accepted_authority(self):
        self.metadata['authority']='accepted';self.write_metadata()
        with self.assertRaisesRegex(ValueError,'authority'):load_process_preset('p3283')

    def test_process_info_checks_the_bundled_input_without_a_model_call(self):
        with patch('process_observability.llm.OpenAICompatibleModel',side_effect=AssertionError('No model expected')):
            code,out,err=invoke('process-info','p3283')
        self.assertEqual(code,0,err)
        self.assertEqual(json.loads(out)['sha256'],self.metadata['sha256'])

    def test_process_flag_forwards_the_whole_context_and_records_its_hash(self):
        records=json.loads((EXAMPLE/'model-responses.json').read_text(encoding='utf-8'))['records'].values()
        replies={('code' if 'findings' in record else 'map' if 'process' in record else
                  'instrumentation' if 'cards' in record else 'metrics'):record for record in records}
        requests={}
        def transport(url,payload,headers,context):
            system,user=(message['content'] for message in payload['messages'])
            task=system.splitlines()[0].split(':',1)[1].strip()
            requests[task]=json.loads(user)
            answer = review_reply(requests[task]) if task == 'code' else replies[task]
            response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(answer)}}]}
            return TransportResponse(200,json.dumps(response).encode())
        output=self.root/'run'
        with patch('process_observability.llm.urllib_transport',side_effect=transport),patch(
            'process_observability.llm._build_ssl_context',
            side_effect=lambda config:ssl.create_default_context() if config.base_url.startswith('https:') else None):
            code,_,err=invoke('analyze','--process','p3283','--repo',EXAMPLE,'--include','app.py','--out',output,
                              '--map-mode','coarse-refine')
        self.assertEqual(code,0,err)
        business=requests['map']['business_text']
        self.assertTrue(business.endswith(self.text))
        self.assertIn('ПРОЦЕСС-ТЕСТ',business)
        self.assertEqual(requests['metrics']['business_text'],business)
        self.assertNotIn('ПРОЦЕСС-ТЕСТ',json.dumps(requests['code'],ensure_ascii=False))
        bundle=json.loads((output/'bundle.json').read_text(encoding='utf-8'))
        forwarded=hashlib.sha256(business.encode('utf-8')).hexdigest()
        self.assertEqual(bundle['diagnostics']['business_input']['sha256'],self.metadata['sha256'])
        self.assertEqual(bundle['diagnostics']['business_input']['forwarded_text_sha256'],forwarded)
        inputs={item['path']:item['sha256'] for item in bundle['manifest']['input_fingerprints']}
        self.assertEqual(inputs['inputs/business.txt'],forwarded)
        events=[json.loads(line) for line in (output/'trace.jsonl').read_text(encoding='utf-8').splitlines()]
        loaded=next(item['data'] for item in events if item['event']=='input.business_loaded')
        self.assertEqual(loaded['sha256'],self.metadata['sha256'])

    def test_missing_preset_cannot_silently_run_without_business_context(self):
        self.description.unlink()
        with patch('process_observability.llm.OpenAICompatibleModel',side_effect=AssertionError('No model expected')):
            code,_,_=invoke('analyze','--process','p3283','--repo',EXAMPLE,'--include','app.py','--out',self.root/'missing')
        self.assertEqual(code,2)

    def test_explicit_business_file_and_preset_are_not_silently_combined(self):
        code,_,err=invoke('analyze','--process','p3283','--business-file',self.description,
                          '--repo',EXAMPLE,'--include','app.py','--out',self.root/'conflict')
        self.assertEqual(code,2)
        self.assertIn('not both',err)


if __name__=='__main__':unittest.main()
