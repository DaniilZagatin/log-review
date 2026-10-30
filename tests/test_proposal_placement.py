"""Source-text-only placement controls across unrelated Python/Java processes."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from process_observability.logging_changes import validate_changes
from process_observability.logging_contracts import ChangeValidationError
from process_observability.map_input import MapView, RepoContext
from process_observability.source_tools import snapshot_repository

PY = '''def assign(harbor, ferry):
    slot = harbor.reserve(ferry.size)
    if slot is None:
        return None
    ferry.dock(slot)
    return slot
'''
JAVA = '''class Hive {
 void collect(Apiary apiary) {
  while (apiary.open()) {
   Frame frame = apiary.nextFrame();
   if (frame == null || frame.empty()) {
    break;
   }
   frame = apiary.inspect(frame.id());
  }
 }
}
'''


class PlacementControls(unittest.TestCase):
    def context(self, code, path):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        root = Path(folder.name); (root / path).write_text(code)
        view = MapView.from_dict({'schema_version':'2.0','sources':[{'id':'s'}],'scenario':{},'nodes':[],
                                 'edges':[],'run':{'id':'synthetic-placement','status':'completed'}})
        context = RepoContext(snapshot_repository(root,max_files=None,max_chars=None), view)
        for first in range(1,len(code.splitlines())+1,200):context.read(path,first,min(first+199,len(code.splitlines())))
        return context

    def proposal(self, path, first, last=None, position='before', guards=(), column=None):
        location = {'path':path,'start_line':first,'end_line':last or first}
        if column is not None: location['column'] = column
        return {'id':'record','kind':'add_event','operation_ids':['op'],'problem':'Не видно исхода',
                'what':'Записать установленный исход','why':'Различать исходы','fields':[],
                'location':location,'placement':position,'guards':[
                    {'condition':{'path':path,'start_line':a,'end_line':b},'branch':branch}
                    for a,b,branch in guards],
                'timing':'В указанной ветке, относительно действия в источнике',
                'verification':'Нужный исход даёт запись, противоположный её не даёт'}

    def accept(self, context, proposal):
        changes,tasks,errors = validate_changes([proposal],'op',context,{})
        self.assertFalse(tasks); self.assertFalse(errors); self.assertEqual(len(changes),1)
        return changes[0]

    def reject(self, context, proposal, reason):
        with self.assertRaisesRegex(ChangeValidationError,reason):
            validate_changes([proposal],'op',context,{})

    def test_bare_anchor_is_not_an_insertion_contract(self):
        ctx=self.context(PY,'port.py'); p=self.proposal('port.py',4)
        del p['placement']; del p['guards']
        self.reject(ctx,p,'placement')

    def test_opposite_branch_rejected_but_negative_before_exit_accepted(self):
        for path,code,condition,exit_line in [('port.py',PY,3,4),('Hive.java',JAVA,5,6)]:
            with self.subTest(path=path):
                ctx=self.context(code,path)
                p=self.proposal(path,exit_line,guards=[(condition,condition,'false')])
                self.reject(ctx,p,'guard_contradiction')
                p['guards'][0]['branch']='true'
                result=self.accept(ctx,p)
                self.assertEqual(result['placement_context']['status'],'supported')
                self.assertEqual(result['guards'][0]['branch'],'true')
                self.assertIn('quote',result['guards'][0]['condition'])

    def test_after_abrupt_exit_is_unreachable_without_blacklisting_before(self):
        for code,path,line,guards in [(PY,'port.py',4,[(3,3,'true')]),(JAVA,'Hive.java',6,[(5,5,'true')])]:
            ctx=self.context(code,path)
            self.reject(ctx,self.proposal(path,line,position='after',guards=guards),'unreachable_insertion')
            self.accept(ctx,self.proposal(path,line,position='before',guards=guards))

    def test_normal_path_after_early_exit_requires_historical_false_guard(self):
        for code,path,line,cond in [(PY,'port.py',5,3),(JAVA,'Hive.java',8,5)]:
            ctx=self.context(code,path)
            self.reject(ctx,self.proposal(path,line,position='after'),'missing_guard')
            row=self.accept(ctx,self.proposal(path,line,position='after',guards=[(cond,cond,'false')]))
            self.assertFalse(row['placement_context']['semantic_verified'])

    def test_guard_not_on_the_path_and_omitted_nested_guard_are_rejected(self):
        code='def act(item):\n    if item.ready:\n        if item.valid:\n            item.commit()\n    if item.other:\n        item.skip()\n'
        ctx=self.context(code,'flow.py')
        self.reject(ctx,self.proposal('flow.py',4,guards=[(2,2,'true')]),'missing_guard')
        self.reject(ctx,self.proposal('flow.py',4,guards=[(5,5,'true')]),'guard_not_on_path')
        self.accept(ctx,self.proposal('flow.py',4,guards=[(2,2,'true'),(3,3,'true')]))

    def test_else_polarity_and_inverted_early_return(self):
        for code,path,line,cond in [
            ('def act(v):\n    if v.ready:\n        return v\n    v.reject()\n','case.py',4,2),
            ('class V {\n void act(V v) {\n  if(v.ready()) {\n   v.use();\n  } else {\n   v.reject();\n  }\n }\n}\n','V.java',6,3)]:
            ctx=self.context(code,path)
            self.accept(ctx,self.proposal(path,line,guards=[(cond,cond,'false')]))
            self.reject(ctx,self.proposal(path,line,guards=[(cond,cond,'true')]),'guard_contradiction')

    def test_renaming_messages_and_line_offsets_do_not_change_decision(self):
        for prefix in ('','\n\n# independent domain\n'):
            code=prefix+PY.replace('harbor','orchard').replace('ferry','crate').replace('slot','space')
            offset=len(prefix.splitlines());ctx=self.context(code,'orchard.py')
            self.reject(ctx,self.proposal('orchard.py',4+offset,guards=[(3+offset,3+offset,'false')]),'guard_contradiction')

    def test_same_line_statement_requires_column_and_preserves_branch(self):
        code='def act(v):\n    if v.empty: v.mark(); return None\n    v.use()\n'
        ctx=self.context(code,'compact.py');line=code.splitlines()[1]
        self.reject(ctx,self.proposal('compact.py',2,column=line.index('None')),'anchor_not_statement_boundary')
        row=self.accept(ctx,self.proposal('compact.py',2,column=line.index('return'),guards=[(2,2,'true')]))
        self.assertEqual(row['placement_context']['point']['column'],line.index('return'))
        # Without a column this denotes the whole if, not the nested return.
        self.accept(ctx,self.proposal('compact.py',2,position='after',guards=[(2,2,'false')]))

    def test_unread_guard_is_not_leaked_or_certified(self):
        ctx=self.context(PY,'port.py');ctx.shown=[];ctx.read('port.py',4,4)
        self.reject(ctx,self.proposal('port.py',4,guards=[(3,3,'true')]),'not delivered')

    def test_partial_multiline_statement_is_not_silently_extended(self):
        code='def act(v):\n    v.store(\n        123\n    )\n'
        ctx=self.context(code,'multi.py')
        self.reject(ctx,self.proposal('multi.py',2),'anchor_not_statement_boundary')
        self.accept(ctx,self.proposal('multi.py',2,4,position='after'))

    def test_unsupported_flow_is_explicit_and_does_not_invent_guards(self):
        code='def act(v):\n    try:\n        v.store()\n    finally:\n        v.close()\n'
        ctx=self.context(code,'finalize.py')
        row=self.accept(ctx,self.proposal('finalize.py',3,position='after'))
        self.assertEqual(row['placement_context']['status'],'not_established')
        self.assertFalse(row['placement_context']['semantic_verified'])
        self.assertTrue(row['placement_context']['limitations'])

    def test_invalid_proposal_isolated_without_mutating_raw(self):
        ctx=self.context(JAVA,'Hive.java');p=self.proposal('Hive.java',6,guards=[(5,5,'false')]);before=deepcopy(p)
        changes,tasks,errors=validate_changes([p],'op',ctx,{},isolate=True)
        self.assertFalse(changes);self.assertFalse(tasks);self.assertIn('guard_contradiction',errors[0]['error'])
        self.assertEqual(p,before)

    def test_unread_early_exit_cannot_prove_a_fallthrough_guard(self):
        ctx=self.context(PY,'port.py');ctx.shown=[];ctx.read('port.py',3,3);ctx.read('port.py',5,5)
        self.reject(ctx,self.proposal('port.py',5,guards=[(3,3,'false')]),'not delivered')

    def test_java_same_line_uses_exact_statement_column(self):
        code='class Pool {\n void take(Item v) {\n  if (v.empty()) { v.note(); return; } v.use();\n }\n}\n'
        ctx=self.context(code,'Pool.java');line=code.splitlines()[2]
        self.reject(ctx,self.proposal('Pool.java',3),'anchor_not_statement_boundary')
        self.reject(ctx,self.proposal('Pool.java',3,column=line.index('return'),guards=[(3,3,'false')]),'guard_contradiction')
        self.accept(ctx,self.proposal('Pool.java',3,column=line.index('v.use'),guards=[(3,3,'false')]))
        self.accept(ctx,self.proposal('Pool.java',3,column=line.index('return'),guards=[(3,3,'true')]))

    def test_multiline_java_condition_is_a_complete_read_header(self):
        code=JAVA.replace('if (frame == null || frame.empty()) {','if (frame == null\n       || frame.empty()) {')
        ctx=self.context(code,'Hive.java')
        self.accept(ctx,self.proposal('Hive.java',7,guards=[(5,6,'true')]))
        self.reject(ctx,self.proposal('Hive.java',7,guards=[(5,5,'true')]),'guard_not_on_path')

    def test_both_branches_exit_and_condition_join_without_exit(self):
        code='def act(v):\n    if v.ready:\n        return 1\n    else:\n        raise ValueError()\n    v.use()\n'
        ctx=self.context(code,'join.py')
        self.reject(ctx,self.proposal('join.py',6),'unreachable_insertion')
        code=code.replace('return 1','v.mark()').replace('raise ValueError()','v.reject()')
        ctx=self.context(code,'join.py')
        self.accept(ctx,self.proposal('join.py',6))
        self.reject(ctx,self.proposal('join.py',6,guards=[(2,2,'true')]),'guard_not_on_path')

    def test_loop_body_guards_do_not_escape_to_after_the_loop(self):
        code=JAVA.replace('  }\n }\n}', '  }\n  apiary.close();\n }\n}')
        ctx=self.context(code,'Hive.java')
        row=self.accept(ctx,self.proposal('Hive.java',10))
        self.assertEqual(row['placement_context']['path_conditions'],[])
        self.assertFalse(row['placement_context']['repetition_lines'])

    def test_unbraced_java_body_requires_braces_for_added_statement(self):
        code='class Pond {\n void fill(Valve v) {\n  if (v.closed())\n   return;\n  v.open();\n }\n}\n'
        ctx=self.context(code,'Pond.java')
        row=self.accept(ctx,self.proposal('Pond.java',4,guards=[(3,3,'true')]))
        self.assertTrue(row['placement_context']['required_edits'])
        self.assertTrue(any('{ }' in r for r in row['requirements']))

    def test_java_try_and_labelled_exit_are_not_certified(self):
        cases=[('class A {\n void act(A a) {\n  try {\n   a.put();\n  } finally {\n   a.close();\n  }\n }\n}\n',4),
               ('class A {\n void act(A a) {\n  outer: while(a.open()) {\n   break outer;\n  }\n }\n}\n',4)]
        for code,line in cases:
            ctx=self.context(code,'A.java')
            row=self.accept(ctx,self.proposal('A.java',line))
            self.assertEqual(row['placement_context']['status'],'not_established')

    def test_existing_field_declared_at_anchor_is_not_available_before_it(self):
        code='def act(item):\n    stored = item.persist()\n    return stored\n'
        ctx=self.context(code,'write.py');p=self.proposal('write.py',2)
        p['fields']=[{'name':'stored','origin':'existing','expression':'stored',
                     'evidence':[{'path':'write.py','start_line':2,'end_line':2}]}]
        self.reject(ctx,p,'existing field')
        p['placement']='after';self.accept(ctx,p)

    def test_module_logger_cannot_be_used_before_its_creation_statement(self):
        code='import logging\njournal = logging.getLogger(__name__)\n'
        ctx=self.context(code,'module.py');p=self.proposal('module.py',2)
        p['logger']={'expression':'journal','evidence':[{'path':'module.py','start_line':2,'end_line':2}]}
        self.reject(ctx,p,'logger.expression')
        p['placement']='after';self.assertEqual(self.accept(ctx,p)['logger_access']['status'],'available')

    def test_renderer_explains_path_and_escapes_source(self):
        from process_observability.logging_report import _improvements_panel
        code='def act(v):\n    if v.value < 3:\n        v.note()\n'
        ctx=self.context(code,'display.py');row=self.accept(ctx,self.proposal('display.py',3,guards=[(2,2,'true')]))
        html=_improvements_panel({'changes':[row],'operations':[]},{'op':'Действие'})
        self.assertIn('Перед указанным оператором',html);self.assertIn('истина',html)
        self.assertIn('&lt;',html);self.assertNotIn('v.value < 3',html)

    def test_empty_establishes_feedback_names_all_invalid_bindings(self):
        from process_observability.logging_review import _repair_details
        ctx=self.context(PY,'port.py')
        reply={'action':'review','bindings':[{'site_id':'one','establishes':[]},{'site_id':'two','establishes':['fiction']}],
               'changes':[]}
        issues=_repair_details(reply,{'operation':{'id':'op'},'candidates':[]},ctx)['issues']
        invalid=[i for i in issues if i['code']=='binding_establishes_invalid']
        self.assertEqual([i['field'] for i in invalid],['bindings[0].establishes','bindings[1].establishes'])
        self.assertTrue(all('НЕПУСТОЙ' in i['instruction'] for i in invalid))

    def test_failed_mandatory_probe_does_not_claim_adapter_is_not_a_logger(self):
        from unittest.mock import patch
        from process_observability.logging_review import _repair_details
        ctx=self.context(PY,'port.py');p=self.proposal('port.py',5,position='after',guards=[(3,3,'false')])
        p['logger']={'expression':'journal','evidence':[{'path':'port.py','start_line':2,'end_line':2}]}
        reply={'action':'review','changes':[p],'bindings':[{'site_id':'adapter-site','source_contract':{'candidate_id':'adapter'}}]}
        with (patch('process_observability.logging_review._validate_reply',side_effect=ValueError('mandatory invalid')),
              patch('process_observability.logger_access.logger_feedback',return_value={'reason':'logging_purpose_unresolved',
                                                                                      'in_scope_site_ids':['adapter-site']})):
            issues=_repair_details(reply,{'operation':{'id':'op'},'candidates':[]},ctx)['issues']
        pending=[i for i in issues if i['code']=='purpose_pending_binding_validation']
        self.assertEqual(len(pending),1);self.assertEqual(pending[0]['blocked_by'],'mandatory invalid')
        self.assertFalse(any(i['code']=='logger_expression_not_available' for i in issues))

    def test_unrelated_binding_failure_does_not_hide_unknown_business_receiver(self):
        from unittest.mock import patch
        from process_observability.logging_review import _repair_details
        from process_observability.log_discovery import discover_logging
        code='def send(item, bus):\n    bus.publish(item)\n    return item\n'
        ctx=self.context(code,'bus.py');p=self.proposal('bus.py',3)
        p['logger']={'expression':'bus.publish','evidence':[{'path':'bus.py','start_line':2,'end_line':2}]}
        sites=discover_logging(ctx.snapshot,call_names=['bus.publish'],max_sites=None,max_per_file=None,
                               max_hints=None,include_columns=True,exclude_declarations=True)['sites']
        reply={'action':'review','changes':[p],'bindings':[{'site_id':'unrelated','establishes':[]}]}
        with patch('process_observability.logging_review._validate_reply',side_effect=ValueError('mandatory invalid')):
            issues=_repair_details(reply,{'operation':{'id':'op'},'candidates':sites,'required_candidate_ids':[]},ctx)['issues']
        self.assertFalse(any(i['code']=='purpose_pending_binding_validation' for i in issues))
        self.assertTrue(any(i['code']=='logger_expression_not_available' and i['reason']=='logging_purpose_unresolved' for i in issues))

    def test_real_protocol_repairs_wrong_branch_without_relocating_the_raw_reply(self):
        from test_map_first_known_gaps import KnownGapControls
        from process_observability.logging_review import ReviewOptions
        helper=KnownGapControls()
        def answer(packet,number):
            result=helper.partial(packet)
            if number==1:result['changes'][0]['location'].update(start_line=7,end_line=7)
            return result
        model,result=helper.run_review(answer,options=ReviewOptions(stop_on_failed_repair=True))
        self.assertEqual(len(model.calls),2);self.assertEqual(result['operations'][0]['state'],'P')
        self.assertIn('guard_contradiction',model.calls[1]['repair_errors'][0])
        self.assertEqual(model.calls[1]['rejected_response']['reply']['changes'][0]['location']['start_line'],7)
        self.assertEqual(result['operations'][0]['changes'][0]['location']['start_line'],8)

    def test_failed_placement_repair_stops_before_any_further_request(self):
        from test_map_first_known_gaps import KnownGapControls
        from process_observability.logging_review import ReviewOptions
        helper=KnownGapControls()
        def answer(packet,number):
            result=helper.partial(packet);result['changes'][0]['location'].update(start_line=7,end_line=7)
            return result
        model,result=helper.run_review(answer,options=ReviewOptions(stop_on_failed_repair=True))
        self.assertEqual(len(model.calls),2);self.assertEqual(result['stop_reason']['kind'],'repair_failed')
        self.assertFalse(result['changes']);self.assertEqual(result['operations'][0]['state'],'X')

    def test_known_lexical_branch_is_checked_inside_unsupported_control(self):
        cases=[('def act(q, lock):\n    with lock:\n        item=q.take()\n    if item is None:\n        return None\n    item.run()\n','lock.py',5,4),
               ('class Desk {\n void drain(Queue q) {\n  try {\n   while(q.open()) {\n    Item it=q.poll();\n    if(it==null) {\n     break;\n    }\n    it.ship();\n   }\n  } catch(Exception e) { q.fail(e); }\n }\n}\n','Desk.java',7,6)]
        for code,path,line,cond in cases:
            ctx=self.context(code,path)
            self.reject(ctx,self.proposal(path,line,guards=[(cond,cond,'false')]),'guard_contradiction')
            row=self.accept(ctx,self.proposal(path,line,guards=[(cond,cond,'true')]))
            self.assertEqual(row['placement_context']['status'],'not_established')
            self.assertEqual(row['placement_context']['path_conditions'][0]['basis'],'lexical')

    def test_after_literal_infinite_loop_is_not_certified_as_unguarded(self):
        cases=[('def act(q):\n    while True:\n        item=q.take()\n        if item is None:\n            break\n        item.run()\n    q.done()\n','poll.py',7),
               ('class Desk {\n void act(Queue q) {\n  for(;;) {\n   if(q.empty()) break;\n   q.next();\n  }\n  q.done();\n }\n}\n','Desk.java',7)]
        for code,path,line in cases:
            ctx=self.context(code,path);row=self.accept(ctx,self.proposal(path,line))
            self.assertEqual(row['placement_context']['status'],'not_established')

    def test_opaque_declaration_or_expression_does_not_disable_later_branch_checks(self):
        cases=[('def act(v):\n    def later():\n        return v\n    if v.empty:\n        return None\n    v.run()\n','defn.py',5,4),
               ('class Mail {\n void act(Job v) {\n  int n=v.ready()?1:2;\n  if(v.empty()) {\n   return;\n  }\n  v.run();\n }\n}\n','Mail.java',5,4)]
        for code,path,line,cond in cases:
            ctx=self.context(code,path)
            self.reject(ctx,self.proposal(path,line,guards=[(cond,cond,'false')]),'guard_contradiction')
            self.assertEqual(self.accept(ctx,self.proposal(path,line,guards=[(cond,cond,'true')]))['placement_context']['status'],'supported')

    def test_unsupported_language_still_enforces_before_reference_order(self):
        code='fun act(v: Item) {\n val result = v.store()\n return result\n}\n'
        ctx=self.context(code,'Store.kt');p=self.proposal('Store.kt',2,3)
        p['fields']=[{'name':'result','origin':'existing','expression':'result',
                     'evidence':[{'path':'Store.kt','start_line':2,'end_line':2}]}]
        self.reject(ctx,p,'existing field')

    def test_field_from_closed_sibling_branch_is_not_available_at_point(self):
        cases=[('def act(b):\n    if b.due:\n        ref=b.reference()\n        b.pay()\n    b.close()\n','bill.py',5,3),
               ('class Bill {\n void pay(B b) {\n  if(b.due()) {\n   String ref=b.reference();\n   b.pay();\n  }\n  b.close();\n }\n}\n','Bill.java',7,4)]
        for code,path,line,evidence in cases:
            ctx=self.context(code,path);p=self.proposal(path,line)
            p['fields']=[{'name':'ref','origin':'existing','expression':'ref',
                        'evidence':[{'path':path,'start_line':evidence,'end_line':evidence}]}]
            self.reject(ctx,p,'existing field')
            # A wider quote must not turn the inner declaration into an outer-scope one.
            p['fields'][0]['evidence'][0].update(start_line=evidence-1,end_line=evidence+1)
            self.reject(ctx,p,'existing field')

    def test_callback_body_is_unestablished_not_relocated_to_outer_call(self):
        code='class Box {\n void send(Items items) {\n  items.forEach(item -> {\n   item.ship();\n  });\n }\n}\n'
        ctx=self.context(code,'Box.java');p=self.proposal('Box.java',4,position='after')
        row=self.accept(ctx,p)
        self.assertEqual(row['placement_context']['status'],'not_established')
        self.assertEqual(row['location']['start_line'],4)

    def test_missing_member_source_produces_exact_bounded_read_hint(self):
        from process_observability.logging_review import _repair_details
        ctx=self.context(PY,'port.py');ctx.shown=[];ctx.read('port.py',3,5)
        p=self.proposal('port.py',5,guards=[(3,3,'false')])
        details=_repair_details({'action':'review','bindings':[],'changes':[p]}, {'operation':{'id':'op'},'candidates':[]},ctx)
        issue=next(x for x in details['issues'] if x['code']=='invalid_proposal_placement')
        self.assertEqual(issue['reads'],[{'path':'port.py','start_line':1,'end_line':6}])

    def test_multiline_python_parenthesized_header_and_inline_suite(self):
        code='def act(v):\n    if (\n        v.ready\n        and v.open\n    ):\n        v.run()\n'
        ctx=self.context(code,'multi.py');self.accept(ctx,self.proposal('multi.py',6,guards=[(2,5,'true')]))
        code='def act(v):\n    if v.empty: v.mark(); return None\n    v.use()\n'
        ctx=self.context(code,'inline.py');line=code.splitlines()[1]
        row=self.accept(ctx,self.proposal('inline.py',2,column=line.index('return'),guards=[(2,2,'true')]))
        self.assertTrue(row['placement_context']['required_edits'])


    def field(self,p,path,expression,*ranges):
        p['fields']=[{'name':'value','origin':'existing','expression':expression,
                     'evidence':[{'path':path,'start_line':a,'end_line':b} for a,b in ranges]}]
        return p

    def test_peer_string_key_field_and_multiple_evidence_refs(self):
        code='def act(payload):\n    saved = payload["order_id"]\n    use(saved)\n    close()\n'
        ctx=self.context(code,'payload.py')
        self.accept(ctx,self.field(self.proposal('payload.py',4),'payload.py','payload["order_id"]',(1,1),(2,2)))
        self.accept(ctx,self.field(self.proposal('payload.py',4),'payload.py','saved',(1,1),(2,2),(3,3)))
        code='class Bill {\n void settle(Map data) {\n  String id = data.get("id");\n  data.close();\n }\n}\n'
        ctx=self.context(code,'Bill.java')
        self.accept(ctx,self.field(self.proposal('Bill.java',4),'Bill.java','data.get("id")',(2,2),(3,3)))

    def test_peer_parameter_and_outer_binding_usage_inside_branch(self):
        for code,path,line,proof,expr in [
            ('def act(bill):\n    if bill.due:\n        notify(bill.id)\n    bill.close()\n','bill.py',4,3,'bill.id'),
            ('def act(bill):\n    ref=bill.id\n    if bill.due:\n        notify(ref)\n    bill.close()\n','outer.py',5,4,'ref'),
            ('class Bill {\n void settle(Bill bill) {\n  if(bill.due()) {\n   audit(bill.getId());\n  }\n  bill.close();\n }\n}\n','Bill.java',6,4,'bill.getId()')]:
            ctx=self.context(code,path);self.accept(ctx,self.field(self.proposal(path,line),path,expr,(proof,proof)))

    def test_peer_header_or_nested_expression_names_do_not_escape(self):
        cases=[('class Port {\n void close(Boxes boxes) {\n  for(Box b : boxes) { b.seal(); }\n  boxes.close();\n }\n}\n','Port.java',4,3,'b'),
               ('def act(items):\n    values=[b.id for b in items]\n    close()\n','comp.py',3,2,'b'),
               ('def act(items):\n    fn=lambda b: b.id\n    close()\n','lam.py',3,2,'b'),
               ('def act(item):\n    try:\n        item.save()\n    except Exception as exc:\n        notify(exc)\n    item.close()\n','except.py',6,5,'exc')]
        for code,path,line,proof,expr in cases:
            ctx=self.context(code,path);self.reject(ctx,self.field(self.proposal(path,line),path,expr,(proof,proof)),'existing field')

    def test_peer_module_logger_use_in_branch_remains_available(self):
        from process_observability.log_discovery import discover_logging
        code='import logging\naudit=logging.getLogger(__name__)\ndef act(item):\n    if item.due:\n        audit.info("due")\n    item.close()\n'
        ctx=self.context(code,'audit.py');p=self.proposal('audit.py',6)
        p['logger']={'expression':'audit','evidence':[{'path':'audit.py','start_line':5,'end_line':5}]}
        sites=discover_logging(ctx.snapshot,max_sites=None,max_per_file=None,max_hints=None,include_columns=True,exclude_declarations=True)['sites']
        rows,_,_=validate_changes([p],'op',ctx,{s['id']:s for s in sites})
        self.assertEqual(rows[0]['logger_access']['status'],'available')

    def test_peer_local_type_does_not_swallow_following_guard(self):
        for declaration in ('enum Mode { FAST }','final class Local {}','record Key(int n) {}'):
            code='class Port {\n void dock(Ship s) {\n  '+declaration+'\n  if(s==null) return;\n  s.berth();\n }\n}\n'
            ctx=self.context(code,'Port.java');self.reject(ctx,self.proposal('Port.java',5),'missing_guard')
            self.accept(ctx,self.proposal('Port.java',5,guards=[(4,4,'false')]))

    def test_peer_control_continuations_do_not_swallow_else_or_next_loop(self):
        code='class Lock {\n void act(Lock l) {\n  if(l.ready()) synchronized(l) { l.use(); }\n  else {\n   l.alarm();\n  }\n }\n}\n'
        ctx=self.context(code,'Lock.java')
        self.accept(ctx,self.proposal('Lock.java',5,guards=[(3,3,'false')]))
        self.reject(ctx,self.proposal('Lock.java',5,guards=[(3,3,'true')]),'guard_contradiction')
        code='class Port {\n void act(Queue q) {\n  try { q.open(); } catch(Exception e) { q.fail(); }\n  while(q.hasNext()) {\n   q.next();\n  }\n }\n}\n'
        ctx=self.context(code,'Port.java');self.accept(ctx,self.proposal('Port.java',4,6))
        self.assertEqual(self.accept(ctx,self.proposal('Port.java',5))['placement_context']['repetition_lines'],[4])

    def test_peer_immediate_exit_is_rejected_even_inside_try(self):
        for code,path,line in [
            ('def act(v):\n    try:\n        return v\n    finally:\n        close()\n','try.py',3),
            ('class Port {\n void act(Q q) {\n  try {\n   return;\n  } finally { q.close(); }\n }\n}\n','Port.java',4)]:
            ctx=self.context(code,path);self.reject(ctx,self.proposal(path,line,position='after'),'unreachable_insertion')
            self.accept(ctx,self.proposal(path,line))

    def test_peer_callback_keeps_enclosing_lexical_guard(self):
        code='class Port {\n void act(Q q) {\n  if(!q.ready()) {\n   q.submit(() -> {\n    q.mark();\n   });\n   return;\n  }\n }\n}\n'
        ctx=self.context(code,'Port.java')
        self.reject(ctx,self.proposal('Port.java',5,guards=[(3,3,'false')]),'guard_contradiction')
        row=self.accept(ctx,self.proposal('Port.java',5,guards=[(3,3,'true')]))
        self.assertEqual(row['placement_context']['status'],'not_established')

    def test_peer_tree_cache_and_large_python_headers(self):
        from process_observability.proposal_placement import _tree
        code='def act(v):\n'+''.join('    if v.ready:\n        v.next()\n' for _ in range(1000))+'    v.close()\n'
        ctx=self.context(code,'large.py');loc={'path':'large.py','start_line':2002,'end_line':2002}
        first=_tree(ctx,loc);second=_tree(ctx,dict(loc,start_line=2000,end_line=2000))
        self.assertIs(first,second)

    def test_switch_label_does_not_inherit_exit_from_previous_case(self):
        code='class Bus {\n void act(int mode) {\n  switch(mode) {\n   case 1: use();\n    break;\n   case 2: other();\n    break;\n  }\n }\n}\n'
        ctx=self.context(code,'Bus.java')
        row=self.accept(ctx,self.proposal('Bus.java',6))
        self.assertEqual(row['placement_context']['status'],'not_established')
        self.reject(ctx,self.proposal('Bus.java',5,position='after'),'unreachable_insertion')

    def test_pattern_local_does_not_become_an_outer_scope_field(self):
        code='class Port {\n void act(Object value) {\n  if(value instanceof Box b) { b.seal(); }\n  close();\n }\n}\n'
        ctx=self.context(code,'Port.java')
        self.reject(ctx,self.field(self.proposal('Port.java',4),'Port.java','b',(3,3)),'existing field')

    def test_string_literal_or_comment_cannot_supply_existing_expression(self):
        code='def act(payload):\n    note = \'payload["id"]\' # payload["id"]\n    close()\n'
        ctx=self.context(code,'literal.py')
        self.reject(ctx,self.field(self.proposal('literal.py',3),'literal.py','payload["id"]',(2,2)),'existing field')

    def test_final_peer_with_and_guarded_try_bindings_remain_available(self):
        for code,line,proof in [
            ('def load(path):\n    with open(path) as fh:\n        data=fh.read()\n    store(data)\n',4,3),
            ('def load(api):\n    try:\n        data=api.read()\n    except Exception:\n        return None\n    store(data)\n',6,3)]:
            ctx=self.context(code,'load.py');self.accept(ctx,self.field(self.proposal('load.py',line),'load.py','data',(proof,proof)))
        code='def load(path):\n    with open(path) as fh:\n        if fh.ready:\n            data=fh.read()\n    store(data)\n'
        ctx=self.context(code,'nested.py');self.reject(ctx,self.field(self.proposal('nested.py',5),'nested.py','data',(4,4)),'existing field')

    def test_final_peer_java_nonlocal_expressions_are_not_rejected_as_locals(self):
        for expression in ('MDC.get("traceId")','String.valueOf(obj.getId())','getRequestId()','(long) x'):
            code='class Port {\n void act(Object obj, int x) {\n  inspect('+expression+');\n  close();\n }\n}\n'
            ctx=self.context(code,'Port.java');self.accept(ctx,self.field(self.proposal('Port.java',4),'Port.java',expression,(3,3)))

    def test_final_peer_callback_boundary_does_not_hide_following_statement(self):
        code='class Port {\n void act(Q q) {\n  q.submit(() -> {\n   q.mark();\n  }); q.close();\n }\n}\n'
        ctx=self.context(code,'Port.java');line=code.splitlines()[4]
        row=self.accept(ctx,self.proposal('Port.java',5,column=line.index('q.close')))
        self.assertEqual(row['placement_context']['status'],'supported')
        self.reject(ctx,self.proposal('Port.java',3),'anchor_not_statement_boundary')

    def test_closure_try_assignment_cannot_supply_handler_or_finally_field(self):
        for code,line in [
            ('def load(api):\n    try:\n        data=api.read()\n    except Exception:\n        notify()\n        return None\n    store(data)\n',5),
            ('def load(api):\n    try:\n        data=api.read()\n    finally:\n        notify()\n    store(data)\n',5)]:
            ctx=self.context(code,'handler.py');self.reject(ctx,self.field(self.proposal('handler.py',line),'handler.py','data',(3,3)),'existing field')

    def test_closure_java_second_declarator_cannot_escape_branch(self):
        code='class Bill {\n void settle(Bill b) {\n  if(b.due()) {\n   String ref=b.ref(), code=b.code();\n   b.pay();\n  }\n  b.close();\n }\n}\n'
        for declaration in ('String ref=b.ref(), code=b.code();','int ref=1, code[] = {1,2};'):
            variant=code.replace('String ref=b.ref(), code=b.code();',declaration)
            ctx=self.context(variant,'Bill.java');self.reject(ctx,self.field(self.proposal('Bill.java',7),'Bill.java','code',(4,4)),'existing field')


if __name__=='__main__': unittest.main()
