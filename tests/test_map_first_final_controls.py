"""Independent reproductions from final peer review. All inputs and replies are synthetic."""
import json
from pathlib import Path
import tempfile
import unittest

from process_observability.logging_contracts import safe_public
from process_observability.logging_review import review_logging
from process_observability.map_input import MapView
from process_observability.source_tools import snapshot_repository
from test_map_first_logging import Scripted, model_reply, operation


class FinalReviewControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def inputs(self, code, *, path='app.py', last=None, first=1, effect='result', outcome='success', extra_files=None):
        (self.root/path).write_text(code)
        for name, content in (extra_files or {}).items(): (self.root/name).write_text(content)
        node = operation(code, first=first, last=last or len(code.splitlines()), effect=effect, outcome=outcome)
        node['evidence'][0]['path'] = 'source-1/' + path
        view = MapView.from_dict({'schema_version': '2.0', 'scenario': {}, 'sources': [{'id': 'source-1'}],
            'nodes': [node], 'edges': [], 'run': {'id': 'final-controls', 'status': 'completed'}})
        return view, snapshot_repository(self.root, max_files=None, max_chars=None)

    def test_java_unresolved_helper_does_not_prove_absence(self):
        code = ('class Service { void complete(String id) { audit.recordCompleted(id); } }\n'
                'class Audit { void recordCompleted(String id) { logger.info("completed"); } }\n')
        view, source = self.inputs(code, path='app.java', last=1)
        result = review_logging(view, source, Scripted(lambda p, n: model_reply(p, key='absent')))
        self.assertEqual(result['operations'][0]['state'], 'X')
        self.assertTrue(result['operations'][0]['search']['pending'])

    def test_unresolved_receiver_with_logging_hint_is_unknown(self):
        code = ("class Service:\n    def complete(self, item):\n        self.audit.record('done')\n"
                "class Audit:\n    def record(self, message):\n        logger.info(message)\n")
        view, source = self.inputs(code, first=2, last=3)
        def callback(packet, number):
            self.assertTrue(any(s['start_line'] == 6 for s in packet['candidates']))
            self.assertTrue(any(r['start_line'] <= 6 <= r['end_line'] for r in packet['source']))
            return model_reply(packet, key='absent')
        self.assertEqual(review_logging(view, source, Scripted(callback))['operations'][0]['state'], 'X')

    def test_unresolved_receiver_with_no_logging_hint_does_not_force_unknown(self):
        code = ("class Service:\n    def complete(self, item):\n        self.audit.record('done')\n"
                "class Audit:\n    def record(self, message):\n        return message\n")
        view, source = self.inputs(code, first=2, last=3)
        self.assertEqual(review_logging(view, source, Scripted(lambda p, n: model_reply(p, key='absent')))['operations'][0]['state'], 'U')

    def test_source_literal_thinking_tags_reach_model_and_remain_citable(self):
        code = "def complete(item):\n    prompt = 'Reason inside <thinking> tags'\n    logger.info('done')\n"
        view, source = self.inputs(code)
        def callback(packet, number):
            value = model_reply(packet)
            span = packet['source'][0]
            value['bindings'][0]['evidence'] = [{k: span[k] for k in ('path', 'start_line', 'end_line', 'quote')}]
            return value
        model = Scripted(callback)
        result = review_logging(view, source, model)
        self.assertIn('<thinking>', model.calls[0]['packet']['source'][0]['quote'])
        self.assertEqual(result['operations'][0]['state'], 'C')
        self.assertIn('<thinking>', result['checkpoints'][0]['initial_packet']['source'][0]['quote'])
        self.assertIn('<thinking>', result['checkpoints'][0]['reply']['bindings'][0]['evidence'][0]['quote'])

    def test_model_private_tags_are_rejected_and_absent_from_audit(self):
        view, source = self.inputs("def complete(item):\n    logger.info('done')\n")
        def callback(packet, number):
            value = model_reply(packet)
            if number == 1:
                value['bindings'][0]['meaning'] = '<thinking>PRIVATE_SYNTHETIC_MARKER</thinking>'
                value['analysis'] = 'PRIVATE_SYNTHETIC_ANALYSIS'
            return value
        result = review_logging(view, source, Scripted(callback))
        self.assertEqual(result['operations'][0]['state'], 'C')
        self.assertEqual(result['model_requests'], 2)
        serialized = json.dumps(result)
        self.assertNotIn('PRIVATE_SYNTHETIC_MARKER', serialized)
        self.assertNotIn('PRIVATE_SYNTHETIC_ANALYSIS', serialized)
        self.assertEqual(safe_public({'analysis': 'synthetic'}), {})

    def test_failed_auxiliary_fields_remain_audited_on_checkpoint_reuse(self):
        view, source = self.inputs("def complete(item):\n    logger.info('done')\n")
        def callback(packet, number):
            value = model_reply(packet); value['guidance_ids'] = ['not-a-rule']; return value
        first = review_logging(view, source, Scripted(callback))
        second = review_logging(view, source, Scripted(lambda p, n: self.fail('checkpoint should be reusable')), resume=first)
        self.assertEqual(second['operations'], first['operations'])
        self.assertEqual(second['checkpoints'][0]['ignored_auxiliary_fields'], ['guidance_ids'])
        self.assertNotIn('guidance_ids', second['checkpoints'][0]['reply'])

    def test_reasoning_configuration_is_part_of_request_cache_identity(self):
        view, source = self.inputs("def complete(item):\n    logger.info('done')\n")
        low = {'model': 'fixture', 'extra_body': {'reasoning': {'effort': 'low'}}}
        high = {'model': 'fixture', 'extra_body': {'reasoning': {'effort': 'high'}}}
        first = review_logging(view, source, Scripted(lambda p, n: model_reply(p)), model_identity=low)
        same = review_logging(view, source, Scripted(lambda p, n: self.fail('same configuration must reuse')), resume=first, model_identity=low)
        changed = review_logging(view, source, Scripted(lambda p, n: model_reply(p)), resume=first, model_identity=high)
        self.assertEqual(same['model_requests'], 0)
        self.assertEqual(changed['model_requests'], 1)
        self.assertNotEqual(first['model_identity']['request_configuration_sha256'], changed['model_identity']['request_configuration_sha256'])

    def configured_result(self, filename, config):
        code = "def complete(item):\n    events.publish('card.requested', item)\n"
        view, source = self.inputs(code, effect='command', outcome='unknown', extra_files={filename: config})
        def callback(packet, number):
            if number == 1:
                return {'action': 'inspect', 'logging_calls': ['events.publish'],
                        'reads': [{'path': filename, 'start_line': 1, 'end_line': 1}]}
            value = model_reply(packet, key='card.requested', role='request')
            value['bindings'][0]['emission_evidence'] = [{'path': filename, 'start_line': 1, 'end_line': 1}]
            # Migrated for policy 2: a request record establishes a sent command only with an explicit post-action
            # moment (request-role-proof latent risk). The adapter-contract path under test is unchanged.
            value['bindings'][0]['moment'] = 'after_action'
            value['missing'] = []  # This fixture establishes sending the command, not its remote application.
            return value
        return review_logging(view, source, Scripted(callback))

    def test_business_message_handler_is_not_a_logging_contract(self):
        result = self.configured_result('settings.yaml', 'events.handler: card-command-consumer\n')
        self.assertEqual(result['operations'][0]['state'], 'X')

    def test_distinct_logging_adapter_contract_can_establish_logging(self):
        result = self.configured_result('logback.xml', '<appender name="events" class="AuditJournalAppender"/>\n')
        self.assertEqual(result['operations'][0]['state'], 'C')

    def test_async_operation_without_logging_can_finish_the_search(self):
        code = 'async def complete(item):\n    return await save(item)\nasync def save(item):\n    return item\n'
        view, source = self.inputs(code, last=2)
        self.assertEqual(review_logging(view, source, Scripted(lambda p, n: model_reply(p, key='absent')))['operations'][0]['state'], 'U')

    def test_async_helper_is_delivered_and_its_actual_invocation_can_be_bound(self):
        code = "async def notify(item):\n    logger.info('done')\nclass Service:\n    async def complete(self, item):\n        await notify(item)\n"
        view, source = self.inputs(code, first=4, last=5)
        def callback(packet, number):
            self.assertTrue(any(r['start_line'] <= 2 <= r['end_line'] for r in packet['source']))
            value = model_reply(packet, key='absent')
            site = next(s for s in packet['candidates'] if s['start_line'] == 2)
            call = {'path': 'app.py', 'start_line': 5, 'end_line': 5}
            value['bindings'] = [{'site_id': site['id'], 'role': 'result', 'meaning': 'Запись при вызове notify',
                'conditions': 'При вызове операции', 'correlation': 'Аргумент item передан в notify',
                'establishes': ['occurrence', 'result'], 'evidence': [{'path': 'app.py', 'start_line': 2, 'end_line': 2}],
                'invocation_ref': call, 'argument_evidence': [call], 'covers_outcomes': ['success']}]
            value['missing'] = []  # A source-bound helper record was supplied instead of the absent direct site.
            return value
        self.assertEqual(review_logging(view, source, Scripted(callback))['operations'][0]['state'], 'C')

    def test_truncated_receiver_hint_set_does_not_prove_absence(self):
        declarations = ''.join('class Audit%02d:\n    def record(self, message):\n        return message\n' % i for i in range(25))
        code = declarations + "class Service:\n    def complete(self, item):\n        self.audit.record('done')\n"
        view, source = self.inputs(code, first=77, last=78)
        result = review_logging(view, source, Scripted(lambda p, n: model_reply(p, key='absent')))
        self.assertEqual(result['operations'][0]['state'], 'X')
        self.assertTrue(any('подсказ' in p for p in result['operations'][0]['search']['pending']))

    def test_unvalidated_reference_metadata_cannot_persist_private_tags(self):
        view, source = self.inputs("def complete(item):\n    logger.info('done')\n")
        def callback(packet, number):
            value = model_reply(packet)
            value['bindings'][0]['evidence'][0]['symbol'] = '<thinking>PRIVATE_REFERENCE_MARKER</thinking>'
            return value
        saved = []
        result = review_logging(view, source, Scripted(callback), on_checkpoint=saved.append)
        self.assertEqual(result['operations'][0]['state'], 'C')
        self.assertNotIn('PRIVATE_REFERENCE_MARKER', json.dumps([result, saved]))

    def test_tagged_validation_errors_stay_partial_and_do_not_abort_other_operations(self):
        view, source = self.inputs("def complete(item):\n    logger.info('done')\n")
        def callback(packet, number):
            value = model_reply(packet); value['<thinking>PRIVATE_ERROR_MARKER'] = 'invalid'; return value
        from process_observability.logging_report import validate_report
        result = review_logging(view, source, Scripted(callback))
        self.assertEqual(result['operations'][0]['state'], 'X')
        self.assertNotIn('PRIVATE_ERROR_MARKER', json.dumps(result))
        self.assertTrue(validate_report(result))

    def test_nested_ignored_auxiliary_quote_is_not_a_verified_source_quote(self):
        view, source = self.inputs("def complete(item):\n    logger.info('done')\n")
        proposal = {'id': 'p', 'kind': 'add_event', 'operation_ids': ['save'], 'problem': 'Проверить разрез',
            'what': 'Уточнить', 'why': 'Проверка', 'fields': [], 'location': None, 'timing': 'После', 'verification': 'Проверить',
            'guidance_ids': [{'quote': '<thinking>PRIVATE_GUIDANCE_MARKER</thinking>'}]}
        saved = []
        result = review_logging(view, source, Scripted(lambda p, n: model_reply(p, changes=[proposal])), on_checkpoint=saved.append)
        self.assertEqual(result['operations'][0]['state'], 'C')
        self.assertEqual(result['model_requests'], 2)
        self.assertNotIn('PRIVATE_GUIDANCE_MARKER', json.dumps([result, saved]))
        self.assertIn('changes[0].guidance_ids', result['operations'][0]['warnings'][0])
        self.assertNotIn('guidance_ids', result['checkpoints'][0]['reply']['changes'][0])
        again = review_logging(view, source, Scripted(lambda p, n: self.fail('must reuse')), resume=result)
        self.assertEqual(again['operations'], result['operations'])

    def test_long_model_error_diagnostic_does_not_abort_the_report(self):
        view, source = self.inputs("def complete(item):\n    logger.info('done')\n")
        def callback(packet, number):
            value = model_reply(packet); value['x' * 20001] = 'invalid'; return value
        from process_observability.logging_report import validate_report
        result = review_logging(view, source, Scripted(callback))
        self.assertEqual(result['operations'][0]['state'], 'X')
        self.assertLess(len(result['operations'][0]['limitations'][0]), 2100)
        self.assertTrue(validate_report(result))


if __name__ == '__main__': unittest.main()
