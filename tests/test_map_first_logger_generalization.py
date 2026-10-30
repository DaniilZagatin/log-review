"""Logger feedback over renamed Python/Java sources; no analysed source is executed."""
from pathlib import Path
from copy import deepcopy
import tempfile, unittest

from process_observability.logging_review import review_logging, ReviewOptions, _Navigator, _repair_details
from process_observability.map_input import MapView, RepoContext
from process_observability.log_discovery import discover_logging
from process_observability.source_tools import snapshot_repository
from process_observability.operation_coverage import validate_saved_rows
from process_observability.logging_changes import _logger_access
from process_observability.logging_contracts import LoggingReviewError
from test_map_first_logging import Scripted


def python_case(receiver, level, old_message, function):
    code = ('import logging\n' + receiver + ' = logging.getLogger(__name__)\n'
            + 'def ' + function + '(item):\n'
            + '    ' + receiver + '.' + level + '(' + repr(old_message) + ')\n'
            + '    item.apply()\n    return item\n')
    return {'path':'flow.py','code':code,'first':3,'last':6,'emit':4,'target':6,
            'expression':receiver + '.' + level}


def java_case():
    code = ('import org.slf4j.Logger;\nclass Dispatcher {\n    private Logger logger;\n'
            '    void dispatch() {\n        logger.info("queued");\n'
            '        repository.send();\n    }\n}\n')
    return {'path':'Dispatch.java','code':code,'first':4,'last':7,'emit':5,'target':6,
            'expression':'logger.info'}


