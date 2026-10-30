"""Portable opt-in request controls and bounded 429 retries, with no network."""
import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import patch
from process_observability import cli, llm
from process_observability.cli import _model_fingerprint
from process_observability.llm import LLMError, ModelConfig, OpenAICompatibleModel, TransportResponse
from test_llm import FakeTransport, HTTP_URL, ok

OPTIONS={'reasoning':{'effort':'low','exclude':True},
         'provider':{'sort':'throughput','allow_fallbacks':True,'require_parameters':True,
                     'ignore':['excluded-provider'],'max_price':{'prompt':0.15,'completion':0.30}},
         'response_format':{'type':'json_object'}}

def config(**kw):return ModelConfig.from_dict({'base_url':HTTP_URL,'model':'configured-model',**kw})

class PortableRequestTests(unittest.TestCase):
    def test_configured_options_are_sent_without_overriding_the_core_request(self):
        t=FakeTransport(ok('{"ok":true}'));c=config(extra_body=OPTIONS)
        self.assertEqual(OpenAICompatibleModel(c,transport=t).complete_json('sys','usr'),{'ok':True})
        payload=t.calls[0]['payload']
        for k,v in OPTIONS.items():self.assertEqual(payload[k],v)
        self.assertEqual(payload['model'],'configured-model')
        self.assertEqual(payload['messages'],[{'role':'system','content':'sys'},{'role':'user','content':'usr'}])
        self.assertEqual(ModelConfig.from_dict(c.to_dict()).to_dict(),c.to_dict())

    def test_extension_snapshot_is_not_mutated_by_the_caller_or_transport(self):
        options=copy.deepcopy(OPTIONS);c=config(extra_body=options);initial=_model_fingerprint(c)
        original_hash=hash(c)
        options['reasoning']['effort']='high'
        c.extra_body['provider']['only']=['later-mutation']
        class Mutating:
            def __init__(self):self.observed=[]
            def __call__(self,url,payload,headers,ctx):
                self.observed.append(copy.deepcopy(payload['reasoning']))
                payload['reasoning']['effort']='high'
                return ok('{"ok":true}')
        t=Mutating();m=OpenAICompatibleModel(c,transport=t)
        m.complete_json('s','u');m.complete_json('s','u')
        self.assertEqual(t.observed,[OPTIONS['reasoning'],OPTIONS['reasoning']])
        self.assertEqual(_model_fingerprint(c),initial)
        self.assertEqual(hash(c),original_hash)
        self.assertEqual(c,config(extra_body=OPTIONS))

    def test_extensions_invalidate_cache_identity_but_default_identity_stays_exact(self):
        old={'base_url':HTTP_URL,'model':'configured-model','thinking':None,'max_output_tokens':8192}
        self.assertEqual(_model_fingerprint(config()),json.dumps(old,sort_keys=True))
        self.assertNotEqual(_model_fingerprint(config(extra_body=OPTIONS)),_model_fingerprint(config()))
        self.assertEqual(_model_fingerprint(config(rate_limit_retries=2)),_model_fingerprint(config()))

    def test_rejects_collisions_credentials_nonobjects_and_nonfinite_or_deep_values(self):
        nested={};cursor=nested
        for _ in range(20):cursor['next']={};cursor=cursor['next']
        for value in ([],{'model':'replacement'},{'messages':[]},{'max_tokens':123},
                      {'reasoning':[]},{'provider':{'api_key':'SECRET-CANARY'}},
                      {'provider':{'x-api-key':'SECRET-CANARY'}},{'provider':{'access_token':'SECRET-CANARY'}},
                      {'provider':{'cookie':'SECRET-CANARY'}},
                      {'provider':{'headers':{'Authorization':'SECRET-CANARY'}}},
                      {'provider':{'max_price':float('nan')}},{'provider':nested}):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(LLMError) as caught:config(extra_body=value)
                self.assertNotIn('SECRET-CANARY',str(caught.exception))
                self.assertEqual(caught.exception.category,'config')

    def test_standard_check_model_uses_the_same_explicit_profile_without_a_pilot_wrapper(self):
        t=FakeTransport(ok('{"ok":true}'),ok('{"ok":true}'))
        with tempfile.TemporaryDirectory() as folder:
            profile=Path(folder)/'model.json'
            profile.write_text(json.dumps({role:config(extra_body=OPTIONS).to_dict() for role in ('code','business')}))
            with patch.object(llm,'urllib_transport',t),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                result=cli.main(['check-model','--config',str(profile),'--role','all'])
        self.assertEqual(result,0);self.assertEqual(len(t.calls),2)
        self.assertTrue(all(call['payload']['reasoning']==OPTIONS['reasoning'] for call in t.calls))

