"""Expandable business blocks on the operation diagram, using saved compose groups.

Text is wrapped with the existing embedded font metrics. Browser geometry uses
finite private ports/tracks, with no model calls or exponential state cache.
The static map remains a no-JavaScript fallback. This is navigation, not a trace.
"""
import html
import json

from . import scenario_layout as L
from .operation_hierarchy import hierarchy_view


ISSUE_LABELS = {
    'invalid_group': 'Некорректный состав блока', 'unbound_group': 'Нет принятого основания композиции',
    'unknown_stage': 'Этап не установлен', 'duplicate_group_name': 'Повтор названия блока',
    'unknown_operation': 'Дочерняя операция не найдена', 'group_stage_mismatch': 'Операции относятся к разным этапам',
    'single_member_not_a_block': 'Одна операция показана отдельно', 'overlapping_groups': 'Состав блоков пересекается',
    'unresolved_endpoint': 'Конец перехода не установлен',
}


def issue_text(item):
    return (html.escape(str(item.get('group_id') or item.get('transition_id') or 'Без ID')) + ': '
            + html.escape(ISSUE_LABELS.get(item['code'], item['code'])))


def mount(run):
    bundle = run.bundle
    view = hierarchy_view(bundle)
    if not view['groups']:
        if view['issues']:
            return ('<p class="note">Группировка блоков требует уточнения. Все операции показаны отдельно.</p>'
                    '<details class="technical"><summary>Вопросы группировки</summary><ul>'
                    + ''.join('<li>' + issue_text(x) + '</li>' for x in view['issues']) + '</ul></details>')
        return ('<p class="note">В этом сохранённом результате нет групп дочерних операций для раскрытия. '
                'Схема показывает имеющиеся действия; разбивка по названию не выдумывается.</p>')
    from .scenario_graph import operation_status

    def measured(value, size, line_height, limit):
        text = L.wrap(value or 'Без названия', size, line_height, limit)
        return {'lines': text.lines, 'w': text.width, 'h': text.height}

    operations = [x for x in ((run.sections['map'] or {}).get('operations') or []) if isinstance(x, dict)]
    payload = {
        'groups': view['groups'], 'issues': view['issues'],
        'stages': [{'id': x.get('id'), 'name': x.get('name'),
                    'text': measured(x.get('name'), 15, 20, 220)} for x in run.stages],
        'operations': [{'id': x.get('id'), 'name': x.get('name'), 'stage': x.get('stage_id'),
                        'kind': x.get('kind'), 'anchor': f'op-{i}', 'status': operation_status(run, x),
                        'text': measured(x.get('name'), 14, 18, 180)} for i, x in enumerate(operations, 1)],
        'edges': hierarchy_view(bundle, [x['id'] for x in view['groups']])['edges'],
        'scenarios': [x for x in ((run.sections['map'] or {}).get('scenarios') or []) if isinstance(x, dict)],
    }
    for number, group in enumerate(payload['groups'], 1):
        group['text'] = measured(group['name'], 14, 18, 180)
        group['anchor'] = f'group-{number}'
    for edge in payload['edges']:
        edge['text'] = measured(edge.get('condition'), 12, 16, 156) if edge.get('condition') else None
    data = html.escape(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), quote=True)
    issues = ('<details class="technical"><summary>Вопросы группировки · ' + str(len(view['issues'])) + '</summary><ul>'
              + ''.join('<li>' + issue_text(x) + '</li>' for x in view['issues']) + '</ul></details>'
              if view['issues'] else '')
    return f'<div class="hierarchy-diagram" data-hierarchy="{data}"></div>' + issues


