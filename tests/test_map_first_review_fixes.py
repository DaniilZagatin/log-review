"""Reproductions independently derived from Claude's consumer review."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from process_observability.logging_report import build_html, validate_report, write_report
from process_observability.logging_review import review_logging
from process_observability.map_input import MapView
from process_observability.source_tools import snapshot_repository
from process_observability.upstream_masking import upstream_text
from test_map_first_logging import CODE, Scripted, model_reply, operation


class ReviewCorrections(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.repo = self.root/'repo'; self.repo.mkdir()

    def inputs(self, code=CODE, operations=None, **extra):
        (self.repo/'app.py').write_text(code)
        document = {'schema_version': '2.0', 'scenario': {}, 'sources': [{'id': 'source-1'}],
                    'nodes': operations if operations is not None else [operation(code)], 'edges': [],
                    'run': {'id': 'review-fixture', 'status': 'completed'}, **extra}
        return MapView.from_dict(document), snapshot_repository(self.repo, max_files=None, max_chars=None)

    def test_made_up_invocation_inside_own_function_does_not_prove_a_foreign_log(self):
        view, source = self.inputs()
        def callback(packet, number):
            if number == 1: return {'action': 'inspect', 'reads': [{'path': 'app.py', 'start_line': 8, 'end_line': 9}]}
            value = model_reply(packet); other = next(s for s in packet['candidates'] if s['start_line'] == 9)
            invocation = {'path': 'app.py', 'start_line': 4, 'end_line': 4}  # persist(item), NOT other(item)
            value['bindings'][0].update(site_id=other['id'], evidence=[{'path': 'app.py', 'start_line': 9, 'end_line': 9}],
                                        invocation_ref=invocation, argument_evidence=[invocation])
            return value
        result = review_logging(view, source, Scripted(callback))
        self.assertEqual(result['operations'][0]['state'], 'X')
        self.assertIn('does not resolve', result['operations'][0]['limitations'][0])

    def test_one_branch_cannot_claim_the_other_branches_record(self):
        code = "import logging\nlogger=logging.getLogger(__name__)\ndef handle(item):\n    if item.valid:\n        logger.info('a')\n    else:\n        logger.info('b')\n    return item\n"
        operations = [operation(code, identifier='a', first=5, last=5), operation(code, identifier='b', first=7, last=7)]
        view, source = self.inputs(code, operations)
        def callback(packet, number):
            value = model_reply(packet, key='absent' if packet['operation']['id'] == 'a' else 'b')
            if packet['operation']['id'] == 'b':
                other = next(s for s in packet['candidates'] if s['event_key'] == 'a')
                value['bindings'][0].update(site_id=other['id'], evidence=[{'path': 'app.py', 'start_line': 5, 'end_line': 5}])
            return value
        result = review_logging(view, source, Scripted(callback))
        self.assertEqual([r['state'] for r in result['operations']], ['U', 'X'])

    def test_business_publisher_cannot_prove_logging_by_citing_itself(self):
        code = "def submit(item):\n    broker.publish('command', item)\n"
        view, source = self.inputs(code, [operation(code, first=1, last=2, effect='command', outcome='unknown')])
        def callback(packet, number):
            if number == 1: return {'action': 'inspect', 'logging_calls': ['broker.publish']}
            value = model_reply(packet, key='command')
            value['bindings'][0]['emission_evidence'] = [{'path': 'app.py', 'start_line': 2, 'end_line': 2}]
            return value
        result = review_logging(view, source, Scripted(callback))
        self.assertEqual(result['operations'][0]['state'], 'X')
        self.assertEqual(result['inventory']['scope']['call_names'], [])
        self.assertEqual(result['inventory']['model_suggested_calls'][0]['callee'], 'broker.publish')

    def test_unrelated_common_method_noise_does_not_make_completed_search_unknown(self):
        (self.repo/'noise.py').write_text('\n'.join("logger.info('noise')" for _ in range(100)))
        view, source = self.inputs()
        result = review_logging(view, source, Scripted(lambda p, n: model_reply(p, role='start', establishes=['occurrence'])))
        self.assertEqual(result['operations'][0]['state'], 'P')
        self.assertEqual(len(result['inventory']['sites']), 103)

    def test_published_masked_map_is_preserved_and_resume_does_not_reask_it(self):
        code = "import logging\nlogger=logging.getLogger(__name__)\ndef save(item):\n    API_TOKEN = 'opaqueFixtureValue'\n    logger.info('done')\n"
        node = operation(code, first=3, last=5)
        node['evidence'][0]['quote'] = '\n'.join(upstream_text(code).splitlines()[2:5]).strip()
        view, source = self.inputs(code, [node])
        first = review_logging(view, source, Scripted(lambda p, n: model_reply(p)))
        self.assertEqual(first['map'], view.original)
        self.assertEqual(first['operations'][0]['state'], 'C')
        self.assertTrue(validate_report(first))
        write_report(first, self.root/'published')
        again = review_logging(view, source, Scripted(lambda p, n: self.fail('No repeated model request')), resume=first)
        self.assertEqual(again['model_requests'], 0)
        self.assertEqual(again['operations'], first['operations'])

    def test_bare_key_mask_compatibility_and_source_fragments_use_the_same_policy(self):
        code = "import logging\nlogger=logging.getLogger(__name__)\ndef submit(item):\n    producer.send('cards', key=request_id, value=body)\n    logger.info('done')\n"
        node = operation(code, first=3, last=5); node['evidence'][0]['quote'] = '\n'.join(upstream_text(code).splitlines()[2:5]).strip()
        view, source = self.inputs(code, [node])
        result = review_logging(view, source, Scripted(lambda p, n: model_reply(p)))
        self.assertEqual(result['operations'][0]['state'], 'C')
        self.assertTrue(validate_report(result))

    def test_unknown_no_op_is_not_a_completed_fresh_operation(self):
        view, source = self.inputs(operations=[operation(effect='unknown', outcome='unknown')])
        result = review_logging(view, source, Scripted(lambda p, n: dict(model_reply(p, role='no_op'),
            missing=['Не подтверждён результат нового выполнения'])))
        self.assertEqual(result['operations'][0]['state'], 'P')

    def test_nonfinal_error_handling_operation_can_be_covered_by_its_own_error(self):
        node = operation(identifier='error', first=6, last=7, effect='unknown', outcome='unknown')
        view, source = self.inputs(operations=[node], execution_reviews=[{'operations': [{'node_id': 'error', 'kind': 'error_handling'}]}])
        result = review_logging(view, source, Scripted(lambda p, n: model_reply(p, key='denied', role='error')))
        self.assertEqual(result['operations'][0]['state'], 'C')

    def test_start_never_means_a_completed_command(self):
        view, source = self.inputs(operations=[operation(effect='command', outcome='unknown')])
        result = review_logging(view, source, Scripted(lambda p, n: model_reply(p, role='start')))
        self.assertEqual(result['operations'][0]['state'], 'P')

    def test_newly_read_files_add_their_candidates(self):
        view, source = self.inputs()
        def callback(packet, number):
            if number == 1: return {'action': 'inspect', 'reads': [{'path': 'app.py', 'start_line': 8, 'end_line': 9}]}
            self.assertTrue(any(s['start_line'] == 9 for s in packet['candidates']))
            return model_reply(packet)
        self.assertEqual(review_logging(view, source, Scripted(callback))['operations'][0]['state'], 'C')

    def test_export_refuses_edited_covered_status_with_no_witness(self):
        view, source = self.inputs()
        result = review_logging(view, source, Scripted(lambda p, n: model_reply(p)))
        result['operations'][0]['bindings'] = []
        with self.assertRaises(ValueError): validate_report(result)

    def test_unknown_placement_of_a_proposal_does_not_erase_an_existing_log(self):
        view, source = self.inputs()
        proposal = {'id': 'optional', 'kind': 'add_event', 'operation_ids': ['save'], 'problem': 'Дополнительный разрез',
                    'what': 'Уточнить место', 'why': 'Проверить полезность', 'fields': [], 'location': None,
                    'timing': 'После действия', 'verification': 'Проверить существующую запись'}
        result = review_logging(view, source, Scripted(lambda p, n: model_reply(p, changes=[proposal])))
        self.assertEqual(result['operations'][0]['state'], 'C')
        self.assertEqual(result['operations'][0]['review_tasks'][0]['kind'], 'proposal_location')
        self.assertTrue(validate_report(result))

    def test_proved_business_command_can_be_classified_without_becoming_a_log(self):
        code = "def publish(item):\n    queue.put(item)\ndef submit(item):\n    publish(item)\n"
        view, source = self.inputs(code, [operation(code, first=3, last=4, effect='command', outcome='unknown')])
        def callback(packet, number):
            value = model_reply(packet, key='none'); candidate = next(s for s in packet['candidates'] if s['callee'] == 'publish')
            value['candidate_reviews'] = [{'site_id': candidate['id'], 'classification': 'not_log',
                'reason': 'Реализация кладёт бизнес-команду в очередь и не пишет журнал', 'evidence': [
                    {'path': 'app.py', 'start_line': 4, 'end_line': 4}, {'path': 'app.py', 'start_line': 1, 'end_line': 2}]}]
            return value
        result = review_logging(view, source, Scripted(callback))
        self.assertEqual(result['operations'][0]['state'], 'U')
        self.assertEqual(result['inventory']['sites'][0]['review_status'], 'not_log')

    def test_multiple_first_agent_sources_map_to_same_repo_subdirectories(self):
        (self.repo/'a').mkdir(); (self.repo/'b').mkdir()
        (self.repo/'a/one.py').write_text('value = 1\n'); (self.repo/'b/two.py').write_text('value = 2\n')
        view = MapView.from_dict({'schema_version': '2.0', 'scenario': {}, 'sources': [
            {'id': 'source-1', 'name': 'a'}, {'id': 'source-2', 'name': 'b'}], 'nodes': [], 'edges': [], 'run': {'status': 'completed'}})
        from process_observability.map_input import RepoContext
        context = RepoContext(snapshot_repository(self.repo, max_files=None, max_chars=None), view)
        self.assertEqual(context.resolve('source-1/one.py'), 'a/one.py')
        self.assertEqual(context.resolve('source-2/two.py'), 'b/two.py')


if __name__ == '__main__': unittest.main()
