"""Failure injection for durable dispatch admission; no network or credentials."""
import unittest
import asyncio
from unittest.mock import patch
from process_observability.pipeline import RunBudget, RunHalt, ProductTransport
from process_observability.llm import ModelConfig, OpenAICompatibleModel, TransportResponse


class DurableBudgetAudit(unittest.TestCase):
    def budget(self, save, limit=3):
        manifest={'requests':[]}
        return RunBudget(limit,60,manifest,save),manifest

    def test_failed_reservation_is_terminal_even_if_disk_recovers(self):
        saves=[]
        def flaky():
            saves.append(True)
            if len(saves)==1: raise OSError('disk unavailable')
        budget,manifest=self.budget(flaky)
        with self.assertRaises(RunHalt) as failed: budget.reserve('logging')
        self.assertEqual(failed.exception.category,'checkpoint_error')
        with self.assertRaises(RunHalt) as repeated: budget.reserve('metrics')
        self.assertEqual(repeated.exception.category,'checkpoint_error')
        self.assertEqual(len(saves),1,'a terminal budget cannot recover by retrying save')
        self.assertLessEqual(len(manifest['requests']),1)

    def test_post_response_save_failure_blocks_next_dispatch(self):
        calls=[]
        def flaky():
            calls.append(True)
            if len(calls)==2: raise OSError('fsync failed')
        budget,_=self.budget(flaky)
        budget.reserve('logging')
        with self.assertRaises(RunHalt) as failed: budget.persist()
        self.assertEqual(failed.exception.category,'checkpoint_error')
        with self.assertRaises(RunHalt): budget.reserve('logging')
        self.assertEqual(len(calls),2)

    def test_common_model_cannot_convert_persistence_halt_into_retryable_error(self):
        network=[]
        def unavailable(): raise OSError('disk unavailable')
        budget,_=self.budget(unavailable)
        def transport(*args):
            budget.reserve('logging')
            network.append(True)
            return TransportResponse(200,b'{}')
        model=OpenAICompatibleModel(ModelConfig.from_dict({'base_url':'http://fixture.invalid/v1','model':'fixture','api_key_env':None}),transport=transport)
        with self.assertRaises(RunHalt) as failed: model.complete_json('system','input')
        self.assertEqual(failed.exception.category,'checkpoint_error')
        self.assertFalse(isinstance(failed.exception,Exception),'nested broad Exception handlers must not swallow a terminal halt')
        self.assertEqual(network,[])

    def test_request_limit_stops_before_another_save(self):
        saves=[]
        budget,manifest=self.budget(lambda:saves.append(True),limit=1)
        budget.reserve('map')
        with self.assertRaises(RunHalt) as failed: budget.reserve('logging')
        self.assertEqual(failed.exception.category,'request_budget')
        self.assertEqual(len(manifest['requests']),1)
        self.assertEqual(len(saves),1)

    def test_deadline_stops_before_save_or_dispatch(self):
        saves=[]
        budget,_=self.budget(lambda:saves.append(True))
        with patch('process_observability.pipeline.time.monotonic',return_value=budget.deadline):
            with self.assertRaises(RunHalt) as failed: budget.reserve('logging')
        self.assertEqual(failed.exception.category,'time_limit')
        self.assertEqual(saves,[])

    def test_rate_limit_delay_cannot_sleep_past_product_deadline(self):
        budget,_=self.budget(lambda:None)
        with patch('process_observability.pipeline.time.monotonic',return_value=budget.deadline-5),patch('process_observability.pipeline.time.sleep') as sleep:
            with self.assertRaises(RunHalt) as failed: budget.sleep(30)
        self.assertEqual(failed.exception.category,'time_limit')
        sleep.assert_not_called()

    def test_whole_request_deadline_cancels_a_stalled_transport(self):
        manifest={'requests':[]}
        budget=RunBudget(3,.02,manifest,lambda:None)
        transport=ProductTransport(budget,10)
        cancelled=[]
        async def stalled(*args):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.append(True)
        with patch.object(transport,'_request',stalled):
            with self.assertRaises(RunHalt) as failed: transport('http://fixture.invalid',{}, {}, None)
        self.assertEqual(failed.exception.category,'time_limit')
        self.assertEqual(cancelled,[True])
        self.assertEqual(len(manifest['requests']),1)
        self.assertEqual(manifest['requests'][0]['status'],'failed')
