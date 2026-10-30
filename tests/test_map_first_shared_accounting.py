"""Cross-domain shared-source accounting; fixtures are read as text, never executed."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import test_map_first_context_policy as context_fixture
from test_map_first_context_reuse import PYTHON, JAVA
from test_map_first_logging import CODE, operation, model_reply, Scripted
from process_observability.map_input import MapView
from process_observability.source_tools import snapshot_repository
from process_observability.logging_review import review_logging, reconcile, expected_signal
from process_observability.operation_coverage import validate_saved_rows
from process_observability.logging_contracts import LoggingReviewError
from process_observability.logging_report import validate_report

REUSE_POLICY = 'independent-source-uses/1'


class SharedRecordAccounting(unittest.TestCase):
    def own(self, count=2, reply_fn=None):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'app.py').write_text(CODE)
            ops=[operation(identifier=str(i),first=3,last=5) for i in range(count)]
            view=MapView.from_dict({'schema_version':'2.0','scenario':{},'sources':[{'id':'source-1'}],
                'nodes':ops,'edges':[],'run':{'id':'synthetic','status':'completed'}})
            result=review_logging(view,snapshot_repository(root,max_files=None,max_chars=None),
                                 Scripted(reply_fn or (lambda p,n:model_reply(p))))
            return view,result

    def context(self, java=False):
        helper=context_fixture.ContextPolicyControls()
        row,model=helper.review(JAVA,'Library.java',10,13) if java else helper.review(PYTHON)
        data=deepcopy(helper.saved); other=deepcopy(row); other['operation_id']='checked'
        node=deepcopy(model['nodes'][0]); node['id']='checked'; model['nodes'].append(node)
        other['expected_signal']=expected_signal(node)
        for b in other['bindings']:b['operation_id']='checked'
        data['map']=model; data['operations'].append(other); data['reconcile_policy']=REUSE_POLICY
        return MapView.from_dict(model),data

    def apply(self, data):
        reconcile(data['operations'],data['map'],reconcile_policy=REUSE_POLICY,
                  sites={s['id']:s for s in data['inventory']['sites']})

    def test_two_overlapping_operations_keep_independent_coverage(self):
        view,data=self.own()
        self.assertEqual([r['state'] for r in data['operations']],['C','C'])
        self.assertEqual(data.get('reconcile_policy'),REUSE_POLICY)
        validate_report(data)
        self.assertEqual(data['summary']['denominator'],2)

    def test_three_uses_do_not_require_mutual_prediction_or_map_edges(self):
        view,data=self.own(3)
        self.assertEqual([r['state'] for r in data['operations']],['C']*3)
        validate_report(data)
        self.assertEqual(sum(s['review_status']=='mapped' for s in data['inventory']['sites']),1)

    def test_a_shared_negative_fact_can_cover_negative_operations(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'app.py').write_text(CODE)
            ops=[operation(identifier=i,first=6,last=7,outcome='refusal',effect='decision') for i in ('a','b')]
            view=MapView.from_dict({'schema_version':'2.0','scenario':{},'sources':[{'id':'source-1'}],
                'nodes':ops,'edges':[],'run':{'id':'synthetic','status':'completed'}})
            data=review_logging(view,snapshot_repository(root,max_files=None,max_chars=None),
                                Scripted(lambda p,n:model_reply(p,key='denied',role='error')))
            self.assertEqual([r['state'] for r in data['operations']],['C','C'])
            validate_report(data)

    def test_exact_context_witnesses_allow_python_and_java(self):
        for java in (False,True):
            with self.subTest(java=java):
                view,data=self.context(java); before=deepcopy(data['operations'])
                self.apply(data)
                self.assertEqual(data['operations'],before)
                validate_saved_rows(view,data)

    def test_negative_role_cannot_cover_another_success_but_does_not_poison_valid_use(self):
        for role in ('error','repeat','no_op'):
            with self.subTest(role=role):
                view,data=self.own(reply_fn=lambda p,n:model_reply(p,role='result' if n==1 else role))
                self.assertEqual([r['state'] for r in data['operations']],['C','P'])
                validate_report(data)

    def test_intent_can_coexist_but_does_not_become_sufficient(self):
        view,data=self.own(reply_fn=lambda p,n:model_reply(p,role='result' if n==1 else 'request'))
        self.apply(data)
        self.assertEqual([r['state'] for r in data['operations']],['C','P'])
        validate_saved_rows(view,data)

    def test_one_uncertified_context_blocks_reuse(self):
        view,data=self.context();data['operations'][1]['bindings'][0]['context_basis']['status']='not_established'
        self.apply(data);self.assertEqual([r['state'] for r in data['operations']],['C','X'])

    def test_unanchored_site_cannot_use_broad_evidence_as_ownership(self):
        view,data=self.own();binding=data['operations'][1]['bindings'][0]
        binding['operation_anchors']=[dict(binding['evidence'][0],start_line=1,end_line=2)]
        binding['evidence'][0]['start_line']=1  # quote-range overlap cannot substitute the actual site.
        self.apply(data);self.assertEqual([r['state'] for r in data['operations']],['C','X'])

    def test_missing_site_inventory_is_not_a_witness(self):
        view,data=self.own();data['inventory']['sites']=[]
        self.apply(data);self.assertTrue(all(r['state']=='X' for r in data['operations']))

    def test_not_log_conflict_is_preserved(self):
        view,data=self.own();site=data['operations'][0]['bindings'][0]['site_id']
        data['operations'][1]['candidate_reviews'].append({'site_id':site,'classification':'not_log','reason':'Команда'})
        self.apply(data);self.assertTrue(all(r['state']=='X' for r in data['operations']))

    def test_old_saved_policy_keeps_old_rule(self):
        view,data=self.own();data.pop('reconcile_policy',None)
        for row in data['operations']:row['state']='C';row['review_tasks']=[]
        with self.assertRaises(LoggingReviewError):validate_saved_rows(view,data)
        reconcile(data['operations'],data['map'])
        self.assertTrue(all(r['state']=='X' for r in data['operations']))
        validate_saved_rows(view,data)

    def test_saved_new_policy_rejects_conflict_and_unknown_version(self):
        view,data=self.own()
        data['operations'][1]['bindings'][0].update(role='error',moment='after_action')
        with self.assertRaises(LoggingReviewError):validate_saved_rows(view,data)
        view,data=self.own();data['reconcile_policy']='independent-source-uses/999'
        with self.assertRaises(LoggingReviewError):validate_saved_rows(view,data)

    def test_raw_model_reply_and_checkpoints_are_not_rewritten(self):
        replies=[]
        def response(packet,n):
            reply=model_reply(packet);replies.append(deepcopy(reply));return reply
        view,data=self.own(reply_fn=response)
        self.assertEqual([c['reply'] for c in data['checkpoints']],replies)
        self.assertTrue(all('shared_with' not in b for r in data['operations'] for b in r['bindings']))

    def test_explicit_shared_opt_in_keeps_non_sufficient_legacy_semantics(self):
        def response(p,n):
            r=model_reply(p);r['bindings'][0].update(shared_with='1' if n==1 else '0',shared_meaning='Общий факт')
            return r
        view,data=self.own(reply_fn=response)
        self.assertEqual([r['state'] for r in data['operations']],['P','P']) # Each declared shared use remains non-sufficient.
        validate_report(data)

    def test_one_explicit_shared_use_does_not_poison_other_two(self):
        def response(p,n):
            r=model_reply(p)
            if n==3:r['bindings'][0].update(shared_with='0',shared_meaning='Общий факт')
            return r
        view,data=self.own(3,response)
        self.assertEqual([r['state'] for r in data['operations']],['C','C','P'])
        validate_report(data)

    def test_three_relative_roles_can_describe_one_negative_fact(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'app.py').write_text(CODE)
            ops=[operation(identifier=i,first=6,last=7,outcome='no_decision',effect='decision') for i in ('a','b','c')]
            view=MapView.from_dict({'schema_version':'2.0','scenario':{},'sources':[{'id':'source-1'}],
                'nodes':ops,'edges':[],'run':{'id':'synthetic','status':'completed'}})
            data=review_logging(view,snapshot_repository(root,max_files=None,max_chars=None),
                Scripted(lambda p,n:model_reply(p,key='denied',role=['result','error','no_op'][n-1])))
            self.assertEqual([r['state'] for r in data['operations']],['C']*3)
            validate_report(data)


if __name__=='__main__':unittest.main()
