"""Compact offline passport: escaped HTML data, one embedded font and a fixed, CSP-hashed UI script.

The script holds no layout logic: scenario views arrive fully computed in data
attributes. Opening report.html#layout-check measures the rendered text and
boxes in the browser and publishes a JSON result in #layout-check-result.
"""

import base64
import hashlib
import html
import math

from . import report as r
from . import scenario_layout as L
from .graph_font import FONT_BASE64, FONT_FAMILY, FONT_FORMAT, FONT_LICENSE, FONT_MIME

SCRIPT = r"""(()=>{'use strict';
const cspViolations=[];
document.addEventListener('securitypolicyviolation',e=>cspViolations.push(String(e.violatedDirective)+' '+String(e.blockedURI||'')));
const panes=[...document.querySelectorAll('[data-pane]')];
function show(hash){
 const target=hash.length>1?document.getElementById(hash.slice(1)):null;
 const pane=target?.closest('[data-pane]')||document.getElementById('map');
 if(!pane)return;
 panes.forEach(p=>p.hidden=p!==pane);
 document.querySelectorAll('.tab').forEach(a=>a.classList.toggle('active',a.hash==='#'+pane.id));
 const shownCard=target?.closest('.stage');
 document.querySelectorAll('.stage').forEach(e=>e.classList.toggle('chosen',e===shownCard));
 document.querySelectorAll('.operation-node').forEach(n=>n.classList.toggle('selected',n.dataset.op===hash.slice(1)));
 const inspector=document.querySelector('.stage-inspector');
 if(inspector){inspector.hidden=false;const overview=inspector.querySelector('.inspector-overview');if(overview)overview.hidden=!!shownCard;}
 if(target){let p=target;while(p){if(p.tagName==='DETAILS')p.open=true;p=p.parentElement;}}
 if(target&&pane.id!=='map')target.scrollIntoView({block:'start'});
}
document.addEventListener('click',e=>{const a=e.target.closest('a[href^="#"]');if(!a)return;const hash=a.getAttribute('href');e.preventDefault();history.replaceState(null,'',hash);show(hash);highlight(hash);});
window.addEventListener('hashchange',()=>{show(location.hash);highlight(location.hash);});
show(location.hash||'#map');
const canvas=document.querySelector('.map svg'),scroller=canvas?canvas.parentElement:null;
const scenarioMap=!!(canvas&&canvas.hasAttribute('data-views'));
const views=scenarioMap?String(canvas.dataset.views).split(' '):['all'];
const part=(value,k,sep)=>{const items=String(value||'').split(sep);return k<items.length?items[k]:'';};
let zoom=1,current=scenarioMap?String(canvas.dataset.default||views[0]):'all';
function resize(){if(!canvas)return;const box=canvas.viewBox.baseVal;canvas.setAttribute('width',String(Math.round(box.width*zoom)));canvas.setAttribute('height',String(Math.round(box.height*zoom)));const label=document.getElementById('zoom-label');if(label)label.textContent=Math.round(zoom*100)+'%';}
function fitZoom(){return canvas&&scroller&&scroller.clientWidth>0?(scroller.clientWidth-12)/canvas.viewBox.baseVal.width:1;}
document.getElementById('zoom-in')?.addEventListener('click',()=>{zoom=Math.min(1.8,zoom+.15);resize();});
document.getElementById('zoom-out')?.addEventListener('click',()=>{zoom=Math.max(.3,zoom-.15);resize();});
document.getElementById('fit-map')?.addEventListener('click',()=>{zoom=Math.max(.3,Math.min(1,fitZoom()));resize();});
document.getElementById('node-search')?.addEventListener('input',e=>{const q=e.target.value.toLocaleLowerCase();document.querySelectorAll('.node').forEach(n=>n.classList.toggle('search-dimmed',!!q&&!n.textContent.toLocaleLowerCase().includes(q)));});
const chips=[...document.querySelectorAll('.scenario-filter')];
function applyView(id){
 if(!scenarioMap)return;
 const k=Math.max(0,views.indexOf(id));current=views[k];
 const nodes=[...canvas.querySelectorAll('.operation-node')],nodeLayer=canvas.querySelector('.node-layer');
 nodes.forEach(n=>{n.setAttribute('transform','translate(0 '+(Number(part(n.dataset.ys,k,' '))-Number(n.dataset.y))+')');n.dataset.active=part(n.dataset.as,k,' ');});
 if(nodeLayer)nodes.sort((a,b)=>Number(part(a.dataset.order,k,' '))-Number(part(b.dataset.order,k,' '))).forEach(n=>nodeLayer.appendChild(n.parentElement));
 const edges=[...canvas.querySelectorAll('.scenario-edge')],edgeLayer=canvas.querySelector('.edge-layer');
 edges.forEach(e=>{
  const d=part(e.dataset.d,k,'|'),[color,marker,width,dash,active]=part(e.dataset.style,k,'|').split(' ');
  e.querySelectorAll('.edge-halo,.edge-line').forEach(p=>p.setAttribute('d',d));
  const line=e.querySelector('.edge-line');
  if(line){line.setAttribute('stroke',color);line.setAttribute('stroke-width',width);if(dash&&dash!=='-')line.setAttribute('stroke-dasharray',dash.replace(',',' '));else line.removeAttribute('stroke-dasharray');line.setAttribute('marker-end',`url(#${marker})`);}
  const caption=e.querySelector('.edge-caption');if(caption)caption.setAttribute('transform','translate('+part(e.dataset.l,k,'|')+')');
  e.dataset.active=active;
 });
 if(edgeLayer)edges.sort((a,b)=>Number(a.dataset.active)-Number(b.dataset.active)||Number(a.dataset.edgeIndex)-Number(b.dataset.edgeIndex)).forEach(e=>edgeLayer.appendChild(e));
 canvas.querySelectorAll('.stage-region').forEach(g=>{g.querySelector('rect')?.setAttribute('height',part(g.dataset.hs,k,' '));const count=g.querySelector('.region-count');if(count)count.textContent=part(g.dataset.counts,k,'|');});
 const zone=canvas.querySelector('.context-zone');
 if(zone){const sep=part(zone.dataset.sep,k,'|');
  if(!sep||sep==='-'){zone.setAttribute('hidden','');zone.setAttribute('display','none');}
  else{const [y,tx,ty,x0,x1]=sep.split(' ');zone.removeAttribute('hidden');zone.setAttribute('display','inline');zone.querySelector('path')?.setAttribute('d','M '+x0+' '+y+' H '+x1);const text=zone.querySelector('text');if(text){text.setAttribute('x',tx);text.setAttribute('y',ty);}}}
 canvas.setAttribute('viewBox','0 0 '+canvas.viewBox.baseVal.width+' '+part(canvas.dataset.hs,k,' '));
 chips.forEach(b=>{const on=b.dataset.scenario===current;b.classList.toggle('active',on);b.setAttribute('aria-pressed',String(on));});
 const readout=document.getElementById('scenario-readout'),chip=chips.find(b=>b.dataset.scenario===current);
 if(readout)readout.textContent=(chip?chip.textContent:'Все сценарии')+' · шагов: '+canvas.querySelectorAll('.operation-node[data-active="1"]').length;
 resize();
}
function reveal(){if(!scroller)return;scroller.scrollTop=0;const xs=[...canvas.querySelectorAll('.operation-node[data-active="1"]')].map(n=>Number(n.dataset.x)).sort((a,b)=>a-b);if(xs.length&&(xs[0]*zoom<scroller.scrollLeft||xs[0]*zoom>scroller.scrollLeft+scroller.clientWidth-80))scroller.scrollLeft=Math.max(0,xs[0]*zoom-40);}
chips.forEach(b=>b.addEventListener('click',()=>{applyView(b.dataset.scenario);reveal();}));
const levelButtons=[...document.querySelectorAll('.detail-level')];
function setLevel(level){
 levelButtons.forEach(b=>{const on=b.dataset.detailLevel===level;b.classList.toggle('active',on);b.setAttribute('aria-pressed',String(on));});
 document.querySelectorAll('[data-level-view]').forEach(v=>{v.hidden=!v.dataset.levelView.split(' ').includes(level);});
 document.dispatchEvent(new CustomEvent('detail-level-change',{detail:level}));
 if(level==='operations'&&canvas)resize();
}
levelButtons.forEach(b=>b.addEventListener('click',()=>setLevel(b.dataset.detailLevel)));
// Structural scope over map transitions, cycles included; a bypass from an ancestor to a descendant stays outside.
function pathScope(id,edges){
 const out=new Map(),inc=new Map(),add=(map,a,b)=>{if(!map.has(a))map.set(a,[]);map.get(a).push(b);};
 edges.forEach(e=>{add(out,e.dataset.fromNode,e.dataset.toNode);add(inc,e.dataset.toNode,e.dataset.fromNode);});
 const reach=map=>{const seen=new Set([id]),todo=[id];while(todo.length){for(const next of map.get(todo.pop())||[])if(!seen.has(next)){seen.add(next);todo.push(next);}}return seen;};
 const up=reach(inc),down=reach(out);
 return {nodes:new Set([...up,...down]),edges:new Set(edges.filter(e=>{const a=e.dataset.fromNode,b=e.dataset.toNode;return (up.has(a)&&up.has(b))||(down.has(a)&&down.has(b));}))};
}
function highlight(hash){
 if(!canvas||!scenarioMap)return;
 const nodes=[...canvas.querySelectorAll('.operation-node')],edges=[...canvas.querySelectorAll('.scenario-edge')];
 nodes.concat(edges).forEach(x=>x.classList.remove('path-on','path-dim'));
 const chosen=nodes.find(n=>n.dataset.op===String(hash||'').slice(1));
 if(!chosen)return;
 const scope=pathScope(chosen.dataset.nodeId,edges);
 nodes.forEach(n=>n.classList.add(scope.nodes.has(n.dataset.nodeId)?'path-on':'path-dim'));
 edges.forEach(e=>e.classList.add(scope.edges.has(e)?'path-on':'path-dim'));
}
document.getElementById('expand-map')?.addEventListener('click',()=>document.getElementById('map').classList.toggle('map-expanded'));
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.getElementById('map')?.classList.remove('map-expanded');});
function box(value){const [x,y,w,h]=String(value||'').split(' ').map(Number);return {x,y,w,h};}
function overlap(a,b){return a.x<b.x+b.w-.5&&b.x<a.x+a.w-.5&&a.y<b.y+b.h-.5&&b.y<a.y+a.h-.5;}
function inside(r,b){return r.x>=b.x-.5&&r.y>=b.y-.5&&r.x+r.width<=b.x+b.w+.5&&r.y+r.height<=b.y+b.h+.5;}
function segments(d){const v=(String(d||'').match(/-?\d+(?:\.\d+)?/g)||[]).map(Number),out=[];for(let i=2;i+1<v.length;i+=2)out.push([v[i-2],v[i-1],v[i],v[i+1]]);return out;}
function hits(s,b){const [x1,y1,x2,y2]=s;return Math.min(x1,x2)<b.x+b.w&&Math.max(x1,x2)>b.x&&Math.min(y1,y2)<b.y+b.h&&Math.max(y1,y2)>b.y;}
function carries(s,b){const [x1,y1,x2,y2]=s,cx=b.x+b.w/2,cy=b.y+b.h/2;return y1===y2?Math.abs(y1-cy)<.6&&Math.min(x1,x2)<b.x&&Math.max(x1,x2)>b.x+b.w:Math.abs(x1-cx)<.6&&Math.min(y1,y2)<b.y&&Math.max(y1,y2)>b.y+b.h;}
function layoutCheck(){
 if(canvas.closest('.static-operation-graph')?.hidden&&window.runHierarchyLayoutCheck){
  const result=window.runHierarchyLayoutCheck();result.csp=[...cspViolations];if(cspViolations.length)result.ok=false;
  const out=document.getElementById('layout-check-result');if(out){out.textContent=JSON.stringify(result,null,1);out.hidden=false;}
  document.documentElement.dataset.layoutCheck=result.ok?'pass':'fail';window.layoutCheck=result;return result;
 }
 const family=canvas.dataset.font||'',result={ok:true,font:{family,loaded:false,check:false},csp:cspViolations,views:[]};
 try{result.font.check=document.fonts.check('12px "'+family+'"');result.font.loaded=[...document.fonts].some(f=>f.family.replace(/["']/g,'')===family&&f.status==='loaded');}catch(error){result.font.error=String(error);}
 const start=current;
 views.forEach((view,k)=>{
  applyView(view);
  const problems=[],bad=(kind,detail)=>problems.push(kind+': '+detail);
  canvas.querySelectorAll('text[data-max-w]').forEach(t=>{const max=Number(t.dataset.maxW);t.querySelectorAll('tspan').forEach(s=>{const w=s.getComputedTextLength();if(w>max+.5)bad('text-width',s.textContent+' '+w.toFixed(1)+'>'+max);});});
  const nodes=[...canvas.querySelectorAll('.operation-node')].map(n=>{
   const b=box(n.dataset.box),dy=Number(part(n.dataset.ys,k,' '))-Number(n.dataset.y),text=n.querySelector('.op-name');
   if(text&&!inside(text.getBBox(),box(n.dataset.tb)))bad('node-text-outside',n.dataset.nodeId);
   return {name:'node '+n.dataset.nodeId,id:n.dataset.nodeId,box:{x:b.x,y:b.y+dy,w:b.w,h:b.h}};});
  const edges=[...canvas.querySelectorAll('.scenario-edge')].map(e=>{
   const item={e,segs:segments(e.querySelector('.edge-line')?.getAttribute('d')),label:null},caption=e.querySelector('.edge-caption');
   if(caption){const bg=caption.querySelector('rect'),[x,y]=part(e.dataset.l,k,'|').split(' ').map(Number),w=Number(bg.getAttribute('width')),h=Number(bg.getAttribute('height')),text=caption.querySelector('text');
    if(text&&!inside(text.getBBox(),{x:0,y:0,w,h}))bad('label-text-outside',e.dataset.edgeIndex);
    item.label={name:'label '+e.dataset.edgeIndex,box:{x,y,w,h}};}
   return item;});
  const headers=[...canvas.querySelectorAll('.region-title,.region-count,.region-empty')].map(t=>{const b=t.getBBox();return {x:b.x,y:b.y,w:b.width,h:b.height};});
  const boxes=nodes.concat(edges.filter(i=>i.label).map(i=>i.label));
  boxes.forEach((a,i)=>{boxes.slice(i+1).forEach(b=>{if(overlap(a.box,b.box))bad('overlap',a.name+' / '+b.name);});if(headers.some(h=>overlap(a.box,h)))bad('overlap-header',a.name);});
  edges.forEach(item=>{
   const ends=[item.e.dataset.fromNode,item.e.dataset.toNode],number=item.e.dataset.edgeIndex;
   nodes.forEach(n=>{if(!ends.includes(n.id)&&item.segs.some(s=>hits(s,n.box)))bad('edge-through-node',number+' / '+n.name);});
   if(item.label&&!item.segs.some(s=>carries(s,item.label.box)))bad('label-detached',number);
   edges.forEach(other=>{if(other!==item&&other.label&&item.segs.some(s=>hits(s,other.label.box)))bad('edge-through-label',number+' / '+other.label.name);});
   if(headers.some(h=>item.segs.some(s=>hits(s,h))))bad('edge-through-header',number);
  });
  if(problems.length)result.ok=false;
  result.views.push({view,problems});
 });
 if(!result.font.loaded||!result.font.check||cspViolations.length)result.ok=false;
 applyView(start);
 const out=document.getElementById('layout-check-result');if(out){out.textContent=JSON.stringify(result,null,1);out.hidden=false;}
 document.documentElement.dataset.layoutCheck=result.ok?'pass':'fail';window.layoutCheck=result;
 return result;
}
if(canvas){
 applyView(current);zoom=innerWidth>640?Math.max(.85,Math.min(1,fitZoom())):.85;resize();
 let pan=null;
 scroller.addEventListener('pointerdown',e=>{if(e.target.closest('a,button'))return;pan={x:e.clientX,y:e.clientY,left:scroller.scrollLeft,top:scroller.scrollTop};scroller.setPointerCapture(e.pointerId);});
 scroller.addEventListener('pointermove',e=>{if(pan){scroller.scrollLeft=pan.left+pan.x-e.clientX;scroller.scrollTop=pan.top+pan.y-e.clientY;}});
 scroller.addEventListener('pointerup',()=>{pan=null;});scroller.addEventListener('pointercancel',()=>{pan=null;});
 if(scenarioMap){window.runLayoutCheck=layoutCheck;
  if(location.hash==='#layout-check'){const fonts=document.fonts,ready=fonts?fonts.load('14px "'+(canvas.dataset.font||'')+'"').then(()=>fonts.ready):Promise.resolve();ready.catch(()=>null).then(()=>requestAnimationFrame(()=>layoutCheck()));}}
 highlight(location.hash);
}
})();"""

