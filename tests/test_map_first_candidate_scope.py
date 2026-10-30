"""Candidate completeness is scoped to the operation; SDK implementation and callers are not new actions."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from process_observability.logging_review import review_logging
from process_observability.map_input import MapView
from process_observability.operation_coverage import validate_saved_rows
from process_observability.source_tools import snapshot_repository
from test_map_first_logging import Scripted, model_reply, operation
from test_map_first_takeover import BASE, PORT
from test_map_first_wrapper_review import FILES, GATEWAY, node, bind, reply


class CandidateScopeControls(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def java_review(self, extra=None):
        files = {**FILES, PORT: 'package com.example.journal;\npublic interface RecordingPort {\n'
                                 '    /** Записать текст в журнал. */\n    void write(String text, int level);\n}\n'}
        files[BASE] = files[BASE].replace('class BaseJournal {', 'class BaseJournal implements RecordingPort {').replace('protected void write', 'public void write')
        if extra: files.update(extra)
        for path, content in files.items():
            target = self.root/path; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(content)
        view = MapView.from_dict({'schema_version':'2.0', 'sources':[{'id':'source-1'}], 'scenario':{},
            'nodes':[node(GATEWAY, files[GATEWAY], 10, 15)], 'edges':[], 'run':{'id':'synthetic','status':'completed'}})
        snap = snapshot_repository(self.root, max_files=None, max_chars=None,
            logging_calls=['journal.record', 'super.write', 'Telemetry.Records.emitInfo', 'publisher.publish'])
        def callback(packet, number):
            bindings = []
            for line in (11, 13):
                row = bind(packet, line, role='start', moment='before_action')
                site = next(s for s in packet['candidates'] if s['id'] == row['site_id'])
                contract = site['logging_chain']['adapter_candidates'][0]['contract_basis']
                row['source_contract'] = {'candidate_id':contract['candidate_id'], 'meaning':'Интерфейс и реализация передают текст в журнал'}
                row['emission_evidence'] = []
                bindings.append(row)
            value = reply(packet, bindings); value['missing'] = ['Нет подтверждения результата']
            return value
        model = Scripted(callback); result = review_logging(view, snap, model)
        return view, model, result

    def test_sdk_implementation_is_not_an_unrelated_completeness_obligation(self):
        view, model, result = self.java_review()
        row = result['operations'][0]
        self.assertEqual(row['state'], 'P', row['review_tasks'])
        self.assertTrue(row['search']['complete']); validate_saved_rows(view, result)
        sdk = next(s for s in model.calls[0]['packet']['candidates'] if s['callee']=='Telemetry.Records.emitInfo')
        self.assertTrue(sdk['implementation_of'])
        self.assertTrue(any(s['id']==sdk['id'] for s in result['inventory']['sites']))

    def test_unrelated_caller_candidates_are_visible_without_blocking_the_callee(self):
        path='src/com/example/orders/Caller.java'
        content=('package com.example.orders;\nclass Caller { OrderGateway gateway; Publisher publisher;\n'
                 ' void run(String id) {\n gateway.confirm(id);\n publisher.publish(id);\n }\n}\n')
        _, model, result = self.java_review({path:content})
        packet=model.calls[0]['packet']; candidate=next(s for s in packet['candidates'] if s['callee']=='publisher.publish')
        self.assertEqual(candidate['candidate_scope'],'navigation_context')
        self.assertNotIn(candidate['id'],packet['required_candidate_ids'])
        self.assertEqual(result['operations'][0]['state'],'P')
        self.assertIn(candidate['id'],{s['id'] for s in result['inventory']['sites']})

    def python_review(self, classification):
        code='def apply(item):\n    publisher.publish(item)\n'
        (self.root/'app.py').write_text(code)
        view=MapView.from_dict({'schema_version':'2.0','sources':[{'id':'source-1'}],'scenario':{},
            'nodes':[operation(code,first=1,last=2,outcome='unknown')],'edges':[],'run':{'id':'synthetic','status':'completed'}})
        def callback(p,n):
            value=model_reply(p,key='absent')
            if classification:
                value['candidate_reviews']=[{'site_id':p['candidates'][0]['id'],'classification':classification,
                    'reason':'Этот вызов относится к иной технической задаче, не к результату текущего действия',
                    'evidence':[{'path':'app.py','start_line':2,'end_line':2}]}]
            return value
        model=Scripted(callback)
        result=review_logging(view,snapshot_repository(self.root,max_files=None,max_chars=None,logging_calls=['publisher.publish']),model)
        return view,model,result

    def test_explicit_exclusion_resolves_relevance_without_creating_a_log_fact(self):
        for label in ('technical_log','other_operation_log'):
            with self.subTest(label=label):
                view,_,result=self.python_review(label);row=result['operations'][0]
                self.assertEqual(row['state'],'U');self.assertEqual(row['bindings'],[])
                validate_saved_rows(view,result)

    def test_unresolved_or_unanswered_own_candidate_stays_unknown(self):
        for label in ('unresolved',None):
            with self.subTest(label=label):
                _,_,result=self.python_review(label)
                self.assertEqual(result['operations'][0]['state'],'X')

    def test_exclusion_requires_the_real_call_quote(self):
        _,model,result=self.python_review('technical_log')
        from process_observability.logging_review import _validate_reply
        from process_observability.map_input import RepoContext
        view=MapView.from_dict(result['map']);snap=snapshot_repository(self.root,max_files=None,max_chars=None)
        context=RepoContext(snap,view);context.read('app.py',1,2)
        packet=model.calls[0]['packet'];value=deepcopy(result['checkpoints'][0]['reply'])
        value['candidate_reviews'][0]['evidence'][0]['start_line']=1
        value['candidate_reviews'][0]['evidence'][0]['end_line']=1
        from process_observability.logging_contracts import LoggingReviewError
        with self.assertRaises(LoggingReviewError):_validate_reply(value,packet,context)

    def test_external_log_inside_reachable_helper_still_blocks_absence(self):
        code=('def record(item):\n    publisher.publish(item)\ndef apply(item):\n    record(item)\n')
        (self.root/'app.py').write_text(code)
        view=MapView.from_dict({'schema_version':'2.0','sources':[{'id':'source-1'}],'scenario':{},
            'nodes':[operation(code,first=3,last=4,outcome='unknown')],'edges':[],'run':{'id':'synthetic','status':'completed'}})
        model=Scripted(lambda p,n:model_reply(p,key='absent'))
        result=review_logging(view,snapshot_repository(self.root,max_files=None,max_chars=None,logging_calls=['publisher.publish']),model)
        p=model.calls[0]['packet'];site=p['candidates'][0]
        self.assertIn(site['id'],p['required_candidate_ids'])
        self.assertEqual(result['operations'][0]['state'],'X')

    def test_missing_candidate_answer_gets_one_addressed_repair(self):
        code='def apply(item):\n    publisher.publish(item)\n'; (self.root/'app.py').write_text(code)
        view=MapView.from_dict({'schema_version':'2.0','sources':[{'id':'source-1'}],'scenario':{},
            'nodes':[operation(code,first=1,last=2,outcome='unknown')],'edges':[],'run':{'id':'synthetic','status':'completed'}})
        def callback(p,n):
            value=model_reply(p,key='absent')
            if n==2:
                value['candidate_reviews']=[{'site_id':p['candidates'][0]['id'],'classification':'technical_log',
                    'reason':'Техническая запись, не отражает результат действия',
                    'evidence':[{'path':'app.py','start_line':2,'end_line':2}]}]
            return value
        model=Scripted(callback)
        result=review_logging(view,snapshot_repository(self.root,max_files=None,max_chars=None,logging_calls=['publisher.publish']),model)
        self.assertEqual(len(model.calls),2);self.assertEqual(result['operations'][0]['state'],'U')
        issue=next(i for i in model.calls[1]['repair_details']['issues'] if i['code']=='unreviewed_candidates')
        self.assertEqual(issue['candidates'][0]['reference'],{'path':'app.py','start_line':2,'end_line':2})

    def test_new_inspect_candidate_is_not_silently_left_out_of_completeness(self):
        code='def apply(item):\n    publisher.publish(item)\n'; (self.root/'app.py').write_text(code)
        view=MapView.from_dict({'schema_version':'2.0','sources':[{'id':'source-1'}],'scenario':{},
            'nodes':[operation(code,first=1,last=2,outcome='unknown')],'edges':[],'run':{'id':'synthetic','status':'completed'}})
        def callback(p,n):
            if n==1:return {'action':'inspect','logging_calls':['publisher.publish']}
            return model_reply(p,key='absent')
        model=Scripted(callback)
        result=review_logging(view,snapshot_repository(self.root,max_files=None,max_chars=None),model)
        packet=model.calls[1]['packet'];self.assertEqual(len(packet['required_candidate_ids']),1)
        self.assertEqual(result['operations'][0]['state'],'X')

    def test_sibling_operation_anchor_does_not_become_this_operations_obligation(self):
        code='def apply(item):\n    persist(item)\n    publisher.publish(item)\n'
        (self.root/'app.py').write_text(code)
        first=operation(code,identifier='persist',first=2,last=2,outcome='unknown')
        second=operation(code,identifier='publish',first=3,last=3,outcome='unknown')
        view=MapView.from_dict({'schema_version':'2.0','sources':[{'id':'source-1'}],'scenario':{},
            'nodes':[first,second],'edges':[],'run':{'id':'synthetic','status':'completed'}})
        model=Scripted(lambda p,n:model_reply(p,key='absent'))
        result=review_logging(view,snapshot_repository(self.root,max_files=None,max_chars=None,logging_calls=['publisher.publish']),model)
        first_packet=model.calls[0]['packet'];site=first_packet['candidates'][0]
        self.assertEqual(site['anchored_operation_ids'],['publish'])
        self.assertNotIn(site['id'],first_packet['required_candidate_ids'])
        self.assertEqual([r['state'] for r in result['operations']],['U','X'])

    def test_focus_does_not_follow_a_siblings_call_but_keeps_the_global_registry(self):
        from process_observability.logging_navigation import LoggingNavigation
        code=('class Flow {\n void run() {\n  own();\n  sibling();\n }\n'
              ' void own() { logger.info("own"); }\n'
              ' void sibling() { publisher.publish("other"); }\n}\n')
        (self.root/'Flow.java').write_text(code)
        snap=snapshot_repository(self.root,max_files=None,max_chars=None)
        nav=LoggingNavigation(snap)
        wanted,_,_=nav.related([{'path':'Flow.java','start_line':2,'end_line':5}],10,focus=[{'path':'Flow.java','start_line':3,'end_line':3}])
        self.assertTrue(any(p=='Flow.java' and a==6 for p,a,b in wanted))
        self.assertFalse(any(p=='Flow.java' and a==7 for p,a,b in wanted))

if __name__=='__main__':unittest.main()
