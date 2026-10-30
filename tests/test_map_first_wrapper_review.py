"""End-to-end Java wrapper controls through review_logging. Synthetic, independent classes only; nothing executed."""
from pathlib import Path
import tempfile
import unittest

from process_observability.logging_review import review_logging
from process_observability.map_input import MapView
from process_observability.operation_coverage import validate_saved_rows
from process_observability.source_tools import snapshot_repository
from test_map_first_logging import Scripted

FILES = {
    'src/com/example/journal/BaseJournal.java': (
        'package com.example.journal;\n'
        '\n'
        'import com.vendor.sdk.Telemetry;\n'
        '\n'
        'public class BaseJournal {\n'
        '    protected void write(String text, int level) {\n'
        '        Telemetry.Records.emitInfo(text);\n'
        '    }\n'
        '}\n'),
    'src/com/example/journal/Journal.java': (
        'package com.example.journal;\n'
        '\n'
        'public class Journal extends BaseJournal {\n'
        '    public void record(String text, int level) {\n'
        '        write(text, level);\n'
        '    }\n'
        '\n'
        '    public void record(String text, int level, boolean withCase) {\n'
        '        super.write(text, level);\n'
        '    }\n'
        '}\n'),
    'src/com/example/orders/BaseGateway.java': (
        'package com.example.orders;\n'
        '\n'
        'import com.example.journal.Journal;\n'
        '\n'
        'public class BaseGateway {\n'
        '    protected Journal journal;\n'
        '}\n'),
    'src/com/example/orders/OrderGateway.java': (
        'package com.example.orders;\n'
        '\n'
        'public class OrderGateway extends BaseGateway {\n'
        '    private final Transport transport;\n'
        '\n'
        '    public OrderGateway(Transport transport) {\n'
        '        this.transport = transport;\n'
        '    }\n'
        '\n'
        '    public String confirm(String orderId) {\n'
        '        journal.record("Отправка подтверждения " + orderId, 1, true);\n'
        '        String answer = transport.post(orderId);\n'
        '        journal.record("Подтверждение принято " + orderId, 2, true);\n'
        '        return answer;\n'
        '    }\n'
        '}\n'),
}
GATEWAY = 'src/com/example/orders/OrderGateway.java'
CONTRACT = [{'callee': 'Telemetry.Records.emitInfo', 'basis': 'Синтетическое заявление оператора: внешний SDK журналирования'}]

COMMAND_FILES = {
    'src/com/example/handlers/RefusalHandler.java': (
        'package com.example.handlers;\n'
        '\n'
        'import com.example.ops.TaskOps;\n'
        '\n'
        'public class RefusalHandler {\n'
        '    private final TaskOps ops;\n'
        '\n'
        '    public RefusalHandler(TaskOps ops) {\n'
        '        this.ops = ops;\n'
        '    }\n'
        '\n'
        '    public void handle(String documentId) {\n'
        '        ops.refuse(documentId, "Уточнить реквизиты");\n'
        '    }\n'
        '}\n'),
    'src/com/example/ops/TaskOps.java': (
        'package com.example.ops;\n'
        '\n'
        'import com.example.clients.DocumentClient;\n'
        '\n'
        'public class TaskOps {\n'
        '    private final DocumentClient client;\n'
        '\n'
        '    public TaskOps(DocumentClient client) {\n'
        '        this.client = client;\n'
        '    }\n'
        '\n'
        '    public void refuse(String documentId, String comment) {\n'
        '        new RefuseCommand(client, documentId, comment).execute();\n'
        '    }\n'
        '}\n'),
    'src/com/example/ops/RefuseCommand.java': (
        'package com.example.ops;\n'
        '\n'
        'import com.example.clients.DocumentClient;\n'
        '\n'
        'public class RefuseCommand {\n'
        '    private final DocumentClient client;\n'
        '    private final String id;\n'
        '    private final String comment;\n'
        '\n'
        '    public RefuseCommand(DocumentClient client, String id, String comment) {\n'
        '        this.client = client;\n'
        '        this.id = id;\n'
        '        this.comment = comment;\n'
        '    }\n'
        '\n'
        '    public void execute() {\n'
        '        client.refuse(id, comment);\n'
        '    }\n'
        '}\n'),
    'src/com/example/clients/DocumentClient.java': (
        'package com.example.clients;\n'
        '\n'
        'public interface DocumentClient {\n'
        '    void refuse(String id, String comment);\n'
        '}\n'),
    'src/com/example/clients/HttpDocumentClient.java': (
        'package com.example.clients;\n'
        '\n'
        'import com.example.journal.Journal;\n'
        '\n'
        'public class HttpDocumentClient implements DocumentClient {\n'
        '    private final Journal journal;\n'
        '\n'
        '    public HttpDocumentClient(Journal journal) {\n'
        '        this.journal = journal;\n'
        '    }\n'
        '\n'
        '    @Override\n'
        '    public void refuse(String id, String comment) {\n'
        '        journal.record("Отказ документу " + id, 2, true);\n'
        '    }\n'
        '}\n'),
}


