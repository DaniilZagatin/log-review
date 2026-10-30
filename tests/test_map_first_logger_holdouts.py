"""Independent reviewer counterexamples: different structures, not just renamed sample calls."""
from pathlib import Path
import tempfile, unittest
from copy import deepcopy
from process_observability.map_input import MapView,RepoContext
from process_observability.source_tools import snapshot_repository
from process_observability.logging_changes import _logger_access
from process_observability.logging_contracts import LoggingReviewError
from process_observability.logging_review import _Navigator,_repair_details,ReviewOptions
from process_observability.log_discovery import discover_logging

TARIFF='import logging\ntariff_audit_log=logging.getLogger(__name__)\ndef recalculate(plan):\n    tariff_audit_log.info("tariff.recalc.started")\n    plan.reprice()\n    return plan\n'
GREENHOUSE='import logging\ndef irrigate(zone):\n    def notify():\n        valve_log=logging.getLogger("valves")\n        valve_log.info("valve.ping")\n    notify()\n    zone.open_valve()\n    return zone\n'
DESK='import org.slf4j.Logger;\nimport org.slf4j.LoggerFactory;\nclass VaccinationDesk {\n    private static final Logger SHOT_LOG=LoggerFactory.getLogger(VaccinationDesk.class);\n    void administer(Dose dose) {\n        dose.inject();\n    }\n}\n'
KENNEL='import logging\nkennel_journal=logging.getLogger(__name__)\ndef admit(pet):\n    kennel_journal.debug("boarding.checkin.begin")\n    boarding_bus.publish(pet)\n    pet.assign_run()\n    return pet\n'

