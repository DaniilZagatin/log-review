/* Optional browser acceptance check. Requires Playwright and installed Edge.
 * Reads a generated, synthetic HTML fixture; uses a fresh offline browser.
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
      await page.locator('#notation-toggle').click();
      assert.equal(await page.locator('#notation-items .notation-item').count(), 8);
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
        const memberIds=model.nodes.filter(n=>model.groups.find(g=>g.id===n.group_id)?.parent_id===stage.id).map(n=>n.id);
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
      const operation=model.groups.find(g=>g.kind==='operation'&&new Set(model.nodes.filter(n=>n.group_id===g.id).map(n=>n.system_id)).size>1)||model.groups.find(g=>g.kind==='operation');
      await page.locator('#stage-select').selectOption(operation.parent_id);
      const member=model.nodes.find(n=>n.group_id===operation.id);
      await page.locator('[data-node-id="'+member.id+'"]').focus();
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('.node.selected').count(), 1);
      assert.equal(await page.locator('#stage-select').inputValue(), '', 'Stage can be selected again after inspecting an operation');
      assert.equal(await page.locator('.node.in-scope').count(), model.nodes.filter(n=>n.group_id===operation.id).length);
      assert.equal(await page.locator('.node:not(.dimmed)').count(), model.nodes.filter(n=>n.group_id===operation.id).length);
      assert.equal(await page.locator('.node').count(),model.nodes.length,'All nodes must stay on the map');
      await page.keyboard.press('Escape');
      await page.locator('.edge').first().focus();
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('#inspector').isVisible(), true);
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
      console.log(`PASS ${width}x${height}: compact header, all nodes, stage framing, operation highlighting across AS, keyboard, zoom`);
      await page.close();
    }
  } finally {
    await browser.close();
  }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