class LoggerGeneralizationControls(unittest.TestCase):
    def inputs(self, root, case):
        (root/case['path']).write_text(case['code'])
        op = {'id':'action','kind':'operation','name':'Действие','description':'Выполнить изменение',
              'input':'Объект','output':'Изменённый объект','effect_kind':'result','outcome':'success',
              'evidence':[{'path':'source-1/'+case['path'],'line_start':case['first'],'line_end':case['last'],
                           'quote':'\n'.join(case['code'].splitlines()[case['first']-1:case['last']])}]}
        view=MapView.from_dict({'schema_version':'2.0','scenario':{},'sources':[{'id':'source-1'}],
                               'nodes':[op],'edges':[],'run':{'id':'synthetic','status':'completed'}})
        return view,snapshot_repository(root,max_files=None,max_chars=None)

    def reply(self,p,case,expression):
        site=next(s for s in p['candidates'] if s['path']==case['path'] and s['start_line']==case['emit'])
        ref={'path':case['path'],'start_line':case['emit'],'end_line':case['emit']}
        return {'action':'review','operation_id':'action','bindings':[
            {'site_id':site['id'],'role':'start','moment':'before_action','meaning':'Запись начала, не результата',
             'conditions':'Перед изменением','correlation':'По месту вызова','establishes':['occurrence'],
             'invocation_ref':None,'covers_outcomes':[]}],
            'missing':['Не записан результат изменения'],'review_tasks':[],'limitations':[],
            'changes':[{'id':'result','kind':'add_event','operation_ids':['action'],
                        'problem':'Нет результата','what':'Записать результат после успешного изменения',
                        'why':'Отличать выполненное изменение от начала','fields':[],
                        'location':{'path':case['path'],'start_line':case['target'],'end_line':case['target']},
                        'placement':'before' if case['path'].endswith('.py') else 'after','guards':[],
                        'timing':'После изменения','verification':'Есть запись при успехе, отсутствует при ошибке',
                        'existing_site_ids':[site['id']],'dependencies':[],
                        'logger':{'expression':expression,'evidence':[ref]}}]}

    def cases(self):
        return [python_case('clinic_audit','info','appointment.requested','reserve'),
                python_case('media_trace','warning','upload pending','upload'),
                python_case('catalog_events','debug','Архивация начата','archive'), java_case()]

    def test_single_repair_names_existing_expression_across_domains_and_languages(self):
        for case in self.cases():
            with self.subTest(path=case['path'],expression=case['expression']), tempfile.TemporaryDirectory() as folder:
                view,snap=self.inputs(Path(folder),case)
                bad=case['expression']+'("new result")'
                model=Scripted(lambda p,n:self.reply(p,case,bad if n==1 else case['expression']))
                result=review_logging(view,snap,model,options=ReviewOptions(stop_on_failed_repair=True))
                issues=[x for x in model.calls[1]['repair_details']['issues'] if x['code']=='logger_expression_not_available']
                self.assertEqual(len(issues),1)
                self.assertEqual(issues[0]['field'],'changes[0].logger.expression')
                self.assertIn(case['expression'],issues[0]['expressions_seen_in_evidence'])
                self.assertEqual(result['operations'][0]['state'],'P')
                self.assertEqual(result['operations'][0]['changes'][0]['logger_access']['expression'],case['expression'])
                self.assertEqual(len(model.calls),2);validate_saved_rows(view,result)

    def test_renaming_and_different_event_text_do_not_change_coverage(self):
        for index,case in enumerate(self.cases()):
            with self.subTest(index=index),tempfile.TemporaryDirectory() as folder:
                view,snap=self.inputs(Path(folder),case)
                model=Scripted(lambda p,n:self.reply(p,case,case['expression']))
                result=review_logging(view,snap,model)
                self.assertEqual((len(model.calls),result['operations'][0]['state']),(1,'P'))
                self.assertEqual(result['summary']['counts']['C'],0)

    def test_wrong_logger_after_repair_stops_without_isolation_or_new_call(self):
        case=self.cases()[0]
        with tempfile.TemporaryDirectory() as folder:
            view,snap=self.inputs(Path(folder),case)
            model=Scripted(lambda p,n:self.reply(p,case,case['expression']+'("not in source")'))
            result=review_logging(view,snap,model,options=ReviewOptions(stop_on_failed_repair=True))
            self.assertEqual(len(model.calls),2);self.assertEqual(result['stop_reason']['kind'],'repair_failed')
            self.assertEqual(result['operations'][0]['state'],'X')

    def test_scope_and_expression_must_have_the_same_witness(self):
        case=self.cases()[0]
        case['code']=('import logging\ndef foreign():\n    remote=logging.getLogger("outside")\n'
                      '    remote.info("foreign")\ndef local(item):\n    local=logging.getLogger("inside")\n'
                      '    local.info("begin")\n    return item\n')
        case.update(first=5,last=8,emit=7,target=8,expression='local.info')
        with tempfile.TemporaryDirectory() as folder:
            view,snap=self.inputs(Path(folder),case);ctx=RepoContext(snap,view)
            ctx.read('flow.py',1,8)
            # Foreign expression is visible somewhere; another unrelated quote is locally available.
            item={'logger':{'expression':'remote.info','evidence':[
                {'path':'flow.py','start_line':4,'end_line':4},
                {'path':'flow.py','start_line':7,'end_line':7}]}}
            row={'location':ctx.verify_ref({'path':'flow.py','start_line':8,'end_line':8}),'existing_site_ids':[]}
            with self.assertRaises(LoggingReviewError):_logger_access(item,row,ctx,{},'action')

    def test_unread_evidence_never_leaks_a_logger_hint(self):
        case=self.cases()[0]
        with tempfile.TemporaryDirectory() as folder:
            view,snap=self.inputs(Path(folder),case);ctx=RepoContext(snap,view);ctx.logging_contracts=[]
            inv=discover_logging(snap,max_sites=None,max_per_file=None,max_hints=None,include_columns=True,exclude_declarations=True)
            packet=_Navigator(ctx,inv,ReviewOptions()).initial(view.operations[0])
            raw=self.reply(packet,case,case['expression']+'("new")')
            ctx.shown=[];details=_repair_details(raw,packet,ctx)
            self.assertTrue(any(x['code']=='invalid_source_reference' for x in details['issues']))
            self.assertFalse(any(x.get('expressions_seen_in_evidence') for x in details['issues']))

if __name__=='__main__':unittest.main()
