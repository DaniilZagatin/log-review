"""Report tabs around the scenario map: markup, Horizon styles and controller.

The map stays laid out while another tab covers it, so its geometry, zoom,
selection and inspector survive tab switches without redrawing. The operation
card is not extended: tables link to the map through the existing selection.
All report text is inserted with textContent.
"""

REPORT_TABS = (
    '<div class="report-bar"><div class="tabs" id="report-tabs" role="tablist" aria-label="Разделы отчёта">'
    '<button type="button" role="tab" id="tab-map" data-tab="map" aria-controls="panel-map" aria-selected="true">Карта</button>'
    '<button type="button" role="tab" id="tab-logging" data-tab="logging" aria-controls="panel-logging" aria-selected="false" tabindex="-1">Логирование</button>'
    '<button type="button" role="tab" id="tab-basis" data-tab="basis" aria-controls="panel-basis" aria-selected="false" tabindex="-1">Основания и ограничения</button>'
    '</div><button type="button" id="report-return" class="text-button" hidden></button>'
    '<p id="report-notice" class="report-notice" role="status" hidden></p></div>'
)

REPORT_PANELS = (
    '<section id="panel-logging" class="report-panel" role="tabpanel" aria-labelledby="tab-logging" tabindex="0" hidden>'
    '<h2>Логирование</h2><p class="report-lead" id="logging-lead"></p><dl class="report-stats" id="logging-stats"></dl>'
    '<div class="report-controls"><label>Связь с операцией <select id="logging-filter">'
    '<option value="all">Все места</option><option value="linked">Связано с операцией</option>'
    '<option value="ambiguous">Несколько операций</option><option value="unlinked">Вне оснований операций</option>'
    '</select></label><label>Поиск <input id="logging-search" type="search" placeholder="Файл, ключ, операция"></label></div>'
    '<div class="report-table-wrap"><table class="report-table"><thead><tr><th scope="col">Место в коде</th>'
    '<th scope="col">Вызов</th><th scope="col">Ключ события (кандидат)</th><th scope="col">Поля в вызове</th>'
    '<th scope="col">Операция карты</th><th scope="col">Прочитано исследователем</th></tr></thead>'
    '<tbody id="logging-rows"></tbody></table></div><p class="muted" id="logging-empty" hidden>Мест логирования по выбранным условиям нет.</p>'
    '<div class="report-pager"><button type="button" id="logging-prev">Назад</button><span id="logging-page"></span>'
    '<button type="button" id="logging-next">Далее</button></div>'
    '<h3>Ограничения каталога</h3><ul class="report-list" id="logging-limitations"></ul>'
    '<details class="details" id="logging-hints-block"><summary>Подсказки конфигурации и доставки</summary><ul class="report-list" id="logging-hints"></ul></details>'
    '</section>'
    '<section id="panel-basis" class="report-panel" role="tabpanel" aria-labelledby="tab-basis" tabindex="0" hidden>'
    '<h2>Основания и ограничения</h2><dl class="report-fields" id="basis-run"></dl>'
    '<h3>Состояние этапов</h3><div class="report-table-wrap"><table class="report-table"><thead><tr>'
    '<th scope="col">Этап</th><th scope="col">Выполнение</th><th scope="col">Результат</th><th scope="col">Причины</th>'
    '</tr></thead><tbody id="basis-stages"></tbody></table></div>'
    '<h3>Происхождение выводов</h3><ul class="report-list" id="basis-provenance"></ul>'
    '<h3>Что не выполнялось</h3><ul class="report-list" id="basis-not-run"></ul>'
    '</section>'
)

