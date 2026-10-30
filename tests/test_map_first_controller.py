"""Report controllers run with inert DOM stubs, never analysed HTML/application code.

Migrated (map-first quality corrections, Q3): the former consumer-only graph controller
(``logging_report.JS``: projection/rank/CSS zoom) was removed in favour of the first agent's
renderer. Each former check now targets the code that owns the behaviour:
- tab switching and table-to-node selection: the report shell (``SHELL_JS``) with a stub map API;
- child-based coverage filter and counts: the renderer overlay helpers;
- block expansion, coalesced parallel transitions with every original kept, global detail and a
  visible self-loop: the renderer's own projection/layout functions;
- the 50.0% headline: the server-side header of the report.
These are unit checks with inert stubs; visual behaviour still needs a browser check.
"""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from process_observability.logging_report import SHELL_JS, build_html
from process_observability.logging_review import review_logging
from process_observability.map_input import MapView
from process_observability.map_presentation.scenario_map import _GRAPH_LAYOUT_JS, _PRESENTATION_SCRIPT, _STAGE_PROJECTION_JS
from process_observability.source_tools import snapshot_repository
from test_map_first_logging import CODE, operation, Scripted, model_reply

SHELL_HARNESS = r"""'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Element{
 constructor(tag,attributes={}){this.tagName=tag;this.attributes={...attributes};this.dataset={};this.hidden=false;this.tabIndex=0;this.listeners={};this.focused=false;}
 setAttribute(key,value){this.attributes[key]=String(value);}
 getAttribute(key){return Object.prototype.hasOwnProperty.call(this.attributes,key)?this.attributes[key]:null;}
 removeAttribute(key){delete this.attributes[key];}
 addEventListener(type,listener){(this.listeners[type]=this.listeners[type]||[]).push(listener);}
 dispatch(type,extra={}){for(const listener of this.listeners[type]||[])listener({target:this,preventDefault(){},...extra});}
 focus(){this.focused=true;}
}
const ids=new Map();
const tabs=[['map','panel-map'],['logging','panel-logging'],['improvements','panel-improvements']].map(([view,panel])=>{
 const tab=new Element('button',{role:'tab','aria-controls':panel});tab.dataset.view=view;ids.set('tab-'+view,tab);
 const section=new Element('section');ids.set(panel,section);if(view!=='map'){section.hidden=true;section.setAttribute('inert','');}return tab;});
const calls=[];let requestMap=null;
const mapApi={selectNode:id=>{calls.push('select:'+id);if(ids.get('panel-map').hidden&&requestMap)requestMap();return true;},
 hidePanel:()=>{calls.push('hide');ids.get('panel-map').hidden=true;return true;},
 showPanel:()=>{calls.push('show');ids.get('panel-map').hidden=false;return true;},onRequestMap:callback=>{requestMap=callback;}};
const documentListeners={};
const document={getElementById:id=>ids.get(id)||null,querySelectorAll:selector=>selector==='#report-tabs [role=tab]'?tabs:[],
 addEventListener:(type,listener)=>{(documentListeners[type]=documentListeners[type]||[]).push(listener);}};
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),vm.createContext({document,window:{scenarioMapView:mapApi},console}),{timeout:2000});
const panel=id=>ids.get(id);
assert.equal(panel('panel-map').getAttribute('role'),'tabpanel');assert.equal(typeof requestMap,'function','the renderer can ask for the map tab');
tabs[1].dispatch('click');
assert.equal(panel('panel-logging').hidden,false);assert.equal(panel('panel-logging').getAttribute('inert'),null);
assert.equal(panel('panel-improvements').hidden,true);assert.equal(panel('panel-improvements').getAttribute('inert'),'');
assert.equal(panel('panel-map').hidden,true,'the map panel is hidden by the renderer hook');assert.deepEqual(calls,['hide']);
assert.equal(tabs[1].getAttribute('aria-selected'),'true');assert.equal(tabs[0].tabIndex,-1);
tabs[1].dispatch('keydown',{key:'ArrowRight'});
assert.equal(panel('panel-improvements').hidden,false);assert.equal(panel('panel-logging').hidden,true);assert.equal(tabs[2].focused,true);
const button={dataset:{reportOperation:'save'}};
for(const listener of documentListeners.click)listener({target:{closest:selector=>selector==='[data-report-operation]'?button:null},preventDefault(){}});
assert.deepEqual(calls.slice(-3),['hide','select:save','show'],'a table button selects through the renderer and returns to the map tab');
assert.equal(panel('panel-map').hidden,false);assert.equal(panel('panel-improvements').hidden,true);assert.equal(tabs[0].getAttribute('aria-selected'),'true');
for(const listener of documentListeners.click)listener({target:{closest:()=>null},preventDefault(){}});
assert.equal(calls.length,4,'unrelated clicks do nothing');
tabs[0].dispatch('keydown',{key:'End'});assert.equal(tabs[2].getAttribute('aria-selected'),'true');
console.log(JSON.stringify({shell_checks:14,result:'PASS',scope:'inert DOM unit; no browser or visual QA'}));
"""

