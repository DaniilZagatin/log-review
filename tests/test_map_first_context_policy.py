"""Cross-domain source-path and coverage controls, no model/network/target execution."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from process_observability.map_input import MapView, RepoContext
from process_observability.source_tools import snapshot_repository
from process_observability.logging_review import _Navigator, ReviewOptions, _validate_reply, reconcile
from process_observability.log_discovery import discover_logging
from process_observability.operation_coverage import sufficient_bindings, coverage_limits, shared_pair_allowed
from process_observability.operation_coverage import validate_saved_rows
from process_observability.logging_contracts import POLICY, POLICY_2, LoggingReviewError
from test_map_first_context_reuse import PYTHON, JAVA
from test_map_first_logging import CODE, operation, model_reply, Scripted
from process_observability.logging_review import review_logging
from process_observability.logging_report import validate_report


class ContextPolicyControls(unittest.TestCase):
    def review(self, code=PYTHON, path='Clinic.py', first=8, last=10, explicit=True, role='result', other_anchor=False, include_signature=False):
        if path.endswith('.py'):code=code.replace('journal','logger')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/path).write_text(code)
            op={'id':'check','kind':'operation','name':'Проверить полученный объект','outcome':'success',
                'effect_kind':'result','evidence':[{'path':'source-1/'+path,'line_start':first,'line_end':last,
                'quote':'\n'.join(code.splitlines()[first-1:last])}]}
            if include_signature:op['evidence'].append({'path':'source-1/'+path,'line_start':3,'line_end':3,'quote':code.splitlines()[2]})
            ops=[op]
            if other_anchor:
                end=next(i for i,line in enumerate(code.splitlines(),1) if line.startswith('def collect'))-1
                ops.append({'id':'details','kind':'operation','name':'Прочитать детали','evidence':[{'path':'source-1/'+path,'line_start':end if other_anchor=='narrow' else 3,'line_end':end,'quote':'\n'.join(code.splitlines()[end-1:end] if other_anchor=='narrow' else code.splitlines()[2:end])}]})
            view=MapView.from_dict({'schema_version':'2.0','sources':[{'id':'source-1'}],
                'scenario':{},'nodes':ops,'edges':[],'run':{'id':'synthetic','status':'completed'}})
            snap=snapshot_repository(root,max_files=None,max_chars=None,logging_calls=['journal.info'])
            context=RepoContext(snap,view)
            inv=discover_logging(snap,call_names=snap.logging_calls,max_sites=None,max_per_file=None,max_hints=None,
                                 include_columns=True,exclude_declarations=True)
            packet=_Navigator(context,inv,ReviewOptions()).initial(view.operations[0])
            if packet['downstream_context']['delivered']:
                entry=packet['downstream_context']['delivered'][0]
                site=next(s for s in packet['candidates'] if s['id'] in entry['candidate_site_ids'])
            else:
                site=next(s for s in packet['candidates'] if 'appointment key' in str(s.get('event_key')))
                line=next(i for i,v in enumerate(code.splitlines(),1) if 'return appointment_details(' in v)
                entry={'invocation':{'path':path,'start_line':line,'end_line':line}}
            binding={'site_id':site['id'],'role':role,'moment':'after_action','meaning':'ID поступившего объекта',
                     'conditions':'После проверки ID','correlation':'Переданный ID','establishes':['occurrence','result'],
                     'covers_outcomes':['success']}
            if explicit:binding['context_call_id']=entry['context_call_id']
            else:
                binding.update(invocation_ref=entry['invocation'],argument_evidence=[entry['invocation']])
            reply={'action':'review','operation_id':'check','bindings':[binding],'missing':[],
                   'review_tasks':[],'limitations':[],'changes':[],
                   'candidate_reviews':[{'site_id':s['id'],'classification':'technical_log','reason':'Другой факт'}
                                        for s in packet['candidates'] if s['id']!=site['id']]}
            if role == 'request': reply['missing'] = ['Запрос не подтверждает последующий результат']
            row=_validate_reply(reply,packet,context)
            self.saved={'policy':POLICY,'map':view.original,'inventory':inv,'operations':[row],
                        'source_fingerprints':snap.fingerprints}
            return row, view.original

    def test_explicit_python_and_java_context_uses_exact_entry_and_path(self):
        for code,path,first,last in [(PYTHON,'Clinic.py',8,10),(JAVA,'Library.java',10,13)]:
            with self.subTest(path=path):
                row,model=self.review(code,path,first,last)
                self.assertEqual(row['state'],'C',row)
                basis=row['bindings'][0]['context_basis']
                self.assertEqual(basis['status'],'supported',basis)
                self.assertEqual(basis['caller']['path_conditions'][0]['branch'],'false')
                self.assertFalse(basis['semantic_verified'])

    def test_ordinary_unanchored_helper_keeps_source_wrapper_rules(self):
        row,_=self.review(explicit=False)
        self.assertEqual(row['state'],'C')
        self.assertNotIn('context_basis',row['bindings'][0])
        self.assertFalse(row['missing'])
        self.assertFalse(any(t['kind']=='instrumentation' for t in row['review_tasks']))

    def test_request_does_not_prove_following_result(self):
        row,_=self.review(role='request')
        self.assertEqual(row['state'],'P')

    def test_intervening_reassignment_prevents_prior_result_claim(self):
        code=PYTHON.replace('    return appointment_details(item.key)','    item=gateway.other()\n    return appointment_details(item.key)')
        row,_=self.review(code)
        self.assertEqual(row['state'],'P')
        self.assertIn('not_immediately_after_operation_anchor',row['bindings'][0]['context_basis']['reasons'])

    def test_guard_before_callee_record_is_not_unconditional_entry(self):
        code=PYTHON.replace('    journal.info("appointment key %s",key)',
                            '    if key is None: return\n    journal.info("appointment key %s",key)')
        row,_=self.review(code,first=9,last=11)
        self.assertEqual(row['state'],'P')
        self.assertIn('record_not_at_unconditional_entry',row['bindings'][0]['context_basis']['reasons'])

    def test_unsupported_caller_does_not_become_supported(self):
        code=PYTHON.replace('    return appointment_details(item.key)',
                            '    try:\n        return appointment_details(item.key)\n    except Exception:\n        return')
        row,_=self.review(code)
        self.assertEqual(row['state'],'P')

    def test_choice_requires_both_known_sides_and_legacy_unchanged(self):
        row,model=self.review()
        op=model['nodes'][0];op.update(continuation_mode='choice',outcome='unknown')
        binding=row['bindings'][0]
        self.assertFalse(sufficient_bindings(op,[binding],model))
        self.assertTrue(sufficient_bindings(op,[binding],model,POLICY_2))
        second=deepcopy(binding);second.pop('context_basis');second['site_id']='negative'
        second['flow_context']['path_conditions'][0]['branch']='true'
        self.assertTrue(sufficient_bindings(op,[binding,second],model))
        second['flow_context']['path_conditions'][0]['branch']='false'
        self.assertFalse(sufficient_bindings(op,[binding,second],model))

    def test_shared_mutual_pair_is_non_sufficient_and_never_missing(self):
        row,model=self.review()
        a=row['bindings'][0]
        # Different own source anchors sharing a physical record, no contextual route.
        a.pop('context_basis');a['invocation_ref']=None
        a['operation_anchors']=deepcopy(a['evidence'])
        a.update(shared_with='other',shared_meaning='Один факт для двух соседних операций')
        other=deepcopy(row);other['operation_id']='other';b=other['bindings'][0]
        b.update(operation_id='other',shared_with='check')
        b['operation_anchors'][0]['start_line']-=1
        model['edges']=[{'id':'e','source':'check','target':'other'}]
        group=[(row,a),(other,b)]
        self.assertTrue(shared_pair_allowed(group,model))
        self.assertFalse(sufficient_bindings(model['nodes'][0],[a],model))
        for mutation in ('no_edge','identical','unilateral','triple'):
            g=deepcopy(group);m=deepcopy(model)
            if mutation=='no_edge':m['edges']=[]
            if mutation=='identical':g[1][1]['operation_anchors']=deepcopy(g[0][1]['operation_anchors'])
            if mutation=='unilateral':g[1][1].pop('shared_with')
            if mutation=='triple':g.append(deepcopy(g[0]))
            self.assertFalse(shared_pair_allowed(g,m),mutation)
        self.assertFalse(shared_pair_allowed(group,model,POLICY_2))

    def test_saved_context_reapplies_host_policy_and_rejects_certificate_removal(self):
        row,model=self.review();view=MapView.from_dict(model)
        validate_saved_rows(view,self.saved)
        bad=deepcopy(self.saved);bad['operations'][0]['bindings'][0].pop('context_basis')
        with self.assertRaises(LoggingReviewError):validate_saved_rows(view,bad)
        code=PYTHON.replace('    journal.info("appointment key %s",key)','    if key is None: return\n    journal.info("appointment key %s",key)')
        row,model=self.review(code,first=9,last=11);view=MapView.from_dict(model)
        validate_saved_rows(view,self.saved)
        bad=deepcopy(self.saved);bad['operations'][0]['state']='C'
        with self.assertRaises(LoggingReviewError):validate_saved_rows(view,bad)

    def test_negative_branch_can_cover_its_own_choice_fact(self):
        row,model=self.review();op=model['nodes'][0];op.update(continuation_mode='choice',outcome='unknown')
        positive=row['bindings'][0]
        negative=deepcopy(positive);negative.pop('context_basis');negative.update(role='no_op',covers_outcomes=['no_decision'])
        negative['flow_context']['path_conditions'][0]['branch']='true'
        self.assertTrue(sufficient_bindings(op,[positive,negative],model))

    def test_shared_pair_live_and_saved_use_the_same_rules(self):
        for edge,mutual in [(True,True),(False,True),(True,False)]:
            with self.subTest(edge=edge,mutual=mutual), tempfile.TemporaryDirectory() as d:
                root=Path(d);(root/'app.py').write_text(CODE)
                ops=[operation(identifier='a',first=3,last=5),operation(identifier='b',first=4,last=5)]
                view=MapView.from_dict({'schema_version':'2.0','scenario':{},'sources':[{'id':'source-1'}],
                    'nodes':ops,'edges':[{'id':'edge','source':'a','target':'b'}] if edge else [],
                    'run':{'id':'synthetic','status':'completed'}})
                def response(packet,n):
                    reply=model_reply(packet)
                    if mutual or packet['operation']['id']=='a':
                        reply['bindings'][0].update(shared_with='b' if packet['operation']['id']=='a' else 'a',
                            shared_meaning='Общая запись по результату последовательных действий')
                    return reply
                result=review_logging(view,snapshot_repository(root,max_files=None,max_chars=None),Scripted(response))
                self.assertEqual([r['state'] for r in result['operations']],['P','P'] if mutual else ['P','C'])
                validate_report(result)
                if edge and mutual:
                    self.assertTrue(all(not r['missing'] for r in result['operations']))
                    bad=deepcopy(result);bad['operations'][0]['state']='C'
                    with self.assertRaises(LoggingReviewError):validate_saved_rows(view,bad)

    def test_separate_sufficient_binding_can_cover_despite_shared_record(self):
        row,model=self.review();op=model['nodes'][0]
        independent=deepcopy(row['bindings'][0]);shared=deepcopy(independent)
        shared.update(shared_with='other',shared_meaning='Общий факт')
        self.assertEqual(sufficient_bindings(op,[shared,independent],model),[independent])


if __name__=='__main__':unittest.main()