REPORT_STYLE = r'''
.report-bar{display:flex;align-items:center;flex-wrap:wrap;gap:8px 16px;margin-top:8px}
.report-bar .tabs{display:flex;gap:2px;width:fit-content}
.report-bar .tabs button{min-height:36px;font-size:13px;white-space:nowrap}
.report-notice{margin:0;padding:4px 12px;border-radius:var(--radius-lg);background:var(--warning-soft);color:var(--warning);font-size:13px}
.report-panel{position:absolute;inset:0;z-index:8;overflow:auto;background:var(--bg);padding:16px 24px 40px}
.report-panel[hidden]{display:none}
.report-panel h2{font-size:20px;font-weight:500;letter-spacing:-.02em;margin:0 0 8px}
.report-panel h3{font-size:15px;font-weight:500;margin:24px 0 8px}
.report-lead{max-width:960px;margin:0 0 16px;color:var(--ink-secondary);font-size:13px;line-height:1.5}
.report-stats{display:flex;flex-wrap:wrap;gap:8px 28px;margin:0 0 16px}
.report-stats div{display:flex;align-items:baseline;gap:6px}
.report-stats dt{order:2;font-size:12px;color:var(--ink-secondary)}
.report-stats dd{margin:0;font-size:20px;font-weight:300}
.report-fields{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:6px 16px;margin:0;font-size:13px}
.report-fields dt{color:var(--ink-secondary)}.report-fields dd{margin:0;overflow-wrap:anywhere}
.report-controls{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;margin-bottom:8px;font-size:13px;color:var(--ink-secondary)}
.report-controls label{display:flex;align-items:center;gap:8px}
.report-controls select,.report-controls input{min-height:38px;border:0;border-radius:var(--radius-pill);background:var(--bg-subtle);color:var(--ink);padding:6px 14px;font-size:13px}
.report-controls input{width:260px;max-width:100%}
.report-table-wrap{overflow-x:auto;border:1px solid var(--line-default);border-radius:var(--radius-lg)}
.report-table{width:100%;border-collapse:collapse;font-size:13px;line-height:1.4}
.report-table th{position:sticky;top:0;background:var(--bg-subtle);color:var(--ink-secondary);font-weight:500;text-align:left;padding:8px 12px}
.report-table td{padding:8px 12px;border-top:1px solid var(--line-default);vertical-align:top;overflow-wrap:break-word;min-width:96px;max-width:360px}
.report-table code{font-family:var(--font-mono);font-size:12px;background:var(--bg-subtle);border-radius:6px;padding:1px 5px}
.report-table .text-button{display:block;min-height:32px;margin-top:2px;font-weight:500}
.report-table .muted{font-size:12px}
.report-pager{display:flex;align-items:center;gap:12px;margin-top:8px;font-size:13px}
.report-pager button{min-height:36px;border:0;border-radius:var(--radius-pill);background:var(--bg-subtle);color:var(--ink);padding:6px 16px}
.report-list{max-width:960px;margin:0;padding-left:20px;font-size:13px;color:var(--ink-secondary)}
.report-list li{margin:4px 0;overflow-wrap:anywhere}
@media(max-width:700px){.report-bar .tabs{width:100%}.report-bar .tabs button{flex:1;min-width:0;padding:6px 8px;white-space:normal}.report-panel{padding:12px 16px 32px}.report-fields{grid-template-columns:1fr}}
'''