class PortableRateLimitTests(unittest.TestCase):
    def test_retry_each_transport_attempt_once_then_return_valid_content(self):
        t=FakeTransport(TransportResponse(429,b'not persisted'),TransportResponse(429,b'not persisted'),ok('{"ok":true}'))
        waits=[];events=[]
        m=OpenAICompatibleModel(config(rate_limit_retries=2),transport=t,sleeper=waits.append,
                                observer=lambda event,data:events.append((event,data)))
        self.assertEqual(m.complete_json('s','u'),{'ok':True})
        self.assertEqual(len(t.calls),3);self.assertEqual(waits,[30,90])
        attempts=[d for e,d in events if e=='api.transport_attempt']
        self.assertEqual([d['attempt'] for d in attempts],[1,2,3])
        self.assertEqual([d['http_status'] for d in attempts],[429,429,200])
        self.assertEqual(len({d['api_request_sha256'] for d in attempts}),1)
        self.assertNotIn('not persisted',json.dumps(events))

    def test_a_transport_exception_is_reported_once_with_hash_without_its_message(self):
        t=FakeTransport(RuntimeError('SECRET-CANARY'));events=[];waits=[]
        m=OpenAICompatibleModel(config(rate_limit_retries=2),transport=t,sleeper=waits.append,
                                observer=lambda event,data:events.append((event,data)))
        with self.assertRaises(LLMError):m.complete_json('s','u')
        attempts=[d for e,d in events if e=='api.transport_attempt']
        self.assertEqual(len(attempts),1);self.assertEqual(attempts[0]['attempt'],1)
        self.assertEqual(len(attempts[0]['api_request_sha256']),64)
        self.assertIsNone(attempts[0]['http_status']);self.assertEqual(waits,[])
        self.assertNotIn('SECRET-CANARY',json.dumps(events))

    def test_observer_failure_does_not_chain_the_private_transport_exception(self):
        t=FakeTransport(RuntimeError('SECRET-CANARY'))
        def observer(event,data):
            raise OSError('observer unavailable')
        try:
            OpenAICompatibleModel(config(rate_limit_retries=1),transport=t,observer=observer).complete_json('s','u')
        except OSError as error:
            formatted=''.join(traceback.format_exception(error))
            self.assertNotIn('SECRET-CANARY',formatted)
            self.assertIsNone(error.__context__)
        else:self.fail('The observer failure must propagate')

    def test_exhausted_429_preserves_http_failure_without_extra_calls(self):
        t=FakeTransport(*[TransportResponse(429,b'') for _ in range(3)]);waits=[]
        with self.assertRaises(LLMError) as caught:
            OpenAICompatibleModel(config(rate_limit_retries=2),transport=t,sleeper=waits.append).complete_json('s','u')
        self.assertEqual(caught.exception.status,429);self.assertEqual(len(t.calls),3);self.assertEqual(waits,[30,90])

    def test_never_retries_http200_json_empty_or_length_or_other_http_errors(self):
        cases=[ok('not json'),ok(None),ok('{}',finish_reason='length'),TransportResponse(500,b''),LLMError('offline',category='connection')]
        for response in cases:
            t=FakeTransport(response);waits=[]
            with self.subTest(response_type=type(response).__name__),self.assertRaises(LLMError):
                OpenAICompatibleModel(config(rate_limit_retries=2),transport=t,sleeper=waits.append).complete_json('s','u')
            self.assertEqual(len(t.calls),1);self.assertEqual(waits,[])

    def test_default_does_not_retry_and_invalid_retry_counts_fail_before_io(self):
        t=FakeTransport(TransportResponse(429,b''))
        with self.assertRaises(LLMError):OpenAICompatibleModel(config(),transport=t).complete_json('s','u')
        self.assertEqual(len(t.calls),1)
        for value in (-1,4,True,1.5,'2',None):
            with self.subTest(value=value),self.assertRaises(LLMError):config(rate_limit_retries=value)

if __name__=='__main__':unittest.main()