from .hierarchy_graph import SCRIPT as HIERARCHY_SCRIPT
SCRIPT += HIERARCHY_SCRIPT

_FONT_FACE = ('@font-face{font-family:"' + FONT_FAMILY + '";src:url(data:' + FONT_MIME + ';base64,' + FONT_BASE64
              + ') format("' + FONT_FORMAT + '");font-weight:400;font-style:normal;font-display:block}\n')
# The layout measures with the font's plain advances, so the browser must not kern, ligate or embolden.
_GRAPH_TEXT = ('.map svg text,.hierarchy-scroll svg text{font-family:"' + FONT_FAMILY + '";font-weight:400;font-style:normal;font-kerning:none;'
               'font-variant-ligatures:none;font-feature-settings:"kern" 0,"liga" 0,"clig" 0,"calt" 0;font-synthesis:none;'
               'letter-spacing:0;word-spacing:0;text-rendering:geometricPrecision}\n')

CSS = _FONT_FACE + _GRAPH_TEXT + """
:root{--fg:#233247;--muted:#748195;--line:#dce3ec;--bg:#f6f8fc;--purple:#7255ca}
html,body{height:100%}body{font-size:13px;overflow:hidden}a{text-decoration:none}a:hover{text-decoration:underline}
[hidden]{display:none!important}.app{height:100vh;display:flex;flex-direction:column}
.app-head{display:flex;justify-content:space-between;gap:28px;align-items:center;padding:18px 25px;background:#fff;border-bottom:1px solid var(--line)}
.app-head h1{font-size:22px;line-height:1.25;max-width:700px;margin:3px 0}.app-head .eyebrow{font-size:10px}
.head-counts{display:flex;gap:26px;flex:none}.head-counts span{display:block;color:var(--muted);font-size:10px}.head-counts b{display:block;font-size:23px}
.workspace{display:flex;flex:1;min-height:0}.sidebar{width:250px;flex:none;background:#fff;border-right:1px solid var(--line);padding:20px 14px;overflow:auto}
.sidebar h2{font-size:15px;margin:0 4px 15px}.side-stage{display:block;margin:8px 0;border:1px solid var(--line);border-radius:8px;padding:12px;color:var(--fg)}
.side-stage strong{display:block;font-size:12px;margin:7px 0}.side-stage small{font-size:10px;color:var(--muted)}.side-stage.active{border-color:#a790e0;background:#f7f4ff;box-shadow:inset 3px 0 var(--purple)}
.content{flex:1;min-width:0;display:flex;flex-direction:column}.tabs{display:flex;gap:5px;overflow-x:auto;padding:10px 16px;background:#fff;border-bottom:1px solid var(--line);flex:none}
.tab{display:block;white-space:nowrap;padding:8px 11px;border-radius:6px;font-size:12px;font-weight:600;color:#6b7a8f}.tab.active{color:#6246ad;background:#eee9fa}
.panes{flex:1;min-height:0;overflow:auto}.panes>section{margin:0;border:0;border-radius:0;padding:18px 22px;min-height:100%;background:var(--bg)}
.panes>section>h2{font-size:17px;margin-bottom:12px}.map-layout{display:flex;gap:16px;align-items:flex-start}.map-main{flex:1;min-width:0}.stage-inspector{width:310px;flex:none}
.stage-inspector .stage{display:none}.stage-inspector .stage.chosen{display:block}.stage-inspector .views{display:block}.stage-inspector .view{margin:7px 0;padding:8px}
.map{padding:0;border:1px solid var(--line);background:radial-gradient(#dce3ec .8px,transparent .8px) 0 0/20px 20px;overflow:auto;min-height:400px}
.map svg{max-width:none;min-width:750px}.map .en{font-size:14px;fill:#233247;font-weight:400}.map .ep{font-size:11px;font-weight:400}
.context-line{display:flex;gap:18px;flex-wrap:wrap;color:#68788d;font-size:11px;margin:0 0 12px}.context-line b{color:#233247}
.legend{font-size:10px;line-height:1.5;margin:7px 0 12px}.b{border-radius:4px;font-size:10px;padding:1px 5px;line-height:1.5;border-color:transparent}
.t-info{color:#6246ad;background:#eee9fa}.t-ok{color:#17695c;background:#e1f3ee}.t-warn{color:#88651f;background:#fff2d8}.t-bad{color:#a13551;background:#fae7ec}
.t-unknown{color:#617187;background:#eef1f6}.stage,.card,.metric{border-color:var(--line);box-shadow:0 2px 7px #20304a04}
.cards{grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr))}.card,.metric{padding:15px}.kv{font-size:12px;grid-template-columns:minmax(85px,max-content) minmax(0,1fr);gap:5px 12px}
.card .kv,.metric .kv{grid-template-columns:minmax(0,1fr)}.card .kv dt,.metric .kv dt{font-size:10px;margin-top:6px}
.card h4,.metric h4{font-size:12px}.where,.plan{font-size:12px;padding:9px}.blockers{font-size:11px;border-left-width:2px;background:#fff8f3}
.technical{font-size:11px}.section-meta{font-size:11px;margin-bottom:12px}summary{color:#7255ca;font-size:11px}
.discovery{margin:12px 0;padding:12px;background:#fff;border:1px solid var(--line);border-radius:8px}.discovery h3{margin:0 0 8px;font-size:13px}
.drawer-list{margin-top:12px}.hscroll{overflow:auto}.scope-status{color:#748195;font-size:11px;margin:5px 0}
table.wide{min-width:900px}th{font-size:10px;text-transform:uppercase;letter-spacing:.025em}td{font-size:12px}.note{font-size:10px}
.logging-coverage table{table-layout:fixed;min-width:680px}.logging-coverage th{font-size:12px;text-transform:none;letter-spacing:0}.logging-coverage td{font-size:13px;vertical-align:top;overflow-wrap:anywhere}.logging-coverage th:first-child{width:34%}.logging-coverage td p{margin:7px 0;color:#617187}.logging-links{margin:0;padding-left:16px}.logging-card h3{font-size:17px;line-height:1.4;margin:0 0 9px}.logging-card .kv{font-size:14px;line-height:1.6}.logging-card .kv dt{font-size:12px;color:#617187}.logging-card .plan{font-size:14px}.logging-card .plan h4{font-size:13px}.logging-card th{text-transform:none;font-size:12px;letter-spacing:0}.logging-card td{font-size:13px;overflow-wrap:anywhere}.logging-card .technical{margin-top:12px}.logging-card .where{margin-top:12px}
@media(max-width:900px){.head-counts{gap:12px}.sidebar{width:205px}.map-layout{display:block}.stage-inspector{width:auto;margin-top:12px}.map svg{min-width:750px}}
@media(max-width:640px){body{overflow:auto}.app{height:auto;min-height:100vh}.workspace,.content{display:block}.sidebar{display:none}.app-head{display:block;padding:14px}.app-head h1{font-size:19px}.head-counts{margin-top:12px;justify-content:space-between}.head-counts b{font-size:20px}.tabs{position:sticky;top:0;z-index:3;padding:8px}.panes{overflow:visible}.panes>section{padding:14px 10px}.kv{grid-template-columns:minmax(0,1fr)}}
.sidebar{display:none}.app-head{padding:15px 24px 12px}.panes>section{background:#fff;padding:12px 24px}.tabs{background:#f6f7f7;padding:7px 24px}.tab.active{background:#fff;box-shadow:0 1px 4px #182d3912;color:#8b3fd9}
.map{background-color:#f6faf8;background-image:radial-gradient(#cbded6 .7px,transparent .7px);border-radius:12px;min-height:450px;max-height:66vh}.map svg{min-width:0}
.map-layout{gap:18px}.stage-inspector{display:block;background:#fff;border:1px solid #d0d7dd;border-radius:12px;padding:16px;width:340px;position:sticky;top:0;max-height:74vh;overflow:auto}
.stage-inspector h3{font-size:14px;margin-top:0}.stage-inspector .stage{border:0;box-shadow:none;padding:0}.inspector-overview .kv{grid-template-columns:1fr}.inspector-overview dt{font-size:11px;margin-top:12px}.inspector-overview dd{font-size:13px}
.map-tools{display:flex;justify-content:space-between;gap:14px;align-items:center;margin:10px 0}.map-tools input{width:210px;min-width:100px;border:1px solid #d0d7dd;border-radius:8px;padding:8px 10px;background:#fff;font:inherit}
.zoom-controls{display:flex;gap:5px;align-items:center}.zoom-controls button{border:1px solid #d0d7dd;border-radius:6px;background:#fff;padding:5px 10px;color:#5a6d7c;font:inherit;cursor:pointer}.zoom-controls output{font-size:11px;min-width:35px;text-align:center}
.node.dimmed{opacity:.25}.inspector-fact{padding:8px 0;border-top:1px solid #e4e9e7;font-size:12px}.inspector-fact a{color:#8b3fd9}.legend{font-size:10px;max-width:100%}
@media(max-width:1000px){.map-layout{display:block}.stage-inspector{position:static;width:100%;margin-top:14px;max-height:none}.map{max-height:none}.map-tools{flex-wrap:wrap}}
@media(max-width:640px){.app-head{padding:14px}.tabs{padding:8px}.panes>section{padding:12px}.map{min-height:400px}.map-tools input{width:180px}}
.map-layout{display:grid;grid-template-columns:minmax(0,1fr) 292px;gap:18px;align-items:start}
.stage-inspector{width:auto;padding:20px;border-color:#dee5e0;border-radius:14px;max-height:calc(100vh - 215px);position:sticky;top:0}
.map{min-height:490px;max-height:calc(100vh - 270px);border-color:#dfebe4;border-radius:15px;cursor:grab;touch-action:none}
.map:active{cursor:grabbing}.map a{cursor:pointer}.map svg{display:block;margin:0 auto;font-family:inherit}
.map .region-title{font-size:15px;fill:#33463d}.map .region-count,.map .region-empty{font-size:11px;fill:#66746c}
.map .op-name{font-size:14px;fill:#24362e}
.map .edge-label{font-size:12px;fill:#3b4850}
.edge-label-bg{fill:#f8fbf9;stroke:#d6e0da;stroke-width:.8}.edge-halo{fill:none;stroke:#f8fbfa;stroke-width:6;stroke-linejoin:round}.edge-line{stroke-linejoin:round}
.operation-node .op-shape{stroke-width:1.3px;transition:stroke .15s,stroke-width .15s}.operation-node:hover .op-shape,.operation-node.selected .op-shape{stroke:#9860cf;stroke-width:2.2px}
.node.dimmed,.scenario-edge.dimmed,.node.search-dimmed{opacity:.15}.scenario-edge{transition:opacity .15s}
.scenario-chips{display:flex;gap:7px;flex-wrap:wrap;padding:0 0 6px}.scenario-filter{border:1px solid #e1e7e3;border-radius:20px;padding:7px 13px;background:#fff;font:inherit;font-size:11px;color:#728178;cursor:pointer}
.scenario-filter.active{color:#7851b8;border-color:#d4c2ec;background:#f6f1fc}.graph-caption{font-size:10px;color:#93a199;margin-top:9px}
.map-tools{margin:8px 0 12px}.map-tools input{font-size:12px;background:#f9fbf9;border-color:#e1e7e3;width:230px}.zoom-controls button{border-color:#e1e7e3;padding:5px 9px}
.inspector-kicker{font-size:10px;color:#9b7bb9;margin:0 0 10px}.stage-inspector h3{font-size:18px;line-height:1.4;margin:0 0 12px;font-weight:600}
.inspector-overview>p:not(.inspector-kicker){font-size:12px;line-height:1.7;color:#738078}.process-unit{padding:16px 0;border-top:1px solid #edf0ed;margin-top:18px}.process-unit span{display:block;font-size:10px;color:#8c998f}.process-unit strong{display:block;font-size:12px;font-weight:500;margin-top:5px}
.scenario-list>div{border-top:1px solid #edf0ed;padding:13px 0}.scenario-list strong{font-size:12px;font-weight:550}.scenario-list p{font-size:11px;color:#869389;line-height:1.6}.inspector-hint{font-size:11px!important;color:#a0aaa4!important;border-top:1px solid #edf0ed;padding-top:14px}
.operation-description{font-size:13px;line-height:1.8;color:#65746b}.operation-actor{font-size:11px;color:#9a84af}.operation-card .technical{margin-top:23px;border-top:1px solid #edf0ed;padding-top:14px}
.next-steps{list-style:none;padding:0}.next-steps li{padding:12px 0;border-bottom:1px solid #edf0ed}.next-steps span,.next-steps strong{display:block;font-size:11px;font-weight:400}.next-steps span{color:#92a096;font-size:10px}.next-steps strong{margin-top:4px;color:#4d6657}
.operation-card h3:not(:first-of-type){font-size:12px;margin-top:25px}.map-questions{font-size:12px;margin:18px 0}.map-questions summary{color:#8b988f}.map-questions li{margin:8px 0;color:#758579}
.panes>section>h2{font-size:15px;margin:0 0 13px}.section-meta{margin:0 0 12px}.section-meta summary{color:#a3aba6;font-size:10px}.section-meta .kv{margin-top:10px}
#map>h2{display:none}.stage-inspector .stage>h4{font-size:17px}.stage-inspector .views{margin-top:18px}.stage-inspector .view{border:0;border-top:1px solid #edf0ed;border-radius:0;padding:10px 0}.stage-inspector .view h4{font-size:11px}
.map-expanded{position:fixed!important;inset:12px;z-index:20;overflow:auto;background:white;box-shadow:0 0 0 100vmax #233c3155;border-radius:16px!important}.map-expanded .map{max-height:calc(100vh - 180px)}
@media(max-width:1000px){.map-layout{display:block}.stage-inspector{position:static;width:100%;max-height:none}.map{max-height:none}.scenario-chips{gap:5px}}
@media(max-width:640px){.head-counts{justify-content:flex-start;gap:30px}.map{min-height:360px}.map-tools{flex-wrap:wrap;gap:9px}.map-tools input{width:100%}.stage-inspector{padding:16px}.map-expanded{inset:3px}}

.coverage-legend{display:flex;gap:8px 15px;flex-wrap:wrap;margin:0 0 10px;color:#809087;font-size:10px}.coverage-legend span{display:inline-flex;align-items:center;gap:5px}.coverage-legend .coverage-swatch{width:12px;height:9px;border:1px solid;border-radius:3px}
.operation-node .op-shape{stroke:var(--cov-stroke,#d8e0df)}.operation-node:hover .op-shape,.operation-node.selected .op-shape{stroke:var(--cov-stroke,#d8e0df);stroke-width:2.5px;filter:drop-shadow(0 0 3px #758ca544)}
.operation-node[data-active="0"] .op-shape{fill:#f3f5f6;stroke:#c3cacf;opacity:.85}.operation-node[data-active="0"] .op-name{fill:#5f6b73}.operation-node.search-dimmed{opacity:.08}
.scenario-edge[data-active="0"] .edge-line{opacity:.6}.scenario-edge[data-active="0"] .edge-label{fill:#5f6b73}.scenario-edge[data-active="0"] .edge-label-bg{fill:#f5f7f7;stroke:#e1e6e8}
.map a:focus{outline:none}.map a:focus-visible .op-shape{stroke:#5b3fb0;stroke-width:3px}
.route-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--route-color,#8f9da9);margin-right:6px}.scenario-filter{border-color:var(--route-color,#d7dfe3);color:var(--route-color,#728178)}.scenario-filter.active{border-color:var(--route-color,#8f9da9);background:var(--route-bg,#f1f4f6);color:var(--route-color,#5e7080);box-shadow:0 0 0 1px var(--route-color,#8f9da9)}
.context-zone path{fill:none;stroke:#d5ddd9;stroke-width:1;stroke-dasharray:4 5}.context-zone text{font-size:11px;fill:#66746c}
.route-list{font-size:12px;margin:10px 0}.route-list summary{color:#8b988f}.route-list li{margin:5px 0;color:#4f5f57}
.transition-link{cursor:pointer}.transition-link:hover .edge-label-bg,.transition-link:focus .edge-label-bg{stroke:#7851b8;stroke-width:2}.transition-link .edge-line{pointer-events:stroke}
.map .node-kind,.map .edge-kind{font-size:9px;fill:#7851b8;font-weight:600;letter-spacing:.02em}.edge-label-bg{stroke-dasharray:3 2;fill:#fbfaf4}
.hierarchy-scroll{overflow:auto;max-height:76vh;background:#f8fbf9;border:1px solid #e1e9e3;border-radius:12px;touch-action:none}.hierarchy-scroll svg{display:block;font-family:'ProcessGraph Sans',sans-serif}.hierarchy-scroll [data-toggle-group]{cursor:pointer}.hierarchy-scroll [role="button"]:focus-visible rect{stroke:#7851b8;stroke-width:3px}.hierarchy-node.search-dimmed{opacity:.15}.hierarchy-node-name{fill:#30493d}.hierarchy-stage-name{fill:#52665b;font-weight:400}.hierarchy-condition-text{fill:#56615c}.hierarchy-edge:hover .hierarchy-edge-line{stroke:#7851b8;stroke-width:3px}.hierarchy-scenario{font:inherit;font-size:11px;border:1px solid #ddd2ea;color:#7851b8;border-radius:18px;background:#fff;padding:6px 12px;cursor:pointer}.hierarchy-scenario.active{background:#f1eaf8;border-color:#b097cb}.hierarchy-controls .scenario-chips{margin-bottom:10px}
.hierarchy-node.path-dim,.hierarchy-edge.path-dim,.hierarchy-node.path-dim+.hierarchy-info,.hierarchy-node.search-dimmed+.hierarchy-info{opacity:.18}.hierarchy-node.path-on .op-shape{stroke-width:2.5px}.hierarchy-edge.path-on .hierarchy-edge-line{stroke-width:2.5px}
.layout-check{font-size:11px;white-space:pre-wrap;background:#fff;border:1px solid #d0d7dd;border-radius:8px;padding:10px;max-height:40vh;overflow:auto}
.font-license pre{font-size:10px;max-height:40vh;overflow:auto}
.metric-status{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px;margin:0 0 14px}.metric-status h3{font-size:15px;margin:0 0 10px}.metric-pending{list-style:none;padding:0}.metric-pending>li{border-top:1px solid #edf0ed;padding:10px 0}.metric-pending strong{font-size:13px}
.detail-levels{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:0 0 6px}.detail-level{border:1px solid #e1e7e3;border-radius:16px;padding:6px 13px;background:#fff;font:inherit;font-size:11px;color:#5e7080;cursor:pointer}.detail-level.active{color:#7851b8;border-color:#d4c2ec;background:#f6f1fc;box-shadow:0 0 0 1px #d4c2ec}
.detail-note{font-size:10px;color:#809087;margin:0 0 10px;max-width:900px}.level-view h3{font-size:15px;margin:4px 0 10px}.level-stage{background:#fff;border:1px solid #e1e7e3;border-radius:10px;padding:12px 14px;margin:0 0 10px}.level-stage h4{font-size:13px;margin:0 0 6px}.level-counts{font-size:11px;color:#617187;margin:4px 0 8px}.level-stage ul,.level-stage-list{font-size:12px;padding-left:18px;margin:6px 0}
.op-section{border-top:1px solid #edf0ed;margin-top:14px;padding-top:11px;font-size:12px;line-height:1.55}.op-section h4{font-size:12px;margin:0 0 7px;color:#33463d}.op-section ul{padding-left:16px;margin:6px 0}.op-section li{margin:7px 0}.op-section p{margin:3px 0}.op-section summary{font-size:12px}
.operation-card .kv{grid-template-columns:minmax(0,1fr);gap:4px}.operation-card .kv dt{font-size:11px;margin-top:7px}.operation-card .kv dd{min-width:0;overflow-wrap:anywhere}
.op-external{border:1px solid #edf0ed;border-radius:8px;padding:8px 10px;margin:8px 0;background:#fbfcfb}.code-preview{font-size:11px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f9f7;border:1px solid #e5ece7;border-radius:6px;padding:6px 8px;margin:4px 0 8px}.code-explanation{border-top:1px dashed #e1e7e3;padding-top:7px;margin-top:7px}.interpretation{color:#617187}.code-link{overflow-wrap:anywhere}
.operation-node.path-dim,.scenario-edge.path-dim{opacity:.16}.operation-node.path-on .op-shape{stroke-width:2.4px}.scenario-edge.path-on .edge-line{stroke-width:2.6px}
.metric h3{font-size:16px;margin:0 0 16px}.metric-formula-label{font-size:10px;color:#809087;margin:0 0 6px}.metric .metric-formula{font-size:14px;background:#f6f9f7;border:1px solid #e5ece7;border-radius:8px;padding:14px;line-height:1.8;margin-bottom:18px}.metric-inputs{padding-left:18px;font-size:12px}.metric-inputs li{margin:9px 0}.metric-inputs p{color:#809087;font-size:10px}.metric-rules{border-top:1px solid #edf0ed;margin-top:16px;padding-top:12px}

"""