SCRIPT = r"""
(()=>{'use strict';
const NS=document.querySelector('.map svg')?.namespaceURI;
if(!NS)return;
const kinds={start:'Начало',operation:'Действие',decision:'Проверка',end:'Результат',group:'Бизнес-блок'};
const coverage={code_logs:['Найдены связанные логи','#edf6ef','#659b76'],
 code_only:['Связанные логи не подтверждены','#fff3e7','#cd9d67'],
 unlinked:['Нет привязки к коду','#f5f7f8','#aebbc3'],unknown:['Нужна проверка','#fff','#b7c2bf'],
 mixed:['Связанные логи найдены у части операций','#fff5ce','#b29a40'],
 mixed_unconfirmed:['Состояния различаются; связи с логами не подтверждены','#f5f7f8','#aebbc3']};
function svg(tag,attrs={},text){const e=document.createElementNS(NS,tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,String(v));if(text!==undefined)e.textContent=text;return e;}
function html(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;}
function label(parent,text,x,y,size,lineHeight,cls){const e=svg('text',{x,y,'font-size':size,class:cls||'', 'text-anchor':'middle'});text.lines.forEach((line,i)=>e.appendChild(svg('tspan',{x,y:y+i*lineHeight},line)));parent.appendChild(e);return e;}
document.querySelectorAll('.hierarchy-diagram').forEach(mount=>{
 let data;try{data=JSON.parse(mount.dataset.hierarchy);}catch{return;}
 const ops=new Map(data.operations.map(x=>[x.id,x])),groups=new Map(data.groups.map(x=>[x.id,x]));
 const owner=new Map();data.groups.forEach(g=>g.operation_ids.forEach(id=>owner.set(id,g.id)));
 let expanded=new Set(),scenario='all',zoom=1,search='';
 const controls=html('div','hierarchy-controls'),filters=html('div','scenario-chips');
 const all=html('button','hierarchy-scenario active','Все сценарии');all.type='button';all.dataset.scenario='all';all.setAttribute('aria-pressed','true');filters.appendChild(all);
 data.scenarios.forEach((s,i)=>{const b=html('button','hierarchy-scenario',s.name);b.type='button';b.dataset.scenario=String(i);b.setAttribute('aria-pressed','false');filters.appendChild(b);});
 const assignedOps=new Set(data.scenarios.flatMap(s=>s.operation_ids||[])),assignedEdges=new Set(data.scenarios.flatMap(s=>s.transition_ids||[]));
 const unassignedOps=data.operations.filter(op=>!assignedOps.has(op.id)).map(op=>op.id),unassignedEdges=data.edges.filter(e=>!e.transition_ids.some(id=>assignedEdges.has(id))).flatMap(e=>e.transition_ids);
 if(unassignedOps.length){const b=html('button','hierarchy-scenario','Вне сценариев · '+unassignedOps.length);b.type='button';b.dataset.scenario='unassigned';b.setAttribute('aria-pressed','false');filters.appendChild(b);}
 filters.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;scenario=b.dataset.scenario;draw();});
 const tools=html('div','map-tools'),input=html('input');input.type='search';input.placeholder='Найти блок или операцию';input.setAttribute('aria-label',input.placeholder);
 input.addEventListener('input',()=>{search=input.value.toLocaleLowerCase();paintSearch();});tools.appendChild(input);
 const zooms=html('div','zoom-controls'),readout=html('output','hierarchy-zoom','100%');
 for(const [name,delta] of [['−',-.15],['+',.15],['Вписать',0]]){const b=html('button','',name);b.type='button';b.setAttribute('aria-label',name==='−'?'Уменьшить':name==='+'?'Увеличить':'Вписать');b.addEventListener('click',()=>{zoom=delta?Math.min(1.8,Math.max(.25,zoom+delta)):Math.min(1,(scroll.clientWidth-10)/Number(canvas.dataset.w));resize();});zooms.appendChild(b);if(delta<0)zooms.appendChild(readout);}tools.appendChild(zooms);
 const legend=html('div','coverage-legend');for(const [name,fill,border] of Object.values(coverage)){const item=html('span'),swatch=html('span','coverage-swatch');swatch.style.background=fill;swatch.style.borderColor=border;item.append(swatch,document.createTextNode(name));legend.appendChild(item);}
 controls.append(filters,tools,legend);
 const scroll=html('div','hierarchy-scroll'),canvas=svg('svg',{role:'group','aria-label':'Переходы операций с раскрывающимися бизнес-блоками'});scroll.appendChild(canvas);
 const note=html('p','graph-caption','Нажмите на бизнес-блок, чтобы раскрыть операции на этой схеме. Группы предложены моделью при композиции и служат навигации. Условия относятся к стрелкам и открывают пояснения. Цвет показывает связи с логированием в исходниках, не выполнение или доставку.');
 mount.append(controls,scroll,note);
 function aggregate(ids){const states=ids.map(id=>ops.get(id)?.status||'unknown');return states.every(x=>x===states[0])?states[0]:states.includes('code_logs')?'mixed':'mixed_unconfirmed';}
 function resize(){canvas.setAttribute('width',Math.ceil(Number(canvas.dataset.w)*zoom));canvas.setAttribute('height',Math.ceil(Number(canvas.dataset.h)*zoom));readout.textContent=Math.round(zoom*100)+'%';}
 function syncLevels(){const allOpen=expanded.size===groups.size,none=!expanded.size;document.querySelectorAll('.detail-level').forEach(b=>{const on=b.dataset.detailLevel===(allOpen?'operations':none?'stages':'mixed');b.classList.toggle('active',on);b.setAttribute('aria-pressed',String(on));});}
 function toggle(id){if(expanded.has(id))expanded.delete(id);else expanded.add(id);draw();syncLevels();canvas.querySelector(`[data-toggle-group="${CSS.escape(id)}"]`)?.focus();}
 function paintSearch(){canvas.querySelectorAll('[data-search]').forEach(n=>{n.classList.toggle('search-dimmed',!!search&&!n.dataset.search.includes(search));});}
 function paintPath(hash=location.hash){
  const op=data.operations.find(x=>'#'+x.anchor===hash);canvas.querySelectorAll('.hierarchy-node').forEach(x=>x.classList.toggle('selected',!!hash&&x.dataset.op===hash.slice(1)));canvas.querySelectorAll('.path-on,.path-dim').forEach(x=>x.classList.remove('path-on','path-dim'));if(!op)return;
  const reach=reverse=>{const seen=new Set([op.id]),todo=[op.id];while(todo.length){const id=todo.pop();data.edges.forEach(e=>{const from=reverse?e.to_operation:e.from_operation,to=reverse?e.from_operation:e.to_operation;if(from===id&&!seen.has(to)){seen.add(to);todo.push(to);}});}return seen;};
  const up=reach(true),down=reach(false),path=new Set([...up,...down]);
  canvas.querySelectorAll('.hierarchy-node').forEach(n=>{const ids=n.dataset.nodeId.startsWith('group:')?groups.get(n.dataset.nodeId.slice(6)).operation_ids:[n.dataset.nodeId.slice(3)];n.classList.add(ids.some(x=>path.has(x))?'path-on':'path-dim');});
  canvas.querySelectorAll('.hierarchy-edge').forEach(n=>{const raw=data.edges.find(x=>x.anchor===n.dataset.anchor);const on=raw&&((up.has(raw.from_operation)&&up.has(raw.to_operation))||(down.has(raw.from_operation)&&down.has(raw.to_operation)));n.classList.add(on?'path-on':'path-dim');});
 }
 function revealHash(hash){const op=data.operations.find(x=>'#'+x.anchor===hash),edge=data.edges.find(x=>(x.anchors||[x.anchor]).some(a=>'#'+a===hash));const ids=op?[op.id]:edge?[edge.from_operation,edge.to_operation]:[];let changed=false;ids.forEach(id=>{const g=owner.get(id);if(g&&!expanded.has(g)){expanded.add(g);changed=true;}});if(ids.length){document.querySelectorAll('[data-level-view]').forEach(v=>{v.hidden=!v.dataset.levelView.split(' ').includes('operations');});syncLevels();}if(changed){const focus=document.activeElement?.closest('.hierarchy-edge');draw();if(focus&&edge)canvas.querySelector('[data-anchor="'+CSS.escape(edge.anchor)+'"]')?.focus();}paintPath(hash);}
 function draw(){
  const selected=scenario==='all'?null:scenario==='unassigned'?{operation_ids:unassignedOps,transition_ids:unassignedEdges}:data.scenarios[Number(scenario)],selectedOps=new Set(selected?.operation_ids||[]),selectedEdges=new Set(selected?.transition_ids||[]);
  filters.querySelectorAll('button').forEach(b=>{const on=b.dataset.scenario===scenario;b.classList.toggle('active',on);b.setAttribute('aria-pressed',String(on));});
  const visible=new Map(),nodes=[],byKey=new Map(),seen=new Set();
  data.operations.forEach(op=>{const group=owner.get(op.id),folded=group&&!expanded.has(group),key=folded?'group:'+group:'op:'+op.id;visible.set(op.id,key);if(seen.has(key))return;seen.add(key);
   const g=folded?groups.get(group):null,n={key,group:group||null,kind:g?'group':op.kind,name:g?g.name:op.name,text:g?g.text:op.text,stage:op.stage,ids:g?g.operation_ids:[op.id],anchor:g?null:op.anchor,status:g?aggregate(g.operation_ids):op.status,out:[],inc:[]};nodes.push(n);byKey.set(key,n);});
  const internal=new Map(data.groups.map(g=>[g.id,0])),edges=[];
  data.edges.forEach(raw=>{const from=visible.get(raw.from_operation),to=visible.get(raw.to_operation);if(from===to&&from?.startsWith('group:')){internal.set(from.slice(6),(internal.get(from.slice(6))||0)+raw.transition_ids.length);return;}if(!byKey.has(from)||!byKey.has(to))return;
   const e={...raw,from,to,index:edges.length,active:!selected||raw.transition_ids.some(x=>selectedEdges.has(x))};edges.push(e);byKey.get(from).out.push(e);byKey.get(to).inc.push(e);});
  const stages=data.stages.map(x=>({...x,nodes:[]}));if(nodes.some(n=>!stages.some(s=>s.id===n.stage)))stages.push({id:null,name:'Этап не указан',text:{lines:['Этап не указан'],h:20,w:140},nodes:[]});
  nodes.forEach(n=>stages.find(s=>s.id===n.stage||s.id===null)?.nodes.push(n));
  let cursor=20,maxBottom=0;const clusters=[];
  stages.forEach(stage=>{
   // Group members are placed together for navigation only; every drawn arrow still comes from an input edge.
   const ordered=[],scheduled=new Set();stage.nodes.forEach(n=>{if(scheduled.has(n.key))return;const members=n.group&&expanded.has(n.group)?stage.nodes.filter(x=>x.group===n.group):[n];members.forEach(x=>{if(!scheduled.has(x.key)){ordered.push(x);scheduled.add(x.key);}});});stage.nodes=ordered;
   const ins=stage.nodes.flatMap(n=>n.inc),outs=stage.nodes.flatMap(n=>n.out);const maxLabel=Math.max(0,...outs.map(e=>e.text?Math.ceil(e.text.w)+14:0));
   stage.maxLabel=maxLabel;
   const left=30+ins.length*10,right=30+outs.length*10+maxLabel;stage.x=cursor;stage.w=left+220+right;stage.nodeX=cursor+left;
   let y=stage.text.h+66;const openClusters=new Map();
   stage.nodes.forEach(n=>{
    if(n.group&&expanded.has(n.group)&&!openClusters.has(n.group)){const group=groups.get(n.group),c={id:n.group,x:stage.nodeX-12,y,w:244+maxLabel,bottom:y,name:group.name,text:group.text,headH:group.text.h+50,status:aggregate(group.operation_ids),logged:group.operation_ids.filter(id=>ops.get(id)?.status==='code_logs').length,total:group.operation_ids.length};openClusters.set(n.group,c);clusters.push(c);y+=c.headH+12;}
    y+=n.inc.length*8;n.x=stage.nodeX;n.y=y;n.labelW=maxLabel;
    let outgoing=0;n.out.forEach(e=>{const h=e.text?e.text.h+24:24;e.sy=y+20+outgoing+h/2;e.labelH=h-8;outgoing+=h+8;});n.inc.forEach((e,i)=>{e.ty=y+24+i*18;});
    n.h=Math.max(n.text.h+64,outgoing+40,n.inc.length*18+40);y+=n.h+26;
    if(n.group&&expanded.has(n.group)){const c=openClusters.get(n.group);c.bottom=y-8;const at=stage.nodes.indexOf(n);if(stage.nodes[at+1]?.group!==n.group)y+=16;}
   });
   ins.forEach((e,i)=>{e.tx=stage.nodeX-18-i*10;});outs.forEach((e,i)=>{e.sx=stage.nodeX+220+maxLabel+18+i*10;});stage.bottom=Math.max(y,stage.text.h+160);maxBottom=Math.max(maxBottom,stage.bottom);cursor+=stage.w+24;
  });
  // An existing unlabelled, unbranched edge between neighbouring boxes needs
  // no side detour. Never cut across an expanded block's header or a third node.
  edges.forEach(e=>{const a=byKey.get(e.from),b=byKey.get(e.to),top=a.y+a.h,bottom=b.y;
   e.direct=!e.text&&a.stage===b.stage&&a.out.length===1&&b.inc.length===1&&top<bottom
    &&!nodes.some(n=>n!==a&&n!==b&&n.stage===a.stage&&n.y<bottom&&n.y+n.h>top)
    &&!clusters.some(c=>groups.get(c.id).stage_id===a.stage&&c.y<bottom&&c.y+c.headH>top);
  });
  // A lane belongs to a vertical interval, not permanently to one edge.
  // Disjoint local steps can reuse it. Cross/back edges conservatively keep a
  // lane to the bottom: no shared segment that would imply an invented branch.
  function laneCount(items,side){
   const intervals=items.filter(e=>!e.direct).map(e=>{const a=byKey.get(e.from),b=byKey.get(e.to),candidate=b.y-8-b.inc.indexOf(e)*8,local=a.stage===b.stage&&a.y<b.y;
    const end=local?candidate:maxBottom+20+edges.length*16,start=side==='out'?e.sy:e.ty;return{e,lo:Math.min(start,candidate)-8,hi:Math.max(start,candidate,end)+8};}).sort((a,b)=>a.lo-b.lo||a.hi-b.hi||a.e.index-b.e.index);
   const until=[];for(const item of intervals){let lane=until.findIndex(y=>y<item.lo);if(lane<0)lane=until.length;until[lane]=item.hi;item.e[side+'Lane']=lane;}
   return until.length;
  }
  cursor=20;
  stages.forEach(stage=>{
   const ins=stage.nodes.flatMap(n=>n.inc),outs=stage.nodes.flatMap(n=>n.out),left=30+laneCount(ins,'in')*10,right=30+laneCount(outs,'out')*10+stage.maxLabel;
   stage.x=cursor;stage.w=left+220+right;stage.nodeX=cursor+left;stage.nodes.forEach(n=>n.x=stage.nodeX);
   clusters.filter(c=>groups.get(c.id).stage_id===stage.id).forEach(c=>c.x=stage.nodeX-12);
   ins.forEach(e=>e.tx=stage.nodeX-18-(e.inLane||0)*10);outs.forEach(e=>e.sx=stage.nodeX+220+stage.maxLabel+18+(e.outLane||0)*10);
   cursor+=stage.w+24;
  });
  // Reserve every local corridor before any cross-stage jump. A later local
  // route must not silently overwrite an earlier long arrow at the same y.
  const corridors=[];let returnTracks=0;
  edges.filter(e=>!e.direct).forEach(e=>{const a=byKey.get(e.from),b=byKey.get(e.to);corridors.push({x0:a.x+220,x1:e.sx,y:e.sy},{x0:e.tx,x1:b.x,y:e.ty});});
  edges.filter(e=>!e.direct).forEach(e=>{const a=byKey.get(e.from),b=byKey.get(e.to);e.local=a.stage===b.stage&&a.y<b.y;e.candidate=b.y-8-b.inc.indexOf(e)*8;if(e.local)corridors.push({x0:Math.min(e.sx,e.tx),x1:Math.max(e.sx,e.tx),y:e.candidate});});
  edges.forEach(e=>{
   if(e.direct)return;
   const b=byKey.get(e.to),candidate=e.candidate,x0=Math.min(e.sx,e.tx),x1=Math.max(e.sx,e.tx);
   e.clear=!e.local&&edges.length<=500&&candidate>0&&!nodes.some(n=>n.key!==b.key&&candidate>=n.y-4&&candidate<=n.y+n.h+4&&n.x<=x1&&n.x+220>=x0)
    &&!stages.some(s=>s.x<x1&&s.x+s.w>x0&&candidate<s.text.h+54)
    &&!edges.some(other=>{if(!other.text)return false;const n=byKey.get(other.from),lx=n.x+228,w=Math.ceil(other.text.w)+14,h=other.text.h+20;return lx<x1&&lx+w>x0&&candidate>other.sy-h/2-4&&candidate<other.sy+h/2+4;})
    &&!clusters.some(c=>c.x<x1&&c.x+c.w>x0&&candidate>c.y-4&&candidate<c.y+c.headH+8)
    &&!edges.some(other=>{if(!other.direct)return false;const a=byKey.get(other.from),b=byKey.get(other.to),x=a.x+110;return x>x0-4&&x<x1+4&&candidate>a.y+a.h-4&&candidate<b.y+4;})
    &&!corridors.some(c=>Math.abs(c.y-candidate)<6&&c.x0<x1&&c.x1>x0);
   if(e.local||e.clear){e.routeY=candidate;if(e.clear)corridors.push({x0,x1,y:candidate});}
   else{e.routeY=maxBottom+20+returnTracks*16;returnTracks++;}
  });
  const width=Math.max(cursor,350),height=maxBottom+returnTracks*16+50;canvas.replaceChildren();canvas.dataset.w=width;canvas.dataset.h=height;canvas.setAttribute('viewBox',`0 0 ${width} ${height}`);
  const defs=svg('defs'),marker=svg('marker',{id:'hierarchy-arrow',viewBox:'0 0 10 10',refX:8,refY:5,markerWidth:6,markerHeight:6,orient:'auto'});marker.appendChild(svg('path',{d:'M1 1 L9 5 L1 9',fill:'none',stroke:'#8a8798','stroke-width':1.5}));defs.appendChild(marker);canvas.appendChild(defs);
  stages.forEach(s=>{const g=svg('g',{'data-hierarchy-stage':s.id||''});g.appendChild(svg('rect',{x:s.x,y:16,width:s.w,height:maxBottom-4,rx:16,fill:'#f6faf7',stroke:'#dce7df'}));label(g,s.text,s.x+s.w/2,44,15,20,'hierarchy-stage-name');if(!s.nodes.length)g.appendChild(svg('text',{x:s.x+20,y:100,'font-size':12},'Операции не установлены'));canvas.appendChild(g);});
  clusters.forEach(c=>{const colors=coverage[c.status]||coverage.unknown,g=svg('g',{'data-expanded-group':c.id,'data-coverage':c.status});g.appendChild(svg('rect',{x:c.x,y:c.y,width:c.w,height:c.bottom-c.y,rx:10,fill:'#fff',stroke:colors[2],'stroke-dasharray':'5 3'}));const b=svg('g',{role:'button',tabindex:0,'aria-label':'Свернуть блок '+c.name,'aria-expanded':'true','data-toggle-group':c.id});b.appendChild(svg('rect',{x:c.x+4,y:c.y+3,width:c.w-8,height:c.headH,rx:7,fill:colors[1]}));b.appendChild(svg('text',{x:c.x+c.w/2,y:c.y+16,'text-anchor':'middle','font-size':9},'Бизнес-блок · свернуть'));label(b,c.text,c.x+c.w/2,c.y+35,14,18,'hierarchy-node-name');b.appendChild(svg('text',{x:c.x+c.w/2,y:c.y+c.headH-8,'text-anchor':'middle','font-size':10},`С подтверждённой связью логов: ${c.logged}/${c.total}`));b.appendChild(svg('title',{},c.name));g.appendChild(b);canvas.appendChild(g);});
  edges.forEach(e=>{
   const a=byKey.get(e.from),b=byKey.get(e.to),y=e.routeY;
   const d=e.direct?`M ${a.x+110} ${a.y+a.h} L ${b.x+110} ${b.y}`:`M ${a.x+220} ${e.sy} L ${e.sx} ${e.sy} L ${e.sx} ${y} L ${e.tx} ${y} L ${e.tx} ${e.ty} L ${b.x} ${e.ty}`;
   const endpoint=(id,node)=>(ops.get(id)?.name||'Операция без названия')+(node.kind==='group'?' (в блоке '+node.name+')':'');
   const link=svg('a',{href:'#'+e.anchor,class:'hierarchy-edge','data-routing':e.direct?'direct_neighbour':e.local?'local_forward':e.clear?'clear_corridor':'return_track','data-anchor':e.anchor,'data-from-node':e.from,'data-to-node':e.to,'data-from-operation':e.from_operation,'data-to-operation':e.to_operation,'data-transition-ids':e.transition_ids.join(' '),'data-active':e.active?'1':'0'});link.appendChild(svg('title',{},`${endpoint(e.from_operation,a)} → ${endpoint(e.to_operation,b)}${e.condition?'; условие: '+e.condition:'; условие не установлено'}`));
   link.appendChild(svg('path',{d,fill:'none',stroke:'#fff','stroke-width':5}));link.appendChild(svg('path',{class:'hierarchy-edge-line',d,fill:'none',stroke:e.active?'#9682ac':'#cbd1d5','stroke-width':1.5,'marker-end':'url(#hierarchy-arrow)'}));
   if(e.text){const x=a.x+228,w=Math.ceil(e.text.w)+14,h=e.text.h+20,top=e.sy-h/2;const cap=svg('g',{class:'hierarchy-condition'});cap.appendChild(svg('rect',{x,y:top,width:w,height:h,rx:3,fill:'#fbfaf4',stroke:'#c8c0aa','stroke-dasharray':'3 2'}));cap.appendChild(svg('text',{x:x+w/2,y:top+10,'text-anchor':'middle','font-size':8,fill:'#816b98'},'Условие'));label(cap,e.text,x+w/2,top+24,12,16,'hierarchy-condition-text');link.appendChild(cap);}canvas.appendChild(link);
  });
  nodes.forEach(n=>{
   const active=!selected||n.ids.some(x=>selectedOps.has(x)),[description,fill,border]=coverage[n.status]||coverage.unknown;
   const g=svg('g',{class:'hierarchy-node'+(n.anchor?' operation-node hierarchy-operation':''),'data-node-id':n.key,'data-op':n.anchor||'','data-coverage':n.status,'data-search':(n.name+' '+n.ids.map(x=>ops.get(x)?.name||'').join(' ')).toLocaleLowerCase(),'data-active':active?'1':'0'});
   g.appendChild(svg('rect',{class:'op-shape',x:n.x,y:n.y,width:220,height:n.h,rx:n.kind==='decision'?3:11,fill:active?fill:'#f3f5f6',stroke:border,'stroke-width':1.5}));
   g.appendChild(svg('text',{x:n.x+110,y:n.y+16,'text-anchor':'middle','font-size':9,fill:'#7851b8'},kinds[n.kind]||'Узел'));label(g,n.text,n.x+110,n.y+38,14,18,'hierarchy-node-name');
   g.appendChild(svg('title',{},n.name+' · '+description));
   if(n.kind==='group'){g.setAttribute('role','button');g.setAttribute('tabindex','0');g.setAttribute('aria-expanded','false');g.setAttribute('aria-label','Раскрыть блок '+n.name+'; операций: '+n.ids.length);g.dataset.toggleGroup=n.group;const logged=n.ids.filter(id=>ops.get(id)?.status==='code_logs').length;g.appendChild(svg('text',{x:n.x+110,y:n.y+n.h-26,'text-anchor':'middle','font-size':9},`С логами: ${logged}/${n.ids.length} · связей внутри: ${internal.get(n.group)||0}`));g.appendChild(svg('text',{x:n.x+110,y:n.y+n.h-10,'text-anchor':'middle','font-size':10},'Нажмите, чтобы раскрыть'));const info=svg('a',{class:'hierarchy-info',href:'#'+groups.get(n.group).anchor,'aria-label':'Открыть карточку блока '+n.name});info.appendChild(svg('text',{x:n.x+205,y:n.y+16,'font-size':12},'ⓘ'));canvas.appendChild(g);canvas.appendChild(info);}
   else{const short={code_logs:'Связанный лог найден',code_only:'Связь с логом не подтверждена',unlinked:'Нет привязки к коду',unknown:'Нужна проверка'};g.appendChild(svg('text',{x:n.x+110,y:n.y+n.h-12,'text-anchor':'middle','font-size':10},short[n.status]||'Нужна проверка'));const a=svg('a',{href:'#'+n.anchor,'aria-label':(kinds[n.kind]||'Операция')+': '+n.name+' · '+description});a.appendChild(g);canvas.appendChild(a);}
  });
  resize();paintSearch();paintPath();
 }
 canvas.addEventListener('click',e=>{if(e.target.closest('a[href]'))return;const target=e.target.closest('[data-toggle-group]');if(target){e.preventDefault();toggle(target.dataset.toggleGroup);}});
 canvas.addEventListener('keydown',e=>{if(e.target.closest('a[href]'))return;const target=e.target.closest('[data-toggle-group]');if(target&&(e.key==='Enter'||e.key===' ')){e.preventDefault();toggle(target.dataset.toggleGroup);}});
 window.runHierarchyLayoutCheck=()=>{
  const original=new Set(expanded),originalScenario=scenario;
  const family=getComputedStyle(canvas.querySelector('text')).fontFamily.split(',')[0].replace(/["']/g,'').trim();
  const result={ok:true,hierarchy:true,font:{family,check:document.fonts.check('14px "'+family+'"'),loaded:[...document.fonts].some(f=>f.family.replace(/["']/g,'')===family&&f.status==='loaded')},views:[]};
  const rect=e=>{const b=e.getBBox();return{x:b.x,y:b.y,w:b.width,h:b.height};},overlap=(a,b)=>a.x<b.x+b.w-.5&&b.x<a.x+a.w-.5&&a.y<b.y+b.h-.5&&b.y<a.y+a.h-.5;
  const inside=(a,b)=>a.x>=b.x-.8&&a.y>=b.y-.8&&a.x+a.w<=b.x+b.w+.8&&a.y+a.h<=b.y+b.h+.8;
  const hits=(s,b)=>{const[x1,y1,x2,y2]=s;return y1===y2?y1>b.y+.6&&y1<b.y+b.h-.6&&Math.min(x1,x2)<b.x+b.w-.6&&Math.max(x1,x2)>b.x+.6:x1===x2?x1>b.x+.6&&x1<b.x+b.w-.6&&Math.min(y1,y2)<b.y+b.h-.6&&Math.max(y1,y2)>b.y+.6:false;};
  const states=[['closed',[]],...data.groups.map(g=>[g.id,[g.id]]),['all',[...groups.keys()]]];
  try{for(const[view,ids]of states){
   expanded=new Set(ids);draw();const problems=[],bad=(type,id)=>problems.push(type+': '+id);
   const nodes=[...canvas.querySelectorAll('.hierarchy-node')].map(n=>({id:n.dataset.nodeId,b:rect(n.querySelector('rect'))}));
   const labels=[...canvas.querySelectorAll('.hierarchy-condition')].map(g=>({id:g.closest('a').dataset.anchor,b:rect(g.querySelector('rect'))}));
   canvas.querySelectorAll('.hierarchy-node,.hierarchy-condition,[data-expanded-group] [role="button"]').forEach(n=>{const b=rect(n.querySelector('rect'));n.querySelectorAll('text').forEach(t=>{if(!inside(rect(t),b))bad('text-outside',t.textContent);});});
   const boxes=[...nodes,...labels];boxes.forEach((n,i)=>boxes.slice(i+1).forEach(m=>{if(overlap(n.b,m.b))bad('overlap',n.id+'/'+m.id);}));
   const edges=[...canvas.querySelectorAll('.hierarchy-edge')].map(e=>{const v=(e.querySelector('.hierarchy-edge-line').getAttribute('d').match(/-?\d+(?:\.\d+)?/g)||[]).map(Number),segs=[];for(let i=2;i+1<v.length;i+=2)segs.push([v[i-2],v[i-1],v[i],v[i+1]]);return{id:e.dataset.anchor,segs};});
   const heads=[...canvas.querySelectorAll('.hierarchy-stage-name')].map(rect);
   edges.forEach((e,i)=>{
    nodes.forEach(n=>{if(e.segs.some(s=>hits(s,n.b)))bad('edge-through-node',e.id+'/'+n.id);});
    labels.forEach(l=>{if(l.id!==e.id&&e.segs.some(s=>hits(s,l.b)))bad('edge-through-label',e.id+'/'+l.id);});
    if(heads.some(h=>e.segs.some(s=>hits(s,h))))bad('edge-through-header',e.id);
    if(canvas.querySelector('[data-anchor="'+CSS.escape(e.id)+'"]')?.dataset.routing==='direct_neighbour'){
     const s=e.segs[0],gap={x:s[0]-3,y:Math.min(s[1],s[3])-4,w:6,h:Math.abs(s[3]-s[1])+8};
     if(edges.some(other=>other!==e&&other.segs.some(line=>hits(line,gap))))bad('edge-through-direct-gap',e.id);
    }
    edges.slice(i+1).forEach(other=>{
     const collinear=(a,b)=>{
      if(a[1]===a[3]&&b[1]===b[3])return Math.abs(a[1]-b[1])<.6&&Math.min(Math.max(a[0],a[2]),Math.max(b[0],b[2]))-Math.max(Math.min(a[0],a[2]),Math.min(b[0],b[2]))>1;
      if(a[0]===a[2]&&b[0]===b[2])return Math.abs(a[0]-b[0])<.6&&Math.min(Math.max(a[1],a[3]),Math.max(b[1],b[3]))-Math.max(Math.min(a[1],a[3]),Math.min(b[1],b[3]))>1;
      return false;
     };
     if(e.segs.some(a=>other.segs.some(b=>collinear(a,b))))bad('collinear-arrows',e.id+'/'+other.id);
     const points=segs=>segs.flatMap(s=>[[s[0],s[1]],[s[2],s[3]]]);
     const on=(p,s)=>(s[0]===s[2]?Math.abs(p[0]-s[0])<.6&&p[1]>=Math.min(s[1],s[3])-.6&&p[1]<=Math.max(s[1],s[3])+.6:s[1]===s[3]&&Math.abs(p[1]-s[1])<.6&&p[0]>=Math.min(s[0],s[2])-.6&&p[0]<=Math.max(s[0],s[2])+.6);
     if(points(e.segs).some(p=>other.segs.some(s=>on(p,s)))||points(other.segs).some(p=>e.segs.some(s=>on(p,s))))bad('ambiguous-junction',e.id+'/'+other.id);
    });
   });
   result.views.push({view,scenario,problems,nodes:nodes.length,edges:edges.length});if(problems.length)result.ok=false;
  }}finally{expanded=original;scenario=originalScenario;draw();}
  if(!result.font.loaded||!result.font.check)result.ok=false;
  return result;
 };
 document.addEventListener('detail-level-change',e=>{if(e.detail==='process')return;expanded=e.detail==='operations'?new Set(groups.keys()):new Set();draw();});
 document.addEventListener('click',e=>{const a=e.target.closest('a[href^="#"]');if(a)revealHash(a.getAttribute('href'));});window.addEventListener('hashchange',()=>revealHash(location.hash));
 let pan=null;scroll.addEventListener('pointerdown',e=>{if(e.target.closest('a,[data-toggle-group],button'))return;pan={x:e.clientX,y:e.clientY,l:scroll.scrollLeft,t:scroll.scrollTop};scroll.setPointerCapture(e.pointerId);});scroll.addEventListener('pointermove',e=>{if(pan){scroll.scrollLeft=pan.l+pan.x-e.clientX;scroll.scrollTop=pan.t+pan.y-e.clientY;}});scroll.addEventListener('pointerup',()=>pan=null);scroll.addEventListener('pointercancel',()=>pan=null);
 try{draw();revealHash(location.hash);const fallback=mount.parentElement.querySelector('.static-operation-graph');if(fallback)fallback.hidden=true;}catch(error){mount.hidden=true;console.error('Hierarchy render failed',error);}
});
})();
"""