class LoggerHoldouts(unittest.TestCase):
    def context(self,code,path='sample.py',calls=()):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);root=Path(tmp.name);(root/path).write_text(code)
        view=MapView.from_dict({'schema_version':'2.0','sources':[{'id':'source-1'}],'scenario':{},'nodes':[], 'edges':[], 'run':{'id':'synthetic','status':'completed'}})
        ctx=RepoContext(snapshot_repository(root,max_files=None,max_chars=None,logging_calls=calls),view);ctx.logging_contracts=[];ctx.read(path,1,len(code.splitlines()))
        inv=discover_logging(ctx.snapshot,call_names=calls,max_sites=None,max_per_file=None,max_hints=None,include_columns=True,exclude_declarations=True)
        return ctx,{s['id']:s for s in inv['sites']}

    def access(self,ctx,sites,expression,lines,location,path='sample.py'):
        item={'logger':{'expression':expression,'evidence':[{'path':path,'start_line':a,'end_line':b} for a,b in lines]}}
        row={'location':ctx.verify_ref({'path':path,'start_line':location,'end_line':location}),'existing_site_ids':[]}
        return _logger_access(item,row,ctx,sites,'a')

    def test_identifier_boundary_literal_and_call_syntax(self):
        ctx,sites=self.context(TARIFF)
        for bad in ('audit_log.info','tariff.recalc.started','tariff_audit_log.info("tariff.recalc.started")'):
            with self.subTest(bad=bad),self.assertRaises(LoggingReviewError):self.access(ctx,sites,bad,[(4,4)],6)
        for good in ('tariff_audit_log.info','tariff_audit_log'):
            self.assertEqual(self.access(ctx,sites,good,[(4,4)],6)['status'],'available')

    def test_nested_local_does_not_escape_and_moving_it_outward_is_valid(self):
        ctx,sites=self.context(GREENHOUSE)
        with self.assertRaises(LoggingReviewError):self.access(ctx,sites,'valve_log.info',[(5,5)],8)
        code=GREENHOUSE.replace('    def notify():\n','').replace('        valve_log','    valve_log').replace('    notify()\n','')
        ctx,sites=self.context(code)
        self.assertEqual(self.access(ctx,sites,'valve_log.info',[(4,4)],6)['status'],'available')

    def test_python_module_logger_declaration_is_available(self):
        ctx,sites=self.context(TARIFF)
        result=self.access(ctx,sites,'tariff_audit_log',[(2,2)],6)
        self.assertEqual(result['status'],'available');self.assertEqual(result['basis'],'python_module_logger')
        self.assertEqual(self.access(ctx,sites,'tariff_audit_log.info',[(2,2)],6)['status'],'available')

    def test_java_field_explicit_and_omitted_logger_are_both_available(self):
        ctx,sites=self.context(DESK,'VaccinationDesk.java')
        explicit=self.access(ctx,sites,'SHOT_LOG',[(4,4)],6,'VaccinationDesk.java')
        self.assertEqual(explicit['status'],'available');self.assertEqual(explicit['basis'],'java_standard_field')
        self.assertEqual(self.access(ctx,sites,'SHOT_LOG.info',[(4,4)],6,'VaccinationDesk.java')['status'],'available')
        row={'location':ctx.verify_ref({'path':'VaccinationDesk.java','start_line':6,'end_line':6}),'existing_site_ids':[]}
        self.assertEqual(_logger_access({},row,ctx,sites,'a')['status'],'available')

    def test_java_instance_field_static_context_and_other_class_reject(self):
        for code in (DESK.replace('static final Logger','final Logger').replace('void administer','static void administer'),
                     DESK+'class Other {\n    void visit(Dose dose) {\n        dose.inject();\n    }\n}\n'):
            with self.subTest(code=code):
                ctx,sites=self.context(code,'VaccinationDesk.java');line=11 if 'class Other' in code else 6
                with self.assertRaises(LoggingReviewError):self.access(ctx,sites,'SHOT_LOG',[(4,4)],line,'VaccinationDesk.java')

    def test_business_call_is_not_an_available_logger(self):
        ctx,sites=self.context(KENNEL,calls=['boarding_bus.publish'])
        with self.assertRaises(LoggingReviewError):self.access(ctx,sites,'boarding_bus.publish',[(5,5)],7)

    def test_java_block_local_does_not_escape(self):
        code='import org.slf4j.Logger;\nclass Orchard {\n void prune(Tree tree) {\n  if (tree.ready()) {\n   Logger branchLog=null;\n   branchLog.info("branch");\n  }\n  tree.trim();\n }\n}\n'
        ctx,sites=self.context(code,'Orchard.java',calls=['branchLog.info'])
        with self.assertRaises(LoggingReviewError):self.access(ctx,sites,'branchLog.info',[(6,6)],8,'Orchard.java')

    def test_literal_or_comment_cannot_supply_a_logger_name(self):
        code='import logging\nnoise=logging.getLogger(__name__)\ndef act(item):\n    text="unknown_audit.info" # phantom_journal.info\n    noise.info(text)\n    return item\n'
        ctx,sites=self.context(code)
        for expr in ('unknown_audit.info','phantom_journal.info'):
            with self.subTest(expr=expr),self.assertRaises(LoggingReviewError):self.access(ctx,sites,expr,[(4,4)],6)

    def test_unread_standard_field_is_not_reported_absent_or_available(self):
        ctx,sites=self.context(DESK,'VaccinationDesk.java');ctx.shown=[];ctx.read('VaccinationDesk.java',5,7)
        row={'location':ctx.verify_ref({'path':'VaccinationDesk.java','start_line':6,'end_line':6}),'existing_site_ids':[]}
        result=_logger_access({},row,ctx,sites,'a')
        self.assertEqual(result['status'],'requires_dependency');self.assertEqual(result['basis'],'source_not_delivered')
        ctx.read('VaccinationDesk.java',4,4)
        self.assertEqual(_logger_access({},row,ctx,sites,'a')['status'],'available')

    def test_module_logger_cannot_override_local_or_closure_binding(self):
        cases=[TARIFF.replace('def recalculate(plan):','def recalculate(*tariff_audit_log):'),
               'import logging\ntariff_audit_log=logging.getLogger(__name__)\ndef outer():\n    tariff_audit_log=object()\n    def inner():\n        return 1\n    return inner\n',
               'import logging\ntariff_audit_log=logging.getLogger(__name__)\ndef change():\n    global tariff_audit_log\n    tariff_audit_log=object()\n    return 1\n']
        for code,line in ((cases[0],6),(cases[1],6),(cases[2],6)):
            with self.subTest(code=code):
                ctx,sites=self.context(code)
                with self.assertRaises(LoggingReviewError):self.access(ctx,sites,'tariff_audit_log',[(2,2)],line)

    def test_factory_like_business_name_is_not_a_standard_module_logger(self):
        code='import logging\nlogging=business_factory\npseudo=logging.getLogger("catalog")\ndef act(item):\n    return item\n'
        ctx,sites=self.context(code)
        with self.assertRaises(LoggingReviewError):self.access(ctx,sites,'pseudo',[(3,3)],5)

    def test_wide_quote_cannot_hide_nested_logger_scope(self):
        from process_observability.logger_access import logger_feedback
        ctx,sites=self.context(GREENHOUSE)
        for first,last in ((5,6),(2,8)):
            with self.subTest(span=(first,last)),self.assertRaises(LoggingReviewError):
                self.access(ctx,sites,'valve_log.info',[(first,last)],8)
            feedback=logger_feedback({'expression':'valve_log.info','evidence':[{'path':'sample.py','start_line':first,'end_line':last}]},
                {'path':'sample.py','start_line':8,'end_line':8},ctx,sites)
            self.assertNotIn('valve_log.info',feedback['expressions_seen_in_evidence'])

    def test_java_blank_comment_locations_preserve_field_and_record_access(self):
        for gap in ('','        // completed'):
            for emit in ('','        SHOT_LOG.info("begin");\n'):
                code=DESK.replace('        dose.inject();\n',emit+'        dose.inject();\n'+gap+'\n')
                line=7+(1 if emit else 0)
                ctx,sites=self.context(code,'VaccinationDesk.java')
                with self.subTest(gap=gap,emit=emit):
                    self.assertEqual(self.access(ctx,sites,'SHOT_LOG.info',[(4,4)],line,'VaccinationDesk.java')['status'],'available')
                    row={'location':ctx.verify_ref({'path':'VaccinationDesk.java','start_line':line,'end_line':line}),'existing_site_ids':[]}
                    self.assertEqual(_logger_access({},row,ctx,sites,'a')['status'],'available')

    def test_java_blank_after_block_does_not_extend_local_scope(self):
        code='import org.slf4j.Logger;\nclass Grove {\n void prune(Tree tree) {\n  if (tree.ready()) {\n   Logger branchLog=null;\n   branchLog.info("branch");\n  }\n\n  tree.trim();\n }\n}\n'
        ctx,sites=self.context(code,'Grove.java',calls=['branchLog.info'])
        with self.assertRaises(LoggingReviewError):self.access(ctx,sites,'branchLog.info',[(6,6)],8,'Grove.java')

    def test_unknown_access_does_not_claim_existing_logger_absent(self):
        code='import logging\nclass Workshop:\n    def __init__(self):\n        self.audit=logging.getLogger(__name__)\n    def begin(self):\n        self.audit.info("begin")\n    def finish(self,item):\n        return item\n'
        ctx,sites=self.context(code)
        row={'location':ctx.verify_ref({'path':'sample.py','start_line':8,'end_line':8}), 'existing_site_ids':list(sites)}
        result=_logger_access({},row,ctx,sites,'a')
        self.assertEqual(result['basis'],'not_established');self.assertNotIn('требует внедрить',result['note'])
        self.assertTrue(result['site_ids'])

    def test_python_symbol_bindings_shadow_module_logger(self):
        bodies=['    import other as tariff_audit_log\n    return 1',
                '    try: pass\n    except Exception as tariff_audit_log: pass\n    return 1',
                '    def tariff_audit_log(): pass\n    return 1',
                '    class tariff_audit_log: pass\n    return 1',
                '    match item:\n        case {"audit": tariff_audit_log}: pass\n    return 1']
        for body in bodies:
            code='import logging\ntariff_audit_log=logging.getLogger(__name__)\ndef act(item):\n'+body+'\n'
            ctx,sites=self.context(code)
            with self.subTest(body=body),self.assertRaises(LoggingReviewError):
                self.access(ctx,sites,'tariff_audit_log',[(2,2)],len(code.splitlines()))

    def test_odd_line_separator_and_recursive_parse_fail_closed(self):
        from unittest.mock import patch
        from process_observability.logger_access import _code,declared_loggers
        for char in ('\f','\v','\u2028'):
            ctx,sites=self.context(TARIFF.replace('tariff.recalc.started','tariff'+char+'recalc'))
            self.assertIsNone(_code(ctx,'sample.py'))
        ctx,sites=self.context(TARIFF)
        with patch('process_observability.logger_access.ast.parse',side_effect=RecursionError):
            self.assertEqual(declared_loggers(ctx,{'path':'sample.py','start_line':6,'end_line':6}),[])

    def test_source_interpreted_custom_adapter_remains_usable_for_proposal(self):
        from unittest.mock import patch
        from test_map_first_takeover import TakeoverControls
        from test_map_first_wrapper_review import reply,GATEWAY
        helper=TakeoverControls();helper.setUp();self.addCleanup(helper.doCleanups)
        def propose(packet,bindings):
            raw=reply(packet,bindings)
            raw['changes']=[{'id':'clarify','kind':'enrich','operation_ids':[packet['operation']['id']],
                'problem':'Уточнить результат','what':'Уточнить данные записи','why':'Связать запись с результатом',
                'fields':[],'location':{'path':GATEWAY,'start_line':14,'end_line':14},'timing':'После подтверждения',
                'verification':'Проверить данные записи','existing_site_ids':[bindings[1]['site_id']],
                'logger':{'expression':'journal.record','evidence':[{'path':GATEWAY,'start_line':13,'end_line':13}]}}]
            return raw
        with patch('test_map_first_takeover.reply',side_effect=propose):_,result=helper.run_java()
        self.assertEqual(result['operations'][0]['state'],'C')
        access=result['operations'][0]['changes'][0]['logger_access']
        self.assertEqual(access['status'],'available');self.assertEqual(access['purpose_basis'],'model_interpretation_of_source')

    def test_repair_probe_value_error_does_not_abort_feedback(self):
        from unittest.mock import patch
        from test_map_first_logger_generalization import LoggerGeneralizationControls
        helper=LoggerGeneralizationControls();case=helper.cases()[0]
        with tempfile.TemporaryDirectory() as folder:
            view,snap=helper.inputs(Path(folder),case);ctx=RepoContext(snap,view);ctx.logging_contracts=[]
            inv=discover_logging(snap,max_sites=None,max_per_file=None,max_hints=None,include_columns=True,exclude_declarations=True)
            packet=_Navigator(ctx,inv,ReviewOptions()).initial(view.operations[0])
            raw=helper.reply(packet,case,case['expression']+'("new")')
            with patch('process_observability.logging_review._validate_reply',side_effect=ValueError('invalid probe')):
                feedback=_repair_details(raw,packet,ctx)
            self.assertTrue(any(x['code']=='logger_expression_not_available' for x in feedback['issues']))

    def test_java_header_and_closing_brace_locations_allow_field_logger(self):
        ctx,sites=self.context(DESK,'VaccinationDesk.java')
        for line in (5,7):
            with self.subTest(line=line):self.assertEqual(self.access(ctx,sites,'SHOT_LOG.info',[(4,4)],line,'VaccinationDesk.java')['status'],'available')

    def test_odd_lines_cannot_prove_omitted_module_logger(self):
        from process_observability.logger_access import declared_loggers
        code='import logging\n\f\ntariff_audit_log=logging.getLogger(__name__)\ndef act(item):\n    return item\n'
        ctx,sites=self.context(code)
        location={'path':'sample.py','start_line':6,'end_line':6}
        self.assertEqual(declared_loggers(ctx,location),[])
        self.assertEqual(_logger_access({}, {'location':location,'existing_site_ids':[]},ctx,sites,'a')['basis'],'not_established')

    def test_module_and_global_rebindings_do_not_prove_standard_logger(self):
        additions=['if __debug__:\n    tariff_audit_log=object()',
            'try:\n    tariff_audit_log=object()\nexcept Exception: pass',
            'import other as tariff_audit_log','def tariff_audit_log(): pass','class tariff_audit_log: pass',
            'for tariff_audit_log in []: pass','with source() as tariff_audit_log: pass','tariff_audit_log += other',
            'def reset():\n    global tariff_audit_log\n    tariff_audit_log=object()',
            'def reset():\n    global logging\n    logging=object()',
            'from other import *']
        for extra in additions:
            code=TARIFF+'\n'+extra+'\n'
            ctx,sites=self.context(code)
            with self.subTest(extra=extra),self.assertRaises(LoggingReviewError):self.access(ctx,sites,'tariff_audit_log',[(2,2)],6)

if __name__=='__main__':unittest.main()