def node(path, text, first, last, *, identifier='confirm', effect='command', outcome='unknown'):
    return {'id': identifier, 'kind': 'operation', 'name': 'Подтверждение заказа', 'description': 'Отправка подтверждения заказа',
            'input': 'Заказ', 'output': 'Подтверждение отправлено', 'effect_kind': effect, 'outcome': outcome,
            'evidence': [{'path': 'source-1/' + path, 'line_start': first, 'line_end': last,
                          'quote': '\n'.join(text.splitlines()[first - 1:last])}]}


def bind(packet, line, *, role, moment, hops=None, invocation=None, arguments=(), suffix='OrderGateway.java'):
    site = next(s for s in packet['candidates'] if s['path'].endswith(suffix) and s['start_line'] == line
                and s['callee'] == 'journal.record')
    chain = site.get('logging_chain') or {}
    witnesses = [{k: hop[k] for k in ('path', 'start_line', 'end_line')} for hop in chain.get('hops', [])]
    if chain.get('terminal'):
        witnesses.append({k: chain['terminal'][k] for k in ('path', 'start_line', 'end_line')})
    if hops is not None: witnesses = hops(witnesses)
    return {'site_id': site['id'], 'role': role, 'moment': moment, 'meaning': 'Запись журнала об этом действии',
            'conditions': 'При выполнении операции', 'correlation': 'Идентификатор передан в сообщение',
            'establishes': ['occurrence'] if moment == 'before_action' else ['occurrence', 'result'],
            'evidence': [{'path': site['path'], 'start_line': site['start_line'], 'end_line': site['end_line']}],
            'invocation_ref': invocation, 'argument_evidence': list(arguments), 'emission_evidence': witnesses,
            'covers_outcomes': []}


def reply(packet, bindings, tasks=()):
    missing = ['Есть только начало вызова, его результат не записан'] if bindings and all(
        b.get('moment') == 'before_action' for b in bindings) else []
    return {'action': 'review', 'operation_id': packet['operation']['id'], 'bindings': bindings, 'missing': missing,
            'review_tasks': list(tasks), 'limitations': [], 'changes': []}


