/* Optional browser acceptance check. Requires Playwright and installed Edge.
 * Reads a generated HTML map; uses a fresh offline browser without source execution.
 * Usage: node scripts/check_scenario_map_ui.cjs report.html [screenshot-folder]
 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const model = JSON.parse(html.match(/<script id="scenario-model" type="application\/json">([\s\S]*?)<\/script>/)[1]);
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
      await page.waitForFunction(() => document.querySelectorAll('.node').length > 0);
      assert.equal(await page.locator('.node').count(), model.nodes.length);
      assert.equal(await page.locator('.edge').count(), model.edges.length);
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
      assert.equal(await page.locator('#notation-items .notation-item').count(), 8);
      assert.match(await page.locator('#notation-panel').innerText(), /АС — автоматизированная система/);
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#notation-toggle').getAttribute('aria-expanded'), 'false');
      assert.equal(await page.locator('#notation-toggle').evaluate(e=>document.activeElement===e), true);
      assert.equal(await page.locator('#structure-toggle').count(), 0);
      assert.equal(await page.locator('.workspace-bar,.map-footer,.scope,.heading-badges,#search,[role="tab"]').count(), 0);
      if(width>900){
        const title=await page.locator('#page-title').boundingBox();
        const overview=await page.locator('#overview').boundingBox();
        assert(overview.x>=title.x+title.width,'Metrics must be to the right of the title');
        assert(Math.abs(overview.y+overview.height/2-title.y-title.height/2)<2,'Metrics and title must be vertically aligned');
      }
      const stages=model.groups.filter(g=>g.kind==='stage');
      for(const stage of [stages[0],stages[stages.length-1]]) {
        await page.locator('#stage-select').selectOption(stage.id);
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
      await page.locator('#stage-select').selectOption('');
      assert.equal(await page.locator('.node.dimmed').count(),0);
      const member=model.nodes.find(n=>n.basis&&n.kind==='action')||model.nodes.find(n=>n.kind==='action')||model.nodes[0];
      const memberGroup=model.groups.find(g=>g.id===member.group_id);
      await page.locator('#stage-select').selectOption(memberGroup.kind==='stage'?memberGroup.id:memberGroup.parent_id);
      await page.locator('[data-node-id="'+member.id+'"]').focus();
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('.node.selected').count(), 1);
      assert.equal(await page.locator('#stage-select').inputValue(), '', 'Stage can be selected again after inspecting an action');
      const relatedEdges=model.edges.filter(e=>e.source===member.id||e.target===member.id);
      const neighborIds=[...new Set([member.id,...relatedEdges.flatMap(e=>[e.source,e.target])])].sort();
      assert.deepEqual(await page.locator('.node.in-scope').evaluateAll(nodes=>nodes.map(n=>n.dataset.nodeId).sort()),neighborIds);
      assert.deepEqual(await page.locator('.node:not(.dimmed)').evaluateAll(nodes=>nodes.map(n=>n.dataset.nodeId).sort()),neighborIds);
      assert.deepEqual(await page.locator('.edge.in-scope').evaluateAll(edges=>edges.map(e=>e.dataset.edgeId).sort()),relatedEdges.map(e=>e.id).sort());
      assert.equal(await page.locator('.node').count(),model.nodes.length,'All nodes must stay on the map');
      if(member.basis){
        assert.equal(await page.locator('#inspector-content dl').filter({has:page.locator('dt',{hasText:/^Обоснование$/})}).locator('dd').innerText(),member.basis);
      }
      if(member.code_explanations?.length){
        const explanations=page.locator('#inspector-content .code-explanation');
        assert.equal(await explanations.count(),member.code_explanations.length);
        for(let i=0;i<member.code_explanations.length;i++){
          const entry=member.code_explanations[i],content=await explanations.nth(i).innerText();
          assert(content.includes(entry.explanation));
          if(entry.symbol)assert(content.includes(entry.symbol));
        }
      }
      if(output&&width>900)await page.screenshot({path:path.join(output,`explanation-${width}x${height}.png`)});
      assert.equal(await page.locator('#inspector-content details[open]').count(),0,'Literal citations and system details start collapsed');
      const system=model.systems.find(s=>s.id===member.system_id);
      if(system?.basis){
        await page.locator('#inspector-content summary',{hasText:'Что известно об АС'}).click();
        assert.match(await page.locator('#inspector-content').innerText(),new RegExp(system.basis.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')));
        assert.equal(await page.locator('#inspector-content details[open] .evidence:visible').count(),0,'System citations stay collapsed');
      }
      await page.keyboard.press('Escape');
      const edge=model.edges.find(e=>e.basis)||model.edges[0];
      await page.locator('[data-edge-id="'+edge.id+'"]').focus();
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('#inspector').isVisible(), true);
      if(edge.basis)assert.equal(await page.locator('#inspector-content dl').filter({has:page.locator('dt',{hasText:/^Обоснование перехода$/})}).locator('dd').innerText(),edge.basis);
      await page.keyboard.press('Escape');
      await page.locator('#zoom-in').click();
      await page.locator('#fit').click();
      await page.locator('#reset').click();
      assert.equal(await page.locator('#zoom-value').innerText(), '100%');
      assert.equal(await page.locator('.node.dimmed').count(), 0);
      if (output) {
        await page.locator('#reset').click();
        await page.screenshot({path:path.join(output,`map-${width}x${height}.png`)});
      }
      assert.deepEqual(errors, [], 'No browser runtime errors');
      console.log(`PASS ${width}x${height}: smooth curves, no card crossings, all nodes, stages, explanations, AS boundaries, keyboard, zoom`);
      await page.close();
    }
  } finally {
    await browser.close();
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
