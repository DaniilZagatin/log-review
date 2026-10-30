/* Optional browser acceptance check. Requires Playwright and installed Edge.
 * Reads a generated HTML map; uses a fresh offline browser without source execution.
 * Usage: node scripts/check_scenario_map_ui.cjs report.html [screenshot-folder]
 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');

// The stage menu is a custom listbox over a hidden select: drive it like a user.
async function chooseStage(page, id) {
  await page.locator('#stage-button').click();
  await page.locator(`#stage-menu .stage-option[data-value=${JSON.stringify(id)}]`).click();
}

const settleMap=page=>page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
async function checkBusinessStages(page, model) {
  if(!await page.locator('#map-hierarchy').count())return;
  const hierarchy=JSON.parse(await page.locator('#map-hierarchy').textContent());
  if(!hierarchy.stages?.length)return;
  if(await page.locator('#inspector').isVisible())await page.locator('#close-inspector').click();
  const setMode=async mode=>{await page.locator('#view-mode [data-mode="'+mode+'"]').focus();await page.keyboard.press('Enter');await settleMap(page);};
  const terminalIds=new Set(model.scenario?.terminal_operation_ids||[]);
  const names={success:'Успешное завершение',refusal:'Бизнес-отказ',failure:'Технический сбой',cancelled:'Отмена',deferred:'Отложенная обработка',no_decision:'Решение не принято',unknown:'Завершение · исход не уточнён'};
  await setMode('stages');
  for(const stage of hierarchy.stages.filter(stage=>stage.collapsible)){
    const card=page.locator('.kind-stage[data-stage-id="'+stage.id+'"]');
    assert.equal(await card.count(),1,'Every closed collapsible stage is a block');
    const members=model.nodes.filter(n=>hierarchy.membership[n.id]===stage.id),ends=members.filter(n=>terminalIds.has(n.id));
    const badges=await card.locator('[data-outcome]').evaluateAll(items=>items.map(n=>({outcome:n.dataset.outcome,scope:n.dataset.outcomeScope,conditional:n.dataset.conditional,text:n.textContent})));
    assert.equal(badges.length,new Set(ends.map(n=>(n.outcome_scope||'branch')+':'+(names[n.outcome]?n.outcome:'unknown')+':'+!!n.completion_condition)).size,'Each distinct outcome, scope and conditional status has its own label');
    for(const node of ends){const outcome=names[node.outcome]?node.outcome:'unknown';
      const badge=badges.find(b=>b.outcome===outcome&&b.scope===(node.outcome_scope||'branch')&&b.conditional===String(!!node.completion_condition));
      assert(badge,'A collapsed stage retains each terminal outcome');
      const label=node.outcome_scope==='shared'?(node.completion_condition?'общая обработка разных исходов':'Общее завершение разных ветвей'):names[outcome];
      assert(badge.text.toLocaleLowerCase('ru').includes(label.toLocaleLowerCase('ru')),'Exact outcome label survives collapsing');
      assert.equal(badge.text.startsWith('При завершении:'),!!node.completion_condition,'Conditional completion remains conditional');}
  }
  const stage=hierarchy.stages.find(stage=>stage.collapsible);
  if(stage){
    // Start with a member, then change views without changing the selected operation.
    const member=model.nodes.find(n=>hierarchy.membership[n.id]===stage.id&&n.id!==stage.entry_operation_id)||model.nodes.find(n=>hierarchy.membership[n.id]===stage.id);
    await setMode('atomic');
    await page.locator('[data-node-id="'+member.id+'"]').focus();await page.keyboard.press('Enter');
    await setMode('stages');
    const incoming=new Map(),outgoing=new Map();
    for(const edge of model.edges){if(!outgoing.has(edge.source))outgoing.set(edge.source,[]);outgoing.get(edge.source).push(edge.target);if(!incoming.has(edge.target))incoming.set(edge.target,[]);incoming.get(edge.target).push(edge.source);}
    const reach=adj=>{const found=new Set([member.id]),queue=[member.id];while(queue.length)for(const next of adj.get(queue.pop())||[])if(!found.has(next)){found.add(next);queue.push(next);}return found;};
    const before=reach(incoming),after=reach(outgoing),scope=new Set([...before,...after]);
    const edges=new Set(model.edges.filter(e=>(before.has(e.source)&&before.has(e.target))||(after.has(e.source)&&after.has(e.target))).map(e=>e.id));
    const assertPaths=async()=>{
      for(const item of await page.locator('.node').evaluateAll(items=>items.map(n=>({id:n.dataset.nodeId,stage:n.dataset.stageId,lit:n.classList.contains('in-scope')})))){
        const expected=item.id?scope.has(item.id):model.nodes.some(n=>hierarchy.membership[n.id]===item.stage&&scope.has(n.id));
        assert.equal(item.lit,expected,'Changing view preserves canonical operation paths');}
      for(const link of await page.locator('.edge').evaluateAll(items=>items.map(n=>({ids:JSON.parse(n.dataset.transitionIds),lit:n.classList.contains('in-scope')}))))
        assert.equal(link.lit,link.ids.some(id=>edges.has(id)),'A collapsed stage must not introduce bypass edges into the selected path');
    };
    await assertPaths();await setMode('atomic');await assertPaths();await setMode('stages');
    await page.locator('#close-inspector').click();
    const exit=model.edges.find(e=>hierarchy.membership[e.source]===stage.id&&hierarchy.membership[e.target]!==stage.id);
    let focusTransition=null;
    if(exit){
      const linkId=await page.locator('.edge').evaluateAll((links,id)=>links.find(link=>JSON.parse(link.dataset.transitionIds).includes(id))?.dataset.edgeId,exit.id);
      if(linkId){const edge=page.locator('.edge[data-edge-id="'+linkId+'"]');focusTransition=JSON.parse(await edge.getAttribute('data-transition-ids'))[0];await edge.focus();await page.keyboard.press('Enter');}
    }
    await page.locator('.kind-stage[data-stage-id="'+stage.id+'"]').focus();await page.keyboard.press('Enter');await settleMap(page);
    const head=page.locator('.stage-frame-head[data-stage-id="'+stage.id+'"]');
    assert.equal(await head.count(),1,'The stage expands on the same canvas');
    for(const member of model.nodes.filter(n=>hierarchy.membership[n.id]===stage.id))assert.equal(await page.locator('[data-node-id="'+member.id+'"]').count(),1,'Expansion exposes every atomic operation');
    if(focusTransition){await page.locator('#close-inspector').click();
      assert((await page.evaluate(()=>JSON.parse(document.activeElement.dataset.transitionIds||'[]'))).includes(focusTransition),'Focus after a redraw follows a canonical transition rather than a reused projection link id');}
    await head.locator('.stage-frame-title').focus();await page.keyboard.press('Enter');
    assert.equal(await page.locator('#inspector-content h2').innerText(),stage.name);
    const members=model.nodes.filter(n=>hierarchy.membership[n.id]===stage.id);
    for(const node of members){
      const outgoing=model.edges.filter(e=>e.source===node.id&&e.kind!=='data'),incoming=model.edges.filter(e=>e.target===node.id&&e.kind!=='data');
      if(outgoing.length>1)assert.equal(await page.locator('.stage-semantics [data-operation-id="'+node.id+'"][data-continuation-mode="'+(node.continuation_mode||'sequence')+'"]').count(),1);
      if(incoming.length>1)assert.equal(await page.locator('.stage-semantics [data-operation-id="'+node.id+'"][data-join-mode="'+(node.join_mode||'any')+'"]').count(),1);
    }
    await page.getByRole('button',{name:model.semantic_profile==='variant-actions-v1'?'Свернуть операцию на карте':'Свернуть этап на карте',exact:true}).click();await settleMap(page);
    await page.locator('#close-inspector').click();
    assert.equal(await page.locator('.kind-stage[data-stage-id="'+stage.id+'"]').evaluate(node=>document.activeElement===node),true,'Closing the panel returns focus to the replacement collapsed block');
  }
  await setMode('atomic');await page.locator('#reset').click();
}

async function checkTerminalOutcomes(page, model, legacy) {
  const ends = new Set(model.scenario?.terminal_operation_ids || []);
  const styles = await page.locator('.node').evaluateAll(nodes => nodes.map(node => {
    const surface = node.querySelector('.node-surface'), shape = surface.firstElementChild;
    const tokens = getComputedStyle(node);
    return {id:node.dataset.nodeId, classes:[...node.classList], label:node.querySelector('.node-terminal')?.textContent,
      accessible:node.getAttribute('aria-label'), glow:getComputedStyle(surface).filter,
      stroke:getComputedStyle(shape).stroke, success:tokens.getPropertyValue('--success').trim(),
      danger:tokens.getPropertyValue('--danger').trim()};
  }));
  for (const style of styles) {
    const node = model.nodes.find(node=>node.id===style.id), terminal=legacy?node.kind==='end':ends.has(node.id);
    const negative=['refusal','failure','cancelled'].includes(node.outcome);
    assert.equal(!!style.label,terminal,'Only declared terminal operations have an outcome label');
    assert.equal(style.classes.includes('terminal-success'),terminal&&node.outcome==='success','Success comes from the explicit business outcome');
    assert.equal(style.classes.includes('terminal-negative'),terminal&&negative,'Refusal ends its branch even when its code returned normally');
    if(terminal){
      assert(style.accessible.includes(style.label),'The outcome is accessible without relying on colour');
      if(node.completion_condition)assert(style.label.startsWith('При завершении:'),'Conditional outcomes must not imply every execution ends here');
      if(node.outcome==='success'||negative)assert.match(style.glow,/drop-shadow/,'A terminal business outcome must retain its glow');
      else assert.equal(style.glow,'none','Unresolved or deferred outcomes must not acquire a success/failure colour');
    }
  }
  const refusal=model.nodes.find(node=>ends.has(node.id)&&node.outcome==='refusal');
  if(refusal){
    const button=page.locator('[data-node-id="'+refusal.id+'"]');
    await button.focus();await page.keyboard.press('Enter');
    assert.match(await page.locator('#inspector-content').innerText(),/Бизнес-отказ/);
    const selected=await button.evaluate(node=>({glow:getComputedStyle(node.querySelector('.node-surface')).filter,
      stroke:getComputedStyle(node.querySelector('.node-surface').firstElementChild).stroke,
      expected:(()=>{const swatch=document.createElement('span');swatch.style.color='var(--danger)';node.append(swatch);const color=getComputedStyle(swatch).color;swatch.remove();return color;})()}));
    assert.match(selected.glow,/drop-shadow/);assert.equal(selected.stroke,selected.expected,'Selection must not recolour refusal as success');
    await page.keyboard.press('Escape');await page.locator('#reset').click();
  }
}

async function checkCursorZoom(page, member) {
  // Observe delivered browser events; never invoke the renderer's zoom helpers.
  await page.evaluate(()=>{
    window.__qaWheelCount=0;
    window.addEventListener('wheel',()=>window.__qaWheelCount++,true);
  });
  const settle=()=>page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const snapshot=point=>page.evaluate(point=>{
    const view=document.getElementById('viewport'),stage=document.getElementById('stage');
    const rect=stage.getBoundingClientRect(),viewRect=view.getBoundingClientRect(),inspector=document.getElementById('inspector');
    const panelRect=inspector.getBoundingClientRect(),scale=new DOMMatrixReadOnly(getComputedStyle(stage).transform).a;
    const left=viewRect.left+view.clientLeft,top=viewRect.top+view.clientTop;
    const right=!inspector.hidden?Math.min(left+view.clientWidth,panelRect.left):left+view.clientWidth;
    const visual=window.visualViewport;
    return {scale,world:{x:(point.x-rect.left)/scale,y:(point.y-rect.top)/scale},
      stage:{left:rect.left,top:rect.top,width:rect.width,height:rect.height},
      area:{left,top,width:Math.max(0,right-left),height:view.clientHeight},
      viewScroll:{x:view.scrollLeft,y:view.scrollTop},wheelCount:window.__qaWheelCount,
      page:{x:scrollX,y:scrollY,rootX:document.scrollingElement.scrollLeft,rootY:document.scrollingElement.scrollTop,
        dpr:devicePixelRatio,width:innerWidth,height:innerHeight,visualScale:visual?.scale||1,
        visualWidth:visual?.width||innerWidth,visualHeight:visual?.height||innerHeight}};
  },point);
  function assertPage(before,after,label){
    for(const [key,value] of Object.entries(before.page))assert(Math.abs(value-after.page[key])<.1,`${label}: browser page ${key} must not change`);
  }
  function assertAnchor(before,after,label){
    assert(Number.isFinite(after.scale)&&after.scale>0,`${label}: finite positive map scale`);
    // Scroll offsets may be rounded to a CSS pixel by the browser.
    const error=Math.hypot(after.world.x-before.world.x,after.world.y-before.world.y)*after.scale;
    assert(error<=1.6,`${label}: point under cursor moved ${error.toFixed(3)} screen px`);
    assertPage(before,after,label);
  }
  async function fieldPoint(){
    const {area}=await snapshot({x:0,y:0});
    if(area.width<80||area.height<80)return null; // Portrait inspector covers the whole map.
    for(const [fx,fy] of [[.29,.36],[.61,.42],[.42,.23]]){
      const point={x:area.left+area.width*fx,y:area.top+area.height*fy};
      if(await page.evaluate(p=>!!document.elementFromPoint(p.x,p.y)?.closest('#viewport'),point))return point;
    }
    assert.fail('No unobstructed map point for wheel input');
  }
  async function wheel(point,delta,ctrl=false,label='wheel'){
    await page.mouse.move(point.x,point.y);
    const before=await snapshot(point);
    if(ctrl)await page.keyboard.down('Control');
    try{
      await page.mouse.wheel(0,delta);
      await page.waitForFunction(count=>window.__qaWheelCount>count,before.wheelCount);
      await settle();
    }finally{if(ctrl)await page.keyboard.up('Control');}
    const after=await snapshot(point);assertAnchor(before,after,label);
    return {before,after};
  }
  function assertDirection(result,direction,label){
    assert(direction*(result.after.scale-result.before.scale)>1e-7,`${label}: map scale must ${direction>0?'increase':'decrease'}`);
  }
  await page.locator('#reset').click();
  // A real wheel event bubbles from a node, not just from an empty canvas.
  const nodePoint=await page.evaluate(()=>{
    const view=document.getElementById('viewport'),vr=view.getBoundingClientRect();
    for(const node of document.querySelectorAll('.node')){
      const r=node.getBoundingClientRect(),left=Math.max(r.left,vr.left+2),right=Math.min(r.right,vr.left+view.clientWidth-2);
      const top=Math.max(r.top,vr.top+2),bottom=Math.min(r.bottom,vr.top+view.clientHeight-2);
      if(right-left<10||bottom-top<10)continue;
      const x=(left+right)/2,y=(top+bottom)/2;
      if(document.elementFromPoint(x,y)?.closest('.node')===node)return {x,y,id:node.dataset.nodeId};
    }
    return null;
  });
  assert(nodePoint,'A visible operation must accept wheel input');
  assertDirection(await wheel(nodePoint,-90,false,'Wheel up over operation'),1,'Wheel up');
  assert.equal(await page.evaluate(p=>document.elementFromPoint(p.x,p.y)?.closest('.node')?.dataset.nodeId,nodePoint),nodePoint.id,'The same operation stays under the cursor');
  assertDirection(await wheel(nodePoint,90,false,'Wheel down over operation'),-1,'Wheel down');
  let point=await fieldPoint();
  assertDirection(await wheel(point,-30,true,'Control+wheel zoom in'),1,'Control+wheel up');
  assertDirection(await wheel(point,30,true,'Control+wheel zoom out'),-1,'Control+wheel down');

  // WheelEvent deltaMode and WebKit GestureEvent formats are synthetic compatibility
  // checks in Edge, not a claim that physical trackpad/Safari devices were tested.
  for(const [mode,delta] of [[0,40],[1,2],[2,.12]]){
    await page.locator('#reset').click();point=await fieldPoint();
    for(const sign of [-1,1]){
      const before=await snapshot(point);
      const canceled=await page.evaluate(({point,mode,delta})=>!document.elementFromPoint(point.x,point.y).dispatchEvent(new WheelEvent('wheel',{
        bubbles:true,cancelable:true,clientX:point.x,clientY:point.y,deltaY:delta,deltaMode:mode})),{point,mode,delta:sign*delta});
      await settle();const after=await snapshot(point);
      assert(canceled,`deltaMode ${mode}: native scrolling is canceled over map`);
      assertAnchor(before,after,`deltaMode ${mode}`);assertDirection({before,after},-sign,`deltaMode ${mode}`);
    }
  }
  await page.locator('#reset').click();point=await fieldPoint();
  const gesture=async(type,scale)=>page.evaluate(({point,type,scale})=>{
    const event=new Event(type,{bubbles:true,cancelable:true});
    Object.defineProperties(event,{scale:{value:scale},clientX:{value:point.x},clientY:{value:point.y}});
    return !document.elementFromPoint(point.x,point.y).dispatchEvent(event);
  },{point,type,scale});
  assert(await gesture('gesturestart',1),'Synthetic gesturestart must cancel browser zoom');
  for(const scale of [1.25,.85]){
    const before=await snapshot(point);assert(await gesture('gesturechange',scale));await settle();
    const after=await snapshot(point);assertAnchor(before,after,'Synthetic WebKit gesture');
    assert(Math.abs(after.scale-scale)<1e-5,'Gesture scale is relative to gesturestart, not accumulated changes');
  }
  assert(await gesture('gestureend',.85));
  assertDirection(await wheel(point,-40,false,'Wheel after gestureend'),1,'Wheel after gestureend');

  await page.locator('#reset').click();point=await fieldPoint();
  async function reachLimit(delta,label){
    for(let i=0;i<40;i++){
      const result=await wheel(point,delta,false,label);
      if(Math.abs(result.after.scale-result.before.scale)<1e-9){
        const repeated=await wheel(point,delta,false,label+' repeated');
        assert.equal(repeated.after.scale,result.after.scale,`${label}: stable clamp`);
        return repeated.after;
      }
    }
    assert.fail(`${label}: wheel zoom did not reach a finite limit`);
  }
  const maximum=await reachLimit(-100000,'Maximum wheel zoom');
  const minimum=await reachLimit(100000,'Minimum wheel zoom');
  assert(maximum.scale>1&&minimum.scale<1,'Wheel must cover both sides of 100%');
  assert.equal(await page.locator('#zoom-out').isDisabled(),true,'Minimum clamp agrees with zoom controls');

  async function checkCenteringThreshold(label){
    // "Вписать" fits across the flow only; the minimum zoom fits the whole graph.
    for(let i=0;i<60&&!(await page.locator('#zoom-out').isDisabled());i++)await page.locator('#zoom-out').click();
    point=await fieldPoint();
    const fits=s=>s.stage.width<=s.area.width+.5&&s.stage.height<=s.area.height+.5;
    let state=await snapshot(point);assert(fits(state),`${label}: fitted graph is smaller than its visible field`);
    for(let i=0;fits(state)&&i<40;i++){
      const result=await wheel(point,-120,false,label+' before threshold');state=result.after;
      if(Math.abs(result.after.scale-result.before.scale)<1e-9)break;
    }
    // Tiny partial reports can fit even at the maximum permitted zoom.
    if(fits(state))return;
    for(let i=0;!fits(state)&&i<40;i++)state=(await wheel(point,120,false,label+' crossing threshold')).after;
    assert(fits(state),`${label}: wheel down must cross into a graph smaller than the field`);
    await wheel(point,120,false,label+' inside centered range');
  }
  await checkCenteringThreshold('Closed inspector');
  await page.locator('[data-node-id="'+member.id+'"]').focus();await page.keyboard.press('Enter');
  if(await fieldPoint())await checkCenteringThreshold('Open inspector');
  const source=page.locator('#inspector-content .source-function').first();
  if(await source.count()){
    await source.focus();await page.keyboard.press('Enter');
  }
  const panel=page.locator('#inspector-content');
  await panel.evaluate(element=>element.scrollTo(0,0));
  const panelBox=await panel.boundingBox(),panelPoint={x:panelBox.x+panelBox.width*.64,y:panelBox.y+panelBox.height*.47};
  const before=await snapshot(panelPoint),panelBefore=await panel.evaluate(element=>({top:element.scrollTop,max:element.scrollHeight-element.clientHeight}));
  await page.mouse.move(panelPoint.x,panelPoint.y);await page.mouse.wheel(0,280);
  await page.waitForFunction(count=>window.__qaWheelCount>count,before.wheelCount);await settle();
  if(panelBefore.max>1)await page.waitForFunction(()=>document.getElementById('inspector-content').scrollTop>0);
  const after=await snapshot(panelPoint);
  assert.equal(after.scale,before.scale,'Ordinary inspector scrolling must not zoom the map');
  assert.deepEqual(after.viewScroll,before.viewScroll,'Inspector scrolling must not pan the map');
  assertPage(before,after,'Inspector scroll');
  if(panelBefore.max>1)assert((await panel.evaluate(element=>element.scrollTop))>panelBefore.top,'Wheel must scroll overflowing operation details');
  await page.keyboard.press('Escape');await page.locator('#reset').click();
}

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const model = JSON.parse(html.match(/<script id="scenario-model" type="application\/json">([\s\S]*?)<\/script>/)[1]);
  model.nodes ||= [];model.edges ||= [];model.groups ||= [];
  const legacy=String(model.schema_version||'1.0').split('.')[0]!=='2';
  const output = process.argv[3];
  if (output) fs.mkdirSync(output, {recursive:true});
  const browser = await chromium.launch({headless:true, channel:'msedge'});
  try {
    for (const [width, height] of [[1191,676], [390,844], [844,390]]) {
      const page = await browser.newPage({viewport:{width,height}, reducedMotion:'reduce'});
      const errors = [];
      page.on('pageerror', error => errors.push(String(error)));
      await page.route('**/*', route => route.abort());
      await page.setContent(html, {waitUntil:'load'});
      await page.evaluate(() => document.fonts.ready);
      await page.waitForFunction(() => document.querySelectorAll('.node').length > 0 || !document.getElementById('empty-map').hidden);
      if(!model.nodes.length){
        assert.equal(await page.locator('#empty-map').isVisible(),true);
        assert.equal(await page.locator('#canvas-size').isVisible(),false);
        assert.equal(await page.locator('#inspector').isVisible(),false);
        assert.equal(await page.locator('.node,.edge').count(),0);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        if(model.validation?.issues?.length||model.source_omissions?.length){
          await page.locator('#status-toggle').click();
          await page.locator('.checks-notice').click();
          const findings=await page.locator('#inspector-content').innerText();
          for(const issue of model.validation?.issues||[])assert(findings.includes(issue));
          await page.keyboard.press('Escape');
        }
        await page.locator('#fit').click();
        assert.deepEqual(errors,[]);
        if(output)await page.screenshot({path:path.join(output,`empty-${width}x${height}.png`)});
        console.log(`PASS ${width}x${height}: empty partial map, missing fields, diagnostics, keyboard`);
        await page.close();continue;
      }
      assert.equal(await page.locator('.node').count(), model.nodes.length);
      assert.equal(await page.locator('.edge').count(), model.edges.length);
      assert.equal(await page.locator('.edge-label,.edge text,.edge title').count(),0,'Links must have no text, including hover labels');
      if(legacy)assert.equal(await page.locator('#status-toggle').isVisible(),true,'Old reports must remain explicitly marked');
      assert.equal(await page.locator('#legacy-note').evaluate(e=>!e.hidden),legacy,'Old reports must remain explicitly marked');
      if(!legacy){
        assert.equal(await page.locator('.node.kind-operation').count(),model.nodes.length);
        assert.equal(await page.locator('.node-surface>rect').count(),model.nodes.length,'Every operation uses the same card shape');
        assert.equal(await page.locator('.node-surface>path,.node-system').count(),0);
        assert.doesNotMatch(await page.locator('#overview').innerText(),/АС/);
      }
      assert(await page.locator('.edge-visible').evaluateAll(edges=>edges.every(e=>{
        const d=e.getAttribute('d');return d.includes('C ')&&!/[LQHV]/.test(d);
      })), 'Every link must be drawn with cubic curves');
      const geometry = await page.evaluate(() => {
        const stage = document.getElementById('stage');
        const nodes = [...document.querySelectorAll('.node')].map(n => ({id:n.dataset.nodeId,x:n.offsetLeft,y:n.offsetTop,w:n.offsetWidth,h:n.offsetHeight}));
        const collisions=[];
        for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){
          const a=nodes[i],b=nodes[j];
          if(a.x<b.x+b.w&&b.x<a.x+a.w&&a.y<b.y+b.h&&b.y<a.y+a.h)collisions.push([a.id,b.id]);
        }
        return {nodes,collisions,width:stage.offsetWidth,height:stage.offsetHeight,overflow:document.documentElement.scrollWidth>innerWidth};
      });
      assert.deepEqual(geometry.collisions, [], 'Node boxes must not overlap');
      assert.equal(geometry.overflow, false, 'Page must not overflow outside the graph viewport');
      for (const node of geometry.nodes) {
        assert(node.x>=0&&node.y>=0&&node.x+node.w<=geometry.width+1&&node.y+node.h<=geometry.height+1, 'Node must remain inside canvas');
      }
      const edgeGeometry = await page.evaluate(edges => {
        const boxes=[...document.querySelectorAll('.node')].map(n=>({id:n.dataset.nodeId,x:n.offsetLeft,y:n.offsetTop,w:n.offsetWidth,h:n.offsetHeight}));
        const crossings=[];
        for(const edge of edges){
          const path=document.querySelector('[data-edge-id="'+edge.id+'"] .edge-visible');
          const length=path.getTotalLength(),steps=Math.ceil(length/4);
          const others=boxes.filter(n=>n.id!==edge.source&&n.id!==edge.target);
          for(let i=0;i<=steps;i++){
            const point=path.getPointAtLength(length*i/steps);
            const box=others.find(n=>point.x>n.x+1&&point.x<n.x+n.w-1&&point.y>n.y+1&&point.y<n.y+n.h-1);
            if(box){crossings.push([edge.id,box.id]);break;}
          }
        }
        return crossings;
      }, model.edges);
      assert.deepEqual(edgeGeometry, [], 'Curves must avoid unrelated node boxes');
      await page.locator('#notation-toggle').click();
      assert.equal(await page.locator('#notation-items .notation-item').count(), (legacy?8:1)+(await page.locator('#view-mode').isVisible()?1:0));
      assert.match(await page.locator('#notation-panel').innerText(), model.semantic_profile==='variant-actions-v1'
        ? /Условия выбора следующих действий указаны в карточке исходного действия/
        : /Условия выбора следующих операций указаны в карточке исходной операции/);
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#notation-toggle').getAttribute('aria-expanded'), 'false');
      assert.equal(await page.locator('#notation-toggle').evaluate(e=>document.activeElement===e), true);
      assert.equal(await page.locator('#structure-toggle').count(), 0);
      assert.equal(await page.locator('.workspace-bar,.map-footer,.scope,.heading-badges,#search').count(), 0);
      assert.equal(await page.locator('[role="tab"]').count(), 0);
      if(width>900){
        const title=await page.locator('#page-title').boundingBox();
        const overview=await page.locator('#overview').boundingBox();
        assert(overview.x>=title.x+title.width,'Metrics must be to the right of the title');
        assert(Math.abs(overview.y+overview.height/2-title.y-title.height/2)<2,'Metrics and title must be vertically aligned');
      }
      const groups=model.groups||[],stages=groups.filter(g=>g.kind==='stage');
      for(const stage of [stages[0],stages[stages.length-1]].filter(Boolean)) {
        await chooseStage(page,stage.id);
        const memberIds=model.nodes.filter(n=>n.group_id===stage.id||model.groups.find(g=>g.id===n.group_id)?.parent_id===stage.id).map(n=>n.id);
        assert.equal(await page.locator('#inspector').isVisible(), false);
        assert.equal(await page.locator('.node.in-scope').count(), memberIds.length);
        assert.equal(await page.locator('.node:not(.dimmed)').count(), memberIds.length);
        await page.evaluate(()=>new Promise(requestAnimationFrame));
        const visible=await page.evaluate(ids=>{
          const view=document.getElementById('viewport').getBoundingClientRect();
          return ids.every(id=>{const r=document.querySelector('[data-node-id="'+id+'"]').getBoundingClientRect();return r.left>=view.left-2&&r.right<=view.right+2&&r.top>=view.top-2&&r.bottom<=view.bottom+2;});
        },memberIds);
        assert(visible,'All stage members must be inside viewport after stage selection');
      }
      if(stages.length)await chooseStage(page,'');
      assert.equal(await page.locator('.node.dimmed').count(),0);
      const reviewedBranch=[...model.nodes].reverse().find(n=>n.code_explanations?.length&&model.edges.filter(e=>e.source===n.id).length>1);
      const member=reviewedBranch||model.nodes.find(n=>n.basis&&n.code_explanations?.length)||model.nodes.find(n=>n.basis)||model.nodes[0];
      const memberGroup=groups.find(g=>g.id===member.group_id);
      const businessHierarchy=await page.locator('#map-hierarchy').count()?JSON.parse(await page.locator('#map-hierarchy').textContent()):null;
      const memberStage=businessHierarchy?.stages?.find(stage=>stage.id===businessHierarchy.membership?.[member.id]);
      if(memberGroup)await chooseStage(page,memberGroup.kind==='stage'?memberGroup.id:memberGroup.parent_id);
      await page.locator('[data-node-id="'+member.id+'"]').focus();
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('.node.selected').count(), 1);
      assert.equal(await page.locator('#stage-select').inputValue(), '', 'Stage can be selected again after inspecting an action');
      // Compute reachability independently from the renderer: the union of
      // paths into the selection and paths out of it excludes bypass branches.
      function reachable(reverse){
        const seen=new Set([member.id]),pending=[member.id];
        for(let index=0;index<pending.length;index++)for(const edge of model.edges){
          const from=reverse?edge.target:edge.source,to=reverse?edge.source:edge.target;
          if(from===pending[index]&&!seen.has(to)){seen.add(to);pending.push(to);}
        }
        return seen;
      }
      const ancestors=reachable(true),descendants=reachable(false);
      const highlightedIds=[...new Set([...ancestors,...descendants])].sort();
      const highlightedEdges=model.edges.filter(e=>(ancestors.has(e.source)&&ancestors.has(e.target))||
        (descendants.has(e.source)&&descendants.has(e.target))).map(e=>e.id).sort();
      assert.deepEqual(await page.locator('.node.in-scope').evaluateAll(nodes=>nodes.map(n=>n.dataset.nodeId).sort()),highlightedIds,'Highlight every ancestor and descendant operation');
      assert.deepEqual(await page.locator('.node:not(.dimmed)').evaluateAll(nodes=>nodes.map(n=>n.dataset.nodeId).sort()),highlightedIds);
      assert.deepEqual(await page.locator('.edge.in-scope').evaluateAll(edges=>edges.map(e=>e.dataset.edgeId).sort()),highlightedEdges,'Highlight paths through the selected operation, not bypass edges');
      assert.deepEqual(await page.locator('.edge:not(.dimmed)').evaluateAll(edges=>edges.map(e=>e.dataset.edgeId).sort()),highlightedEdges);
      assert.equal(await page.locator('.node').count(),model.nodes.length,'All nodes must stay on the map');
      const card=page.locator('#inspector-content');
      assert.equal(await card.locator('.execution-review,.predecessors').count(),0,'Operation card must not duplicate execution reviews or predecessors');
      assert.equal(await card.locator('summary',{hasText:model.semantic_profile==='variant-actions-v1'?/^Предыдущие действия$/:/^Предыдущие операции$/}).count(),0);
      assert.equal(await card.locator('dl').filter({has:page.locator('dt',{hasText:/^Обоснование$/})}).count(),0,'Basis belongs only to the unified source explanation section');
      assert.equal(await card.locator(':scope > details').filter({has:page.locator(':scope > summary',{hasText:/^Основания в исходниках/})}).count(),0,'No duplicate general evidence block');
      const stageLinks=card.locator('.operation-meta .group-links');
      if(memberGroup||memberStage){
        assert.equal(await stageLinks.count(),1);
        for(const group of [memberStage,memberGroup].filter(Boolean))assert((await stageLinks.innerText()).includes(group.name));
        const placement=await stageLinks.evaluate(group=>{
          const meta=group.closest('.operation-meta'),title=document.querySelector('#inspector-content h2');
          const box=group.getBoundingClientRect(),frame=meta.getBoundingClientRect(),heading=title.getBoundingClientRect();
          return {before:!!(group.compareDocumentPosition(title)&Node.DOCUMENT_POSITION_FOLLOWING),
            rightAligned:Math.abs(frame.right-box.right)<3,above:box.bottom<=heading.top+1};
        });
        assert(placement.before&&placement.above&&placement.rightAligned,'Stage links must be at the top right, before the operation title');
      }else assert.equal(await stageLinks.count(),0);
      if(output)await page.screenshot({path:path.join(output,`operation-card-${width}x${height}.png`)});
      if(member.code_explanations?.length){
        const section=card.locator('section.code-explanations');
        assert.equal(await section.count(),1);
        assert.equal(await section.locator('h3').first().innerText(),'Обоснование из исходников кода');
        const explanations=section.locator('article.code-explanation');
        assert.equal(await explanations.count(),member.code_explanations.length);
        for(let i=0;i<member.code_explanations.length;i++){
          const entry=member.code_explanations[i],explanation=explanations.nth(i),reference=entry.evidence?.[0];
          const link=explanation.locator('a.source-function').first(),preview=explanation.locator('pre.code-fragment').first();
          assert.equal(await link.count(),1);
          assert.equal(await explanation.locator('p.code-meaning').innerText(),entry.explanation);
          const label=await link.innerText();
          if(entry.symbol)assert(label.includes(entry.symbol),'Source link must name the actual function');
          if(reference?.path)assert(label.includes(reference.path.split(/[\\/]/).pop()),'Source link must name the source file');
          assert.equal(await preview.isVisible(),true);
          const snippet=await preview.textContent();
          assert(snippet.trimEnd().split(/\r?\n/).length<=5,'Inline source preview must remain at most five lines');
          const order=await explanation.evaluate(article=>{
            const source=article.querySelector('.source-function'),code=article.querySelector('.code-fragment'),meaning=article.querySelector('.code-meaning');
            return !!(source.compareDocumentPosition(code)&Node.DOCUMENT_POSITION_FOLLOWING)&&!!(code.compareDocumentPosition(meaning)&Node.DOCUMENT_POSITION_FOLLOWING);
          });
          assert(order,'Source link, short code fragment and meaning must appear in that order');
          const controlled=await link.getAttribute('aria-controls');
          assert(controlled,'Source link must identify its saved fragment');
          assert.equal(await link.getAttribute('href'),'#'+controlled);
          assert.equal(await link.getAttribute('aria-expanded'),'false');
          const fragment=page.locator('[id="'+controlled+'"]');
          assert(await fragment.evaluate(e=>e.classList.contains('source-fragment')));
          assert.equal(await fragment.isVisible(),false);
          const pageAddress=page.url().split('#')[0];
          await link.focus();await page.keyboard.press('Enter');
          assert.equal(await link.getAttribute('aria-expanded'),'true');
          assert.equal(await fragment.isVisible(),true);
          assert.equal(await preview.isVisible(),false,'Expanded source replaces the preview instead of duplicating it');
          assert.equal(await card.locator('.source-fragment:visible').count(),1,'Only the selected saved fragment is expanded');
          assert.equal(page.url().split('#')[0],pageAddress,'Opening evidence must stay inside the standalone report');
          const fullSource=(await fragment.textContent()).replace(/\r\n/g,'\n');
          if(reference?.quote)assert(fullSource.includes(reference.quote.replace(/\r\n/g,'\n')),'Expanded source must contain the exact saved quote');
          if(reference?.path){
            assert(fullSource.includes(reference.path));
            if(reference.line_start)assert(fullSource.includes(reference.path+':'+reference.line_start),'Full source must show its original line location');
          }
          await link.focus();await page.keyboard.press('Enter');
          assert.equal(await link.getAttribute('aria-expanded'),'false');
          assert.equal(await fragment.isVisible(),false);
          assert.equal(await preview.isVisible(),true);
        }
      }else if(member.basis){
        assert((await card.locator('section.code-explanations').innerText()).includes(member.basis),'Legacy basis is permitted only as a fallback source explanation');
      }
      if(output){
        if(member.code_explanations?.length)await card.locator('section.code-explanations').scrollIntoViewIfNeeded();
        await page.screenshot({path:path.join(output,`explanation-${width}x${height}.png`)});
      }
      assert.equal(await page.locator('#inspector-content details[open]').count(),0,'Literal citations and system details start collapsed');
      const system=legacy?(model.systems||[]).find(s=>s.id===member.system_id):null;
      if(system?.basis){
        await page.locator('#inspector-content summary',{hasText:'Что известно об АС'}).click();
        assert.match(await page.locator('#inspector-content').innerText(),new RegExp(system.basis.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')));
        assert.equal(await page.locator('#inspector-content details[open] .evidence:visible').count(),0,'System citations stay collapsed');
      }
      await page.keyboard.press('Escape');
      for(const operation of model.nodes.filter(n=>model.edges.filter(e=>e.source===n.id).length>1)){
        await page.locator('[data-node-id="'+operation.id+'"]').focus();await page.keyboard.press('Enter');
        const outgoing=model.edges.filter(e=>e.source===operation.id);
        assert.equal(await page.locator('.continuation').count(),outgoing.length,'Every outgoing condition belongs in the source operation card');
        for(const edge of outgoing){
          const row=page.locator('.continuation[data-transition-id="'+edge.id+'"]');
          assert.equal(await row.locator('.continuation-condition').innerText(),edge.condition||'Условие продолжения не установлено');
          assert.equal(await row.locator('.continuation-target').innerText(),model.nodes.find(n=>n.id===edge.target).name);
          assert.equal(await row.locator('.continuation-evidence').count(),0,'Continuation rows contain no separate evidence button');
          const presentation=await row.evaluate(item=>{
            const target=item.querySelector('.continuation-target'),condition=item.querySelector('.continuation-condition');
            const targetStyle=getComputedStyle(target),conditionStyle=getComputedStyle(condition);
            const sample=document.createElement('span');item.append(sample);
            sample.style.color='var(--purple)';const purple=getComputedStyle(sample).color;
            sample.style.color='var(--ink)';const ink=getComputedStyle(sample).color;sample.remove();
            return {before:!!(target.compareDocumentPosition(condition)&Node.DOCUMENT_POSITION_FOLLOWING),
              above:target.getBoundingClientRect().bottom<=condition.getBoundingClientRect().top+1,
              bold:Number(targetStyle.fontWeight)>=600,purple:targetStyle.color===purple,
              regular:Number(conditionStyle.fontWeight)<=500,black:conditionStyle.color===ink};
          });
          assert(presentation.before&&presentation.above,'Next operation title must precede its condition');
          assert(presentation.bold&&presentation.purple,'Next operation title must be bold purple');
          assert(presentation.regular&&presentation.black,'Transition condition must use regular body text colour');
        }
        if(!legacy&&operation.continuation_mode==='all')assert.match(await page.locator('.continuations').innerText(),/Запускаются все/);
        if(!legacy&&operation.continuation_mode==='choice')assert.match(await page.locator('.continuations').innerText(),
          model.semantic_profile==='variant-actions-v1'?/условие которого выполнено/:/условие которой выполнено/);
        if(output&&width>900){await page.locator(".continuations").scrollIntoViewIfNeeded();await page.screenshot({path:path.join(output,`conditions-${width}x${height}.png`)});}
        const next=model.nodes.find(n=>n.id===outgoing[0].target);
        await page.locator('.continuation-target').first().click();
        assert.equal(await page.locator('#inspector-content h2').innerText(),next.name,'Next operation button must navigate to its card');
        await page.keyboard.press('Escape');
      }
      for(const operation of model.nodes.filter(n=>n.external_interactions?.length)){
        await page.locator('[data-node-id="'+operation.id+'"]').focus();await page.keyboard.press('Enter');
        const cards=page.locator('.external-interaction');assert.equal(await cards.count(),operation.external_interactions.length);
        for(let i=0;i<operation.external_interactions.length;i++){
          const interaction=operation.external_interactions[i],content=await cards.nth(i).innerText();
          assert(content.includes(interaction.name));assert(content.includes(interaction.purpose));
        }
        assert.equal(await page.locator('.external-interactions details[open]').count(),0);
        if(output&&width>900){await page.locator(".external-interactions").scrollIntoViewIfNeeded();await page.screenshot({path:path.join(output,`external-${width}x${height}.png`)});}
        await page.keyboard.press('Escape');
      }
      const edge=model.edges.find(e=>e.basis)||model.edges[0];
      if(edge){await page.locator('[data-edge-id="'+edge.id+'"]').focus();
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('#inspector').isVisible(), true);
      if(edge.basis)assert.equal(await page.locator('#inspector-content dl').filter({has:page.locator('dt',{hasText:/^Обоснование перехода$/})}).locator('dd').innerText(),edge.basis);}
      await page.keyboard.press('Escape');
      if(model.gaps?.length){
        await page.locator('.findings-toggle').click();
        assert.equal(await page.locator('#inspector-content h2').innerText(),'Что осталось установить');
        const findings=await page.locator('#inspector-content').innerText();
        for(const gap of model.gaps)assert(findings.includes(gap.description));
        await page.keyboard.press('Escape');
      }
      await page.locator('#zoom-in').click();
      await page.locator('#fit').click();
      await checkCursorZoom(page,member);
      await page.locator('#reset').click();
      assert.equal(await page.locator('#zoom-value').innerText(), '100%');
      assert.equal(await page.locator('.node.dimmed').count(), 0);
      await checkTerminalOutcomes(page,model,legacy);
      await checkBusinessStages(page,model);
      if (output) {
        await page.locator('#reset').click();
        await page.screenshot({path:path.join(output,`map-${width}x${height}.png`)});
      }
      assert.deepEqual(errors, [], 'No browser runtime errors');
      console.log(`PASS ${width}x${height}: smooth unlabelled curves, full path highlighting, compact evidence, routing, stages, keyboard; real wheel/ctrl+wheel cursor zoom, centering thresholds, clamps, panel scroll; synthetic deltaMode/WebKit gesture formats`);
      await page.close();
    }
  } finally {
    await browser.close();
  }
}
module.exports = {checkTerminalOutcomes, checkBusinessStages};
if(require.main===module)main().catch(error=>{console.error(error);process.exitCode=1;});
