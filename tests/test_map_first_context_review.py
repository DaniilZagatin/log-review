"""Independent regressions for material Q15 peer-review findings."""
from pathlib import Path
from copy import deepcopy
import tempfile
import unittest
import test_map_first_context_policy as policy
import test_map_first_context_reuse as reuse
from test_map_first_context_reuse import PYTHON
from test_map_first_logging import CODE, operation, Scripted, model_reply
from process_observability.map_input import MapView
from process_observability.source_tools import snapshot_repository
from process_observability.logging_review import review_logging
from process_observability.logging_report import validate_report, write_report


class ContextReviewControls(unittest.TestCase):
    def test_stopped_unstarted_choice_still_exports(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();(root/'app.py').write_text(CODE)
            ops=[operation(identifier='a'),operation(identifier='b')]
            for op in ops:op.update(continuation_mode='choice',outcome='unknown')
            view=MapView.from_dict({'schema_version':'2.0','sources':[{'id':'source-1'}],'scenario':{},
                'nodes':ops,'edges':[],'run':{'id':'test','status':'completed'}})
            result=review_logging(view,snapshot_repository(root,max_files=None,max_chars=None),
                                  Scripted(lambda p,n:model_reply(p)),on_checkpoint=lambda b:False)
            validate_report(result)
            self.assertEqual(result['operations'][1]['state'],'X')
            write_report(result,root/'export')
            self.assertTrue((root/'export/report.html').is_file())

    def test_only_continuations_use_neighbor_slots(self):
        code=('import logging\nlogger=logging.getLogger(__name__)\n'
              'def earlier():\n    logger.info("old")\n'
              'def later():\n    logger.info("new")\n'
              'def process():\n    earlier()\n    earlier()\n    earlier()\n    item=queue.next()\n    later()\n')
        packet=reuse.ContextReuseControls().packet('x.py',code,11,11)
        self.assertEqual([e['invocation']['start_line'] for e in packet['downstream_context']['delivered']],[12])
        self.assertFalse(packet['downstream_context']['frontier'])

    def test_leading_docstring_is_not_an_effect_before_entry_record(self):
        code=PYTHON.replace('def appointment_details(key):','def appointment_details(key):\n    """Read an appointment."""')
        row,_=policy.ContextPolicyControls().review(code,first=9,last=11)
        self.assertEqual(row['state'],'C',row['bindings'][0].get('context_basis'))

    def test_first_statement_of_positive_branch_has_condition_basis(self):
        code=PYTHON.replace('    if item is None:\n        journal.info("none available")\n        return\n    return appointment_details(item.key)',
                            '    if item is not None:\n        return appointment_details(item.key)')
        row,_=policy.ContextPolicyControls().review(code,first=8,last=8)
        self.assertEqual(row['state'],'C',row['bindings'][0].get('context_basis'))

    def test_broad_own_anchor_does_not_bypass_other_operation_entry_check(self):
        code=PYTHON.replace('    journal.info("appointment key %s",key)',
                            '    if key is None: return\n    journal.info("appointment key %s",key)')
        row,_=policy.ContextPolicyControls().review(code,first=9,last=12,explicit=False,other_anchor=True)
        self.assertEqual(row['state'],'P')
        self.assertIn('record_not_at_unconditional_entry',row['bindings'][0]['context_basis']['reasons'])

    def test_foreign_member_with_narrow_action_quote_still_needs_entry_proof(self):
        code=PYTHON.replace('    journal.info("appointment key %s",key)',
                            '    if key is None: return\n    journal.info("appointment key %s",key)')
        row,_=policy.ContextPolicyControls().review(code,first=9,last=11,explicit=False,other_anchor='narrow')
        self.assertEqual(row['state'],'P')
        self.assertIn('record_not_at_unconditional_entry',row['bindings'][0]['context_basis']['reasons'])

    def test_own_signature_anchor_does_not_cancel_foreign_log_line(self):
        code=PYTHON.replace('    journal.info("appointment key %s",key)',
                            '    if key is None: return\n    journal.info("appointment key %s",key)')
        row,_=policy.ContextPolicyControls().review(code,first=9,last=11,explicit=False,other_anchor=True,include_signature=True)
        self.assertEqual(row['state'],'P')
        self.assertIn('record_not_at_unconditional_entry',row['bindings'][0]['context_basis']['reasons'])

    def test_optional_columns_cannot_hide_a_shared_physical_record(self):
        from process_observability.logging_review import reconcile
        rows=[{'operation_id':i,'state':'P','review_tasks':[],'bindings':[{'site_id':'s',
            'invocation_ref':{'path':'x.py','start_line':10,'end_line':10,**extra}}]}
            for i,extra in [('a',{'column':4}),('b',{})]]
        reconcile(rows)
        self.assertEqual([r['state'] for r in rows],['X','X'])

    def test_reading_frontier_code_and_candidates_clears_pending(self):
        from process_observability.logging_context import refresh_frontier
        from types import SimpleNamespace
        entry={'reason':'context_call_limit','target':{'path':'x.py','start_line':7,'end_line':9},'candidate_site_ids':['s']}
        packet={'downstream_context':{'frontier':[entry]},'candidates':[],
                'search':{'pending':['Не завершена проверка соседнего контекста с кандидатом журнала: x.py:7 (context_call_limit)']}}
        context=SimpleNamespace(shown=[entry['target']])
        refresh_frontier(packet,context);self.assertTrue(packet['search']['pending'])
        packet['candidates']=[{'id':'s'}];refresh_frontier(packet,context)
        self.assertEqual(packet['search']['pending'],[])
        self.assertEqual(packet['downstream_context']['frontier'],[])

    def test_saved_caller_and_emit_must_cover_their_real_refs(self):
        helper=policy.ContextPolicyControls();row,model=helper.review()
        from process_observability.operation_coverage import validate_saved_rows
        from process_observability.logging_contracts import LoggingReviewError
        for which in ('caller','emit'):
            bad=deepcopy(helper.saved);bad['operations'][0]['bindings'][0]['context_basis'][which]['statement']['start_line']=99
            with self.assertRaises(LoggingReviewError):validate_saved_rows(MapView.from_dict(model),bad)

    def test_call_between_anchors_and_same_line_call_are_not_silently_lost(self):
        from process_observability.map_input import RepoContext
        from process_observability.logging_review import _Navigator,ReviewOptions
        from process_observability.log_discovery import discover_logging
        for same_line in (False,True):
            with self.subTest(same_line=same_line),tempfile.TemporaryDirectory() as d:
                code=('import logging\nlogger=logging.getLogger(__name__)\ndef detail(item):\n'
                      '    logger.info("detail %s",item)\ndef process(item):\n'
                      +('    item.change(); detail(item)\n' if same_line else
                        '    item.change()\n    detail(item)\n    item.finish()\n'))
                root=Path(d);(root/'x.py').write_text(code)
                evidence=[{'path':'s/x.py','line_start':n,'line_end':n,'quote':code.splitlines()[n-1]}
                          for n in ([6] if same_line else [6,8])]
                view=MapView.from_dict({'schema_version':'2.0','sources':[{'id':'s'}],'scenario':{},
                    'nodes':[{'id':'act','kind':'operation','name':'Изменить объект','evidence':evidence}],
                    'edges':[],'run':{'id':'fixture','status':'completed'}})
                snap=snapshot_repository(root,max_files=None,max_chars=None)
                inv=discover_logging(snap,call_names=snap.logging_calls,max_sites=None,max_per_file=None,max_hints=None)
                packet=_Navigator(RepoContext(snap,view),inv,ReviewOptions()).initial(view.operations[0])
                self.assertTrue(any(s['start_line']==4 for s in packet['candidates']))

if __name__=='__main__':unittest.main()
