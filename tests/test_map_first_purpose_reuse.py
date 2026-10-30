"""Proposal availability is independent of which operation owns a call site.

Synthetic adapter and command sources are parsed, never executed.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from process_observability.logging_review import review_logging, ReviewOptions
from process_observability.map_input import MapView
from process_observability.source_tools import snapshot_repository
from process_observability.operation_coverage import validate_saved_rows
from test_map_first_logging import Scripted
from test_map_first_wrapper_review import FILES, GATEWAY, node, bind, reply
from test_map_first_takeover import BASE, PORT


class PurposeReuseControls(unittest.TestCase):
    def run_case(self, different_target=False, mode=None):
        files = deepcopy(FILES)
        files[PORT] = ('package com.example.journal;\npublic interface RecordingPort {\n'
                       '    void write(String text, int level);\n}\n')
        files[BASE] = files[BASE].replace('class BaseJournal {', 'class BaseJournal implements RecordingPort {')
        files[BASE] = files[BASE].replace('protected void write', 'public void write')
        if different_target:
            files[GATEWAY] = files[GATEWAY].replace('private final Transport transport;',
                'private final Transport transport; CommandBus bus;').replace(
                'journal.record("Отправка подтверждения " + orderId, 1, true);',
                'bus.record("Отправка подтверждения " + orderId, 1, true);')
            files['src/com/example/orders/CommandBus.java'] = (
                'package com.example.orders;\nclass CommandBus {\n'
                ' void record(String value, int level, boolean flag) { publisher.send(value); }\n}\n')
        if mode == 'overload':
            files[GATEWAY] = files[GATEWAY].replace('orderId, 1, true)', 'orderId, 1)')
        if mode == 'closed_local':
            files[GATEWAY] = files[GATEWAY].replace(
                'journal.record("Отправка подтверждения " + orderId, 1, true);',
                '{ Journal journal = new Journal(); journal.record("Отправка подтверждения " + orderId, 1, true); }')
            files[GATEWAY] = files[GATEWAY].replace('package com.example.orders;',
                'package com.example.orders; import com.example.journal.Journal;')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for path, code in files.items():
                out = root/path; out.parent.mkdir(parents=True, exist_ok=True); out.write_text(code)
            view = MapView.from_dict({'schema_version':'2.0','sources':[{'id':'source-1'}],'scenario':{},
                'nodes':[node(GATEWAY, files[GATEWAY], 10, 15)],'edges':[],
                'run':{'id':'synthetic-purpose','status':'completed'}})
            snap = snapshot_repository(root, max_files=None, max_chars=None,
                                       logging_calls=['journal.record','bus.record'])
            def answer(packet, number):
                old = next(s for s in packet['candidates'] if s['path']==GATEWAY and s['start_line']==11)
                selected = next(s for s in packet['candidates'] if s['path']==GATEWAY and s['start_line']==13)
                binding = bind(packet, 13, role='result', moment='after_confirmation')
                binding['emission_evidence'] = []
                binding['source_contract'] = {
                    'candidate_id':selected['logging_chain']['adapter_candidates'][0]['contract_basis']['candidate_id'],
                    'meaning':'Интерфейс и реализация передают текст в журнал через внешний SDK'}
                value = reply(packet,[binding]); value['missing']=['В сообщении нет версии результата']
                value['candidate_reviews'] = [{'site_id':old['id'],'classification':'other_operation_log',
                    'reason':'Вызов перед действием; не свидетельствует о результате текущего действия'}]
                if mode == 'not_log':
                    witness = old['logging_chain']['adapter_candidates'][0]['hops'][0]
                    value['candidate_reviews'][0].update(classification='not_log',
                        evidence=[{k:old[k] for k in ('path','start_line','end_line')},
                                  {k:witness[k] for k in ('path','start_line','end_line')}])
                value['changes'] = [{'id':'version','kind':'add_event','operation_ids':['confirm'],
                    'problem':'Не записана версия результата','what':'Записать версию после завершения',
                    'why':'Различать результаты повторных обработок','fields':[],
                    'location':{'path':GATEWAY,'start_line':14,'end_line':14},
                    'placement':'before','guards':[],'timing':'Перед возвратом, после результата',
                    'verification':'Запись содержит версию; не появляется при ошибке',
                    'existing_site_ids':[selected['id']],'dependencies':[],
                    'logger':{'expression':old['callee'],'evidence':[
                        {'path':GATEWAY,'start_line':11,'end_line':11}]}}]
                return value
            model = Scripted(answer)
            result = review_logging(view,snap,model,options=ReviewOptions(stop_on_failed_repair=True))
            validate_saved_rows(view,result)
            return model,result

    def test_same_resolved_adapter_does_not_require_false_operation_binding(self):
        model,result = self.run_case()
        self.assertEqual(len(model.calls),1, result.get('stop_reason'))
        row = result['operations'][0]
        self.assertEqual(row['state'],'P')
        self.assertEqual(len(row['bindings']),1)
        self.assertEqual(row['bindings'][0]['evidence'][0]['start_line'],13)
        access = row['changes'][0]['logger_access']
        self.assertEqual(access['status'],'available')
        self.assertEqual(access['purpose_basis'],'model_interpretation_of_source')
        self.assertEqual(access['purpose_transferred_from'],row['bindings'][0]['site_id'])
        self.assertTrue(all('_validated_logging_binding' not in s for s in result['inventory']['sites']))

    def test_same_member_spelling_on_business_receiver_is_not_a_logger(self):
        model,result = self.run_case(different_target=True)
        self.assertEqual(len(model.calls),2)
        self.assertEqual(result['stop_reason']['kind'],'repair_failed')
        self.assertEqual(result['operations'][0]['state'],'X')

    def test_another_overload_or_closed_local_does_not_borrow_the_purpose(self):
        for mode in ('overload','closed_local','not_log'):
            with self.subTest(mode=mode):
                model,result = self.run_case(mode=mode)
                self.assertEqual(len(model.calls),2)
                self.assertEqual(result['stop_reason']['kind'],'repair_failed')
                self.assertEqual(result['operations'][0]['changes'],[])
                reason='outside_member_scope' if mode=='closed_local' else 'logging_purpose_unresolved'
                self.assertTrue(any('change.logger.expression: '+reason in e for e in result['stop_reason']['errors']))


if __name__ == '__main__': unittest.main()