# These palettes are trusted constants, shared with SVG and scenario selectors.
from .scenario_graph import ROUTES, STATUS

for route_color, route_bg in ROUTES:
    CSS += f'.scenario-filter[data-route-color="{route_color}"]{{--route-color:{route_color};--route-bg:{route_bg}}}\n'
for state, (_, fill, border) in STATUS.items():
    CSS += f'.operation-node[data-coverage="{state}"]{{--cov-stroke:{border}}}\n'
    CSS += f'.coverage-swatch[data-coverage="{state}"]{{background:{fill};border-color:{border}}}\n'
CSS += '.coverage-swatch[data-coverage="unlinked"]{border-style:dashed}\n'

_CARD_W = 240
_COMPACT_STATES = {"предложено":"проект","неизвестно":"?","реализовано":"есть","наблюдается":"есть"}


def _legacy_card(stage):
    """Measured stage card of the legacy map: full name and state badges, wrapped, never shortened."""
    name=r._known(stage.get("name")) or '(без названия)'
    title=L.wrap(name,*L.NAME,200)
    badges=[]
    for key,_,letter in r._VIEWS:
        label,tone=r._state(r._get(stage.get(key),"state"))
        text=L.wrap(f'{letter}: {_COMPACT_STATES.get(label,label)}',*L.SMALL,196)
        badges.append((text,tone,math.ceil(text.width*L.LABEL_SAFETY)+12,text.height+6))
    rows,used=[[]],0
    for badge in badges:
        if rows[-1] and used+4+badge[2]>_CARD_W-20:
            rows.append([]);used=0
        used+=(4 if rows[-1] else 0)+badge[2];rows[-1].append(badge)
    height=14+title.height+10+sum(max(badge[3] for badge in row)+5 for row in rows)+6
    return name,title,rows,height