REPORT_SCRIPT = r'''
const report=JSON.parse($('process-report').textContent);
const logging=report.logging||{},sites=array(logging.sites),tabs=[...document.querySelectorAll('#report-tabs [role=tab]')];
const panels={map:$('panel-map'),logging:$('panel-logging'),basis:$('panel-basis')};
const tabTitles={map:'Карта',logging:'Логирование',basis:'Основания и ограничения'};
let activeTab='map',returnTo=null;const panelScroll={};
$('panel-map').setAttribute('role','tabpanel');$('panel-map').setAttribute('aria-labelledby','tab-map');
function notice(message){$('report-notice').textContent=message||'';$('report-notice').hidden=!message;if(message)announce(message);}
function writeHash(value){try{history.replaceState(null,'','#'+value);}catch(error){}}
function setTab(name,focus=false){
 if(!panels[name])name='map';
 if(activeTab!=='map')panelScroll[activeTab]=panels[activeTab].scrollTop;
 activeTab=name;const onMap=name==='map';
 for(const tab of tabs){const on=tab.dataset.tab===name;tab.setAttribute('aria-selected',String(on));tab.tabIndex=on?0:-1;if(on&&focus)tab.focus();}
 for(const [key,panel] of Object.entries(panels))if(key!=='map')panel.hidden=key!==name;
 // The map is covered, not hidden: its measured layout and view stay intact.
 for(const id of ['panel-map','inspector']){const node=$(id);node.inert=!onMap;if(onMap)node.removeAttribute('aria-hidden');else node.setAttribute('aria-hidden','true');}
 if(!onMap){closePopovers();panels[name].scrollTop=panelScroll[name]||0;}
 $('report-return').hidden=!(onMap&&returnTo);
}
for(const tab of tabs){
 tab.addEventListener('click',()=>{returnTo=null;notice('');setTab(tab.dataset.tab);writeHash('tab='+tab.dataset.tab);});
 tab.addEventListener('keydown',event=>{const index=tabs.indexOf(tab);let next=null;
  if(event.key==='ArrowRight')next=tabs[(index+1)%tabs.length];else if(event.key==='ArrowLeft')next=tabs[(index-1+tabs.length)%tabs.length];
  else if(event.key==='Home')next=tabs[0];else if(event.key==='End')next=tabs[tabs.length-1];
  if(next){event.preventDefault();returnTo=null;setTab(next.dataset.tab,true);writeHash('tab='+next.dataset.tab);}});
}
function showOnMap(operationId,rowId){
 const node=byId.get(text(operationId));
 if(!node){notice('Операция «'+text(operationId)+'» не найдена на карте.');return;}
 returnTo={tab:activeTab,rowId};notice('');setTab('map');writeHash('tab=map&node='+encodeURIComponent(text(operationId)));
 $('report-return').textContent='← Вернуться: '+tabTitles[returnTo.tab];selectNode(node,true);
}
$('report-return').addEventListener('click',()=>{
 const target=returnTo;returnTo=null;if(!target)return;setTab(target.tab);writeHash('tab='+target.tab);
 const row=target.rowId&&document.getElementById(target.rowId),button=row?.querySelector('button');
 if(row)row.scrollIntoView({block:'nearest'});(button||tabs.find(tab=>tab.dataset.tab===target.tab)).focus({preventScroll:true});
});
function applyHash(){
 const params=new URLSearchParams(location.hash.slice(1)),tab=params.get('tab'),nodeId=params.get('node');
 if(!tab&&!nodeId)return;
 returnTo=null;notice(tab&&!panels[tab]?'Раздел «'+tab+'» не найден, показана карта.':'');setTab(tab&&panels[tab]?tab:'map');
 if(nodeId){const node=byId.get(nodeId);if(node){setTab('map');selectNode(node,true);}else notice('Операция «'+nodeId+'» не найдена на карте.');}
}
window.addEventListener('hashchange',applyHash);

function linkLabel(site){
 const explained=site.link_basis==='code_explanation';
 return {linked:explained?'В функции, которую поясняет операция':'В цитате основания операции',
  ambiguous:explained?'В функции нескольких операций':'В основаниях нескольких операций',
  unlinked:'Вне оснований операций'}[site.link_status]||'Связь не оценивалась';
}
const keyNames={literal:'текст в вызове',resolved_constant:'значение константы',symbolic:'имя переменной, значение не установлено',dynamic:'вычисляется при исполнении',unparsed:'не разобран'};
const callNames={log_call:'Запись в лог',generic_emit:'Публикация события',configured_call:'Настроенный вызов',wrapper_call:'Вызов обёртки логирования'};
const pageSize=50;let siteFilter='all',siteQuery='',sitePage=0;
const counts=logging.counts||{},scope=logging.scope||{};
$('logging-lead').textContent='Места логирования найдены статическим поиском в файлах, которые прочитал исследователь. Связь с операцией установлена по расположению: вызов находится внутри цитаты её основания, а если такой цитаты нет — внутри функции из её пояснения. Смысл события, момент фиксации и связь с экземпляром процесса не установлены. Покрытие процесса не рассчитывается, пока не задан перечень необходимых фактов.';
for(const [label,value] of [['файлов просмотрено',scope.files_scanned],['мест логирования',sites.length],['связано с операциями',counts.linked],['операций с местами логирования',Number.isFinite(counts.operations_with_sites)?counts.operations_with_sites+' из '+counts.operations:undefined],['в основаниях нескольких операций',counts.ambiguous],['вне оснований операций',counts.unlinked]]){
 if(value===undefined||value===null)continue;const row=el('div');row.append(el('dt','',label),el('dd','',value));$('logging-stats').append(row);
}
function siteText(site){return [site.path,site.event_key,site.callee,site.symbol,...array(site.operation_ids).map(id=>byId.get(text(id))?.name||id)].map(value=>text(value).toLowerCase()).join(' ');}
function renderSites(){
 const rows=sites.filter(site=>(siteFilter==='all'||site.link_status===siteFilter)&&(!siteQuery||siteText(site).includes(siteQuery)));
 const pages=Math.max(1,Math.ceil(rows.length/pageSize));sitePage=Math.min(sitePage,pages-1);
 const body=$('logging-rows');body.replaceChildren();
 for(const site of rows.slice(sitePage*pageSize,(sitePage+1)*pageSize)){
  const tr=el('tr');tr.id='site-'+text(site.id);
  const place=el('td');place.append(el('div','',text(site.path)+':'+site.line_start+(site.line_end!==site.line_start?'–'+site.line_end:'')));if(site.symbol)place.append(el('div','muted',site.symbol));
  const call=el('td');call.append(el('div','',callNames[site.kind]||site.kind),el('code','',site.callee));
  const key=el('td');if(site.event_key)key.append(el('code','',site.event_key));key.append(el('div','muted',keyNames[site.key_status]||site.key_status));
  const fields=el('td',array(site.fields).length?'':'muted',array(site.fields).length?array(site.fields).join(', '):'Не найдены');
  if(site.fields_status==='partial'&&array(site.fields).length)fields.append(el('div','muted','Перечень может быть неполным'));
  const operation=el('td');operation.append(el('div','',linkLabel(site)));
  for(const id of array(site.operation_ids)){const node=byId.get(text(id)),button=el('button','text-button','Показать на карте: '+(node?.name||id));button.type='button';button.addEventListener('click',()=>showOnMap(id,tr.id));operation.append(button);}
  tr.append(place,call,key,fields,operation,el('td','',site.read_by_researcher?'Да':'Нет'));body.append(tr);
 }
 $('logging-empty').hidden=rows.length>0;
 $('logging-page').textContent=rows.length?'Строки '+(sitePage*pageSize+1)+'–'+Math.min(rows.length,(sitePage+1)*pageSize)+' из '+rows.length:'';
 $('logging-prev').disabled=sitePage===0;$('logging-next').disabled=sitePage>=pages-1;
}
$('logging-filter').addEventListener('change',event=>{siteFilter=event.target.value;sitePage=0;renderSites();});
$('logging-search').addEventListener('input',event=>{siteQuery=event.target.value.trim().toLowerCase();sitePage=0;renderSites();});
$('logging-prev').addEventListener('click',()=>{sitePage=Math.max(0,sitePage-1);renderSites();});
$('logging-next').addEventListener('click',()=>{sitePage+=1;renderSites();});
renderSites();
for(const item of array(logging.limitations))$('logging-limitations').append(el('li','',item));
if(!array(logging.limitations).length)$('logging-limitations').append(el('li','','Каталог мест логирования не построен.'));
const hints=array(logging.config_hints);$('logging-hints-block').hidden=!hints.length;
for(const hint of hints){const item=el('li');item.append(el('code','',text(hint.path)+':'+hint.line_start),document.createTextNode(' '+text(hint.text)));$('logging-hints').append(item);}

const executionNames={not_run:'Не запускался',running:'Выполняется',completed:'Выполнен',failed:'Ошибка',blocked:'Заблокирован зависимостью',interrupted:'Прерван'};
const resultNames={none:'Нет результата',complete:'Полный',partial:'Частичный',stale:'Устарел'};
const runNames={completed:'Исследование завершено',partial:'Частичная карта',failed:'Исследование не выполнено'};
for(const [label,value] of [['Карта',runNames[report.map_status]||report.map_status||'Состояние неизвестно'],['Ревизия карты',report.map_revision],['Запуск',report.run_id],['Версия отчёта',report.report_version]]){
 if(!value)continue;$('basis-run').append(el('dt','',label),el('dd','',value));
}
for(const stage of array(report.stages)){
 const tr=el('tr');const reasons=el('td');
 for(const reason of array(stage.reasons))reasons.append(el('div','',reason));if(!array(stage.reasons).length)reasons.append(el('span','muted','—'));
 tr.append(el('td','',stage.title||stage.id),el('td','',executionNames[stage.execution_status]||stage.execution_status),el('td','',resultNames[stage.result_status]||stage.result_status),reasons);
 $('basis-stages').append(tr);
}
for(const item of ['Операции и связи карты подтверждены прочитанным кодом. Отметка «Подтверждено кодом» не означает, что исполнение наблюдалось в работающей системе.',
 'Места логирования — статические кандидаты детектора: включённость логирования, достижимость вызова и доставка записи не установлены.',
 'Связь места логирования с операцией установлена по расположению вызова: в цитате основания операции, иначе в функции из её пояснения. Роль события в жизненном цикле операции не определялась.'])$('basis-provenance').append(el('li','',item));
for(const item of ['Перечень необходимых бизнес-фактов и оценка покрытия.','Предложения доработок логирования.','Определения метрик.','Профиль реальных логов: файл логов не передавался.'])$('basis-not-run').append(el('li','',item));
applyHash();
'''