class JavaWrapperReview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def run_review(self, callback, *, files=FILES, operation=None, contracts=CONTRACT, replace=None):
        for path, text in files.items():
            if replace: text = text.replace(*replace)
            target = self.root/path; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(text)
        operation = operation or node(GATEWAY, files[GATEWAY], 10, 15)
        view = MapView.from_dict({'schema_version': '2.0', 'scenario': {}, 'sources': [{'id': 'source-1'}],
                                  'nodes': [operation], 'edges': [], 'run': {'id': 'java-wrapper', 'status': 'completed'}})
        snapshot = snapshot_repository(self.root, max_files=None, max_chars=None, logging_calls=['journal.record'])
        model = Scripted(callback)
        return model, review_logging(view, snapshot, model, logging_contracts=contracts)

    def test_inherited_wrapper_chain_to_a_declared_contract_can_cover_a_confirmed_result(self):
        model, result = self.run_review(lambda p, n: reply(p, [bind(p, 13, role='result', moment='after_confirmation')]))
        row = result['operations'][0]
        self.assertEqual(row['state'], 'C', row['review_tasks'])
        self.assertEqual(len(model.calls), 1)
        chain = row['bindings'][0]['chain']
        self.assertEqual(chain['basis'], 'declared_contract')
        self.assertEqual(chain['terminal']['callee'], 'Telemetry.Records.emitInfo')
        self.assertTrue(chain['limitations'])
        self.assertTrue(all(value in row['limitations'] for value in chain['limitations']))
        self.assertFalse([p for p in row['search']['pending'] if 'не поддержана' in p])
        validate_saved_rows(MapView.from_dict(result['map']), result)

    def test_pre_call_wrapper_record_is_only_partial_for_the_command(self):
        model, result = self.run_review(lambda p, n: reply(p, [bind(p, 11, role='request', moment='before_action')]))
        self.assertEqual(result['operations'][0]['state'], 'P')

    def test_wrapper_without_a_declared_contract_is_not_a_log(self):
        model, result = self.run_review(lambda p, n: reply(p, [bind(p, 13, role='result', moment='after_confirmation')]),
                                        contracts=[])
        row = result['operations'][0]
        self.assertEqual(row['state'], 'X')
        self.assertTrue(any('контракт журналирования для него не объявлен' in p for p in row['search']['pending']))
        self.assertIn('не подтверждено', model.calls[1]['repair_errors'][0])

    def test_log_suffix_name_without_contract_is_never_proof(self):
        model, result = self.run_review(lambda p, n: reply(p, [bind(p, 13, role='result', moment='after_confirmation')]),
                                        replace=('emitInfo', 'infoLog'))
        self.assertEqual(result['operations'][0]['state'], 'X')

    def test_uncited_hop_rejects_the_binding(self):
        model, result = self.run_review(lambda p, n: reply(p, [bind(p, 13, role='result', moment='after_confirmation',
                                                                   hops=lambda w: w[1:])]))
        self.assertEqual(result['operations'][0]['state'], 'X')
        self.assertEqual(len(model.calls), 2)

    def test_command_chain_is_delivered_and_its_invocation_route_is_source_bound(self):
        handler = 'src/com/example/handlers/RefusalHandler.java'
        files = {**FILES, **COMMAND_FILES}
        operation = node(handler, files[handler], 12, 14, identifier='refuse', outcome='refusal')
        call = {'path': handler, 'start_line': 13, 'end_line': 13}
        route = [call, {'path': 'src/com/example/ops/TaskOps.java', 'start_line': 13, 'end_line': 13},
                 {'path': 'src/com/example/ops/RefuseCommand.java', 'start_line': 17, 'end_line': 17}]
        def callback(packet, number):
            self.assertTrue(any(s['path'].endswith('HttpDocumentClient.java') for s in packet['candidates']),
                            'the delegated client body with its record must be delivered')
            return reply(packet, [bind(packet, 14, role='request', moment='before_action', invocation=call, arguments=route,
                                       suffix='HttpDocumentClient.java')])
        model, result = self.run_review(callback, files=files, operation=operation)
        row = result['operations'][0]
        self.assertEqual(row['state'], 'P', row['review_tasks'])
        self.assertIsNotNone(row['bindings'][0].get('route'))
        self.assertTrue(row['bindings'][0]['route']['limitations'])  # interface dispatch is a stated limit

    def test_unread_route_hop_is_rejected(self):
        handler = 'src/com/example/handlers/RefusalHandler.java'
        files = {**FILES, **COMMAND_FILES}
        operation = node(handler, files[handler], 12, 14, identifier='refuse', outcome='refusal')
        call = {'path': handler, 'start_line': 13, 'end_line': 13}
        def callback(packet, number):
            return reply(packet, [bind(packet, 14, role='request', moment='before_action', invocation=call, arguments=[call],
                                       suffix='HttpDocumentClient.java')])
        model, result = self.run_review(callback, files=files, operation=operation)
        self.assertEqual(result['operations'][0]['state'], 'X')


if __name__ == '__main__': unittest.main()