def graph(run, transitions, unknowns):
    count=len(run.stages)
    cols=min(3,max(1,count));width=cols*280+30
    cards=[_legacy_card(stage) for stage in run.stages]
    positions,y={},35
    for start in range(0,count,cols):
        row=cards[start:start+cols]
        for offset in range(len(row)):
            positions[start+offset+1]=(offset*280+25,y,row[offset][3])
        y+=max(card[3] for card in row)+60
    place={key:positions[number] for key,(number,_) in run.stage_index.items()}
    edges,nodes=[],[]
    for index,item in enumerate(transitions,1):
        a,b=place.get(r._known(item.get("from_stage"))),place.get(r._known(item.get("to_stage")))
        if a is None or b is None:continue
        label,tone=r._state(item.get("state"));stroke=r._TONES[tone][0]
        tip=f"№{index}: {r._stage_title(run,item.get('from_stage'))} → {r._stage_title(run,item.get('to_stage'))}; условие: {r._known(item.get('condition')) or 'не указано'}; состояние: {label}"
        (x,y0,h),(xx,yy,hh)=a,b
        if a==b:
            d=f"M{x+170} {y0} C{x+170} {y0-35} {x+240} {y0-35} {x+230} {y0+10}"
            endx,endy,midx,midy=x+230,y0+10,x+215,y0-22
        else:
            startx,starty=x+_CARD_W,y0+h/2;endx,endy=xx,yy+hh/2
            bend=25 if yy==y0 else 65
            d=f"M{startx} {starty} C{startx+bend} {starty} {endx-bend} {endy} {endx} {endy}"
            midx,midy=(startx+endx)/2,(starty+endy)/2-10
        dash='' if tone=='ok' else ' stroke-dasharray="5 4"'
        edges.append(f'<g class="edge"><title>{html.escape(tip)}</title><path d="{d}" fill="none" stroke="{stroke}" stroke-width="1.5"{dash}/>'
                     f'<polygon points="{endx},{endy} {endx-7},{endy-4} {endx-7},{endy+4}" fill="{stroke}"/>'
                     f'<text x="{midx}" y="{midy}" class="ep" fill="{stroke}">{index}</text></g>')
    for index,(stage,(name,title,rows,height)) in enumerate(zip(run.stages,cards),1):
        x,top,_=positions[index]
        content='<text class="en">'+''.join(f'<tspan x="{x+12}" y="{top+14+L.BASELINE[14]+n*title.line_height}">{html.escape(line)}</tspan>' for n,line in enumerate(title.lines))+'</text>'
        badge_y=top+14+title.height+10
        for row in rows:
            badge_x=x+10
            for text,tone,w,h in row:
                color,fill=r._TONES[tone]
                content+=f'<rect x="{badge_x}" y="{badge_y}" width="{w}" height="{h}" rx="4" fill="{fill}"/>'
                if len(text.lines)==1:
                    content+=f'<text x="{badge_x+w/2}" y="{badge_y+3+L.BASELINE[11]}" text-anchor="middle" class="ep" fill="{color}">{html.escape(text.lines[0])}</text>'
                else:
                    content+=f'<text text-anchor="middle" class="ep" fill="{color}">'+''.join(
                        f'<tspan x="{badge_x+w/2}" y="{badge_y+3+L.BASELINE[11]+n*text.line_height}">{html.escape(line)}</tspan>' for n,line in enumerate(text.lines))+'</text>'
                badge_x+=w+4
            badge_y+=max(badge[3] for badge in row)+5
        if not stage.get("alternatives_complete"):
            content+=f'<text x="{x+223}" y="{top+17}" class="ep" fill="#748195">?</text>'
        nodes.append(f'<a href="#st-{index}"><g class="node"><title>{html.escape(name)}</title><rect x="{x}" y="{top}" width="{_CARD_W}" height="{height}" rx="8" fill="#fff" stroke="#cfd8e5"/>'
                     f'<path d="M{x+8} {top} H{x+_CARD_W-8}" stroke="#7255ca" stroke-width="2"/>{content}</g></a>')
    height=max(400,y+30)
    if unknowns:
        nodes.append(f'<a href="#unknowns"><text x="25" y="{height-20}" class="ep" fill="#748195">Неизвестные ветви и пробелы: {len(unknowns)}</text></a>')
    tools='<div class="map-tools"><input id="node-search" type="search" placeholder="Найти этап" aria-label="Поиск этапа"><div class="zoom-controls"><button id="zoom-out" type="button" aria-label="Уменьшить">−</button><output id="zoom-label">100%</output><button id="zoom-in" type="button" aria-label="Увеличить">+</button><button id="fit-map" type="button">Вписать</button></div></div>'
    return tools+f'<div class="map"><svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="group" aria-label="Схема этапов и переходов процесса">'+''.join(edges+nodes)+'</svg></div>'