RENDERER_CHECKS = r"""
const assert=require('node:assert/strict');
const states={save:'C',unlogged:'U'},stateOf=id=>states[id]||'X';
// Filter: a block matches through any child; a non-matching block is only dimmed by the caller.
assert.equal(coverageMatches(['save','unlogged'],stateOf,'C'),true);
assert.equal(coverageMatches(['save','unlogged'],stateOf,'X'),false);
assert.equal(coverageMatches(['save'],stateOf,'all'),true);
assert.deepEqual(coverageCounts(['save','unlogged'],stateOf),{C:1,P:0,U:1,X:0});
const nodes=[{id:'save'},{id:'unlogged'}],edges=[{id:'e1',source:'save',target:'unlogged',evidence_status:'observed'},{id:'e2',source:'save',target:'unlogged',evidence_status:'inferred'}];
const stages=[{id:'stage-1',collapsible:true,entry_operation_id:'save'}],membership={save:'stage-1',unlogged:'stage-1'};
const closed=projectScenarioStages(nodes,edges,stages,membership,[]);
assert.equal(closed.items.length,1);assert.deepEqual(closed.items[0].members,['save','unlogged']);assert.equal(closed.links.length,0);
const opened=projectScenarioStages(nodes,edges,stages,membership,['stage-1']);
assert.equal(opened.links.length,1,'parallel transitions inside an open block share one drawn link');
assert.deepEqual(opened.links[0].transitions.map(edge=>edge.id),['e1','e2'],'every original transition is kept');
assert.equal(opened.links[0].evidence_status,'inferred','the weakest evidence of the merged transitions is shown');
const sizes=new Map(nodes.map(node=>[node.id,{w:224,h:96}]));
const loop=layoutScenarioGraph(nodes,[...edges,{id:'retry',source:'save',target:'save',kind:'sequence'}],sizes,false,['save']);
assert.equal(loop.positions.size,2,'global detail keeps every operation');assert.ok(loop.outerEdges.has('retry'),'a self-loop is routed, not dropped');
console.log(JSON.stringify({renderer_checks:12,result:'PASS'}));
"""


class ControllerTests(unittest.TestCase):
    def bundle(self, root):
        repo = root/'repo'; repo.mkdir(); (repo/'app.py').write_text(CODE)
        model = {'schema_version': '2.0', 'scenario': {'title': 'Synthetic interface'}, 'sources': [{'id': 'source-1'}],
                 'nodes': [operation(), operation(identifier='unlogged', first=10, last=11, effect='unknown', outcome='unknown')],
                 'edges': [{'id': 'e1', 'source': 'save', 'target': 'unlogged'},
                           {'id': 'e2', 'source': 'save', 'target': 'unlogged', 'condition': 'Another condition'}],
                 'run': {'status': 'completed', 'id': 'fixture'}}
        view = MapView.from_dict(model)
        data = review_logging(view, snapshot_repository(repo, max_files=None, max_chars=None),
                              Scripted(lambda p, n: model_reply(p, key='absent' if p['operation']['id'] == 'unlogged' else 'done')))
        data['hierarchy'] = {'map_revision': view.revision, 'membership': {'save': 'stage-1', 'unlogged': 'stage-1'},
                             'stages': [{'id': 'stage-1', 'name': 'Requests', 'operation_ids': ['save', 'unlogged'],
                                         'collapsible': True, 'entry_operation_id': 'save'}]}
        return data

    def test_headline_counts_and_block_hierarchy_are_server_side(self):
        with tempfile.TemporaryDirectory() as raw:
            data = self.bundle(Path(raw).resolve())
            page = build_html(data)
            # Same fixture states and headline as the former controller check (one C, one U of two operations).
            self.assertEqual(data['summary']['counts'], {'C': 1, 'P': 0, 'U': 1, 'X': 0})
            self.assertIn('Без достаточного покрытия логами: 50.0%', page)
            self.assertIn('id="map-hierarchy"', page)
            self.assertIn('data-report-operation="save"', page)

    @unittest.skipUnless(shutil.which('node'), 'Optional Node runtime required only for controller tests')
    def test_report_shell_tabs_and_table_selection_with_inert_dom(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            script = root/'shell.js'; script.write_text(SHELL_JS)
            check = subprocess.run([shutil.which('node'), '--check', str(script)], capture_output=True, text=True, timeout=10)
            self.assertEqual(check.returncode, 0, check.stderr)
            harness = root/'shell-harness.js'; harness.write_text(SHELL_HARNESS)
            run = subprocess.run([shutil.which('node'), str(harness), str(script)], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)['result'], 'PASS')

    @unittest.skipUnless(shutil.which('node'), 'Optional Node runtime required only for controller tests')
    def test_renderer_filter_projection_and_self_loop_without_a_browser(self):
        script = _PRESENTATION_SCRIPT
        source = ("'use strict';\nconst text=v=>v===null||v===undefined?'':typeof v==='string'?v:String(v);\n"
                  + script[script.index('// overlay pure helpers: begin'):script.index('// overlay pure helpers: end')]
                  + _STAGE_PROJECTION_JS + _GRAPH_LAYOUT_JS + RENDERER_CHECKS)
        run = subprocess.run([shutil.which('node'), '-e', source], capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['result'], 'PASS')


if __name__ == '__main__': unittest.main()