def _inventory(bundle):
    inventory=bundle.get("logging_inventory")
    if not isinstance(inventory,dict):return ''
    scope=inventory.get("scope") or {}
    rows=[(r._hcode(s.get("event_key")),r._hjoin(s.get("fields")),
           r._hcode(s.get("path"))+':'+r._h(s.get("start_line")),r._hcode(s.get("callee")))
          for s in inventory.get("sites",[]) if isinstance(s,dict)]
    return ('<div class="discovery" id="logging-registry"><h3>Полный реестр найденных записей</h3><p>'
            +r._h(scope.get("sites_returned"))+' точек · '+r._h(scope.get("files_scanned"))+' файлов</p>'
            +('<p class="blockers">Поиск ограничен: часть результатов пропущена.</p>' if scope.get("truncated") else '')
            +'<details><summary>Сообщения, найденные в исходниках</summary>'+r._htable(('Текст или ключ сообщения','Данные, передаваемые в запись','Где записывается','Вызов в коде'),rows,wide=True)+'</details></div>')


def _scope_details(value):
    if isinstance(value,dict):return {key:_scope_details(item) for key,item in value.items() if key!='limitations'}
    if isinstance(value,list):return [_scope_details(item) for item in value]
    return value


def font_license():
    """Complete OFL text of the embedded font; required wherever the font is distributed."""
    return ('<details class="font-license"><summary>Лицензия шрифта схемы</summary><pre>'
            + html.escape(FONT_LICENSE.lstrip('﻿').strip()) + '</pre></details>')


def build_html(bundle):
    run=r._Run(bundle);script_hash=base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
    csp=r._CSP+f"; script-src 'sha256-{script_hash}'"
    operation_count=sum(op.get('kind') in {'start','operation'} for op in r._records(r._get(run.sections['map'],'operations')) or [])
    scenario_count=len(r._records(r._get(run.sections['map'],'scenarios')) or [])
    counts=[('Этапы',len(run.stages)),('Операции',operation_count),('Сценарии',scenario_count)]
    origin=' · Демо' if r._get(run.manifest,'data_origin')=='synthetic' else ''
    head='<header class="app-head"><div><p class="eyebrow">Карта процесса'+origin+'</p><h1>'+r._h(r._get(run.sections['map'],'purpose'),'Процесс пока не определён')+'</h1></div><div class="head-counts">'+''.join(f'<div><span>{label}</span><b>{n}</b></div>' for label,n in counts)+'</div></header>'
    sidebar=''
    tabs=(('map','Сценарии'),('coverage','Логирование'),('instrumentation','Доработки'),('metrics','Метрики'),('evidence','Источники'),('scope','О запуске'))
    nav='<nav class="tabs">'+''.join(f'<a class="tab" href="#{key}">{label}</a>' for key,label in tabs)+'</nav>'
    renderers={'map':r._hmap,'coverage':r._hcoverage,'instrumentation':r._hplan,'metrics':r._hmetrics}
    panes=[]
    for key,_,title in r._SECTIONS:
        artifact=run.sections[key]
        content=renderers[key](run,artifact) if artifact is not None else r._hmissing(r._NO_ARTIFACT)
        blockers=r._hblockers(artifact.get('blockers')) if key!='metrics' and artifact and artifact.get('blockers') else ''
        if key == 'instrumentation' and blockers:
            blockers = '<p>Подготовка доработок не завершена.</p><details><summary>Причина остановки</summary>' + blockers + '</details>'
        display_title = '2. Существующие логи процесса' if key == 'coverage' else title
        section=f'<section id="{key}" data-pane><h2>{display_title}</h2>'+blockers+content+'</section>'
        panes.append(section)
    evidence=r._hevidence(run).replace('<section id="evidence">','<section id="evidence" data-pane>')
    evidence=evidence.removesuffix('</section>')+r._hreport_sources(run)+_inventory(bundle)+'</section>'
    if run.insights:evidence=evidence.removesuffix('</section>')+r._hinsights(run)+'</section>'
    provenance=r._hdl([('Запуск',r._hcode(run.run_id)),('Данные',r._hbadge(r._get(run.manifest,'data_origin'))),('Модель',r._hbadge(r._get(run.manifest,'model_mode')))])
    if run.manifest is None:provenance+=r._hmissing(r._NO_MANIFEST)
    if run.scope is None:provenance+=r._hmissing(r._NO_SCOPE)
    provenance+='<details><summary>Модели</summary>'+r._hvalue(r._get(bundle.get('diagnostics'),'model'))+'</details>'
    provenance+='<details><summary>Параметры и область анализа</summary>'+r._hvalue(_scope_details(run.scope))+r._hvalue(run.manifest)+'</details>'
    panes.extend([evidence,'<section id="scope" data-pane><h2>О запуске</h2>'+provenance+'<details><summary>Загруженные знания</summary>'+r._hvalue(bundle.get('guidance'))+'</details>'+font_license()+'</section>'])
    return ('<!DOCTYPE html>\n<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            +f'<meta http-equiv="Content-Security-Policy" content="{csp}"><meta name="referrer" content="no-referrer">'
            +'<title>Наблюдаемость процесса — '+html.escape(run.run_id or r._UNKNOWN)+'</title><style>'+r._CSS+CSS+'</style></head>'
            +'<body><div class="app">'+head+'<div class="workspace">'+sidebar+'<div class="content">'+nav+'<div class="panes">'
            +''.join(panes)+'</div></div></div></div><script>'+SCRIPT+'</script></body></html>\n')
