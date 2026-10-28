"""Report tabs around the scenario map: markup, Horizon styles and controller.

The map stays laid out while another tab covers it, so its geometry, zoom,
selection and inspector survive tab switches without redrawing. The operation
card is not extended: tables link to the map through the existing selection.
All report text is inserted with textContent; every status is spelled out.
"""

_TABS = (("map", "Карта"), ("logging", "Логирование"), ("changes", "Доработки"),
         ("metrics", "Метрики"), ("basis", "Основания и ограничения"))

def _tab(key: str, label: str) -> str:
    selected = key == "map"
    return (f'<button type="button" role="tab" id="tab-{key}" data-tab="{key}" aria-controls="panel-{key}" '
            f'aria-selected="{str(selected).lower()}"' + ('' if selected else ' tabindex="-1"') + f'>{label}</button>')


REPORT_TABS = (
    '<div class="report-bar"><div class="tabs" id="report-tabs" role="tablist" aria-label="Разделы отчёта">'
    + "".join(_tab(key, label) for key, label in _TABS)
    + '</div><button type="button" id="report-return" class="text-button" hidden></button>'
    '<p id="report-notice" class="report-notice" role="status" hidden></p></div>'
)

REPORT_PANELS = "".join(
    f'<section id="panel-{key}" class="report-panel" role="tabpanel" aria-labelledby="tab-{key}" tabindex="0" hidden>'
    f'<h2>{label}</h2><div id="{key}-content"></div></section>'
    for key, label in _TABS if key != "map")

REPORT_STYLE = r'''
.report-bar{display:flex;align-items:center;flex-wrap:wrap;gap:8px 16px;margin-top:8px;min-width:0}
.report-bar .tabs{display:flex;gap:2px;width:fit-content;max-width:100%;overflow-x:auto;scrollbar-width:thin}
.report-bar .tabs button{min-height:36px;font-size:13px;white-space:nowrap;flex:none}
.report-notice{margin:0;padding:4px 12px;border-radius:var(--radius-lg);background:var(--warning-soft);color:var(--warning);font-size:13px}
.report-panel{position:absolute;inset:0;z-index:8;overflow:auto;background:var(--bg);padding:16px 24px 48px}
.report-panel[hidden]{display:none}
.report-panel h2{font-size:20px;font-weight:500;letter-spacing:-.02em;margin:0 0 8px}
.report-panel h3{font-size:15px;font-weight:500;margin:28px 0 8px}
.report-panel h4{font-size:14px;font-weight:500;margin:16px 0 6px}
.report-lead{max-width:960px;margin:0 0 16px;color:var(--ink-secondary);font-size:13px;line-height:1.5}
.report-stats{display:flex;flex-wrap:wrap;gap:8px 28px;margin:0 0 16px}
.report-stats div{display:flex;align-items:baseline;gap:6px}
.report-stats dt{order:2;font-size:12px;color:var(--ink-secondary)}
.report-stats dd{margin:0;font-size:20px;font-weight:300}
.report-fields{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:6px 16px;margin:0;font-size:13px;max-width:1100px}
.report-fields dt{color:var(--ink-secondary)}.report-fields dd{margin:0;overflow-wrap:break-word}
.report-controls{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;margin-bottom:8px;font-size:13px;color:var(--ink-secondary)}
.report-controls label{display:flex;align-items:center;gap:8px}
.report-controls select,.report-controls input{min-height:38px;border:0;border-radius:var(--radius-pill);background:var(--bg-subtle);color:var(--ink);padding:6px 14px;font-size:13px}
.report-controls input{width:260px;max-width:100%}
.report-table-wrap{overflow-x:auto;border:1px solid var(--line-default);border-radius:var(--radius-lg)}
.report-table{width:100%;border-collapse:collapse;font-size:13px;line-height:1.4}
.report-table th{position:sticky;top:0;background:var(--bg-subtle);color:var(--ink-secondary);font-weight:500;text-align:left;padding:8px 12px;vertical-align:bottom}
.report-table td{padding:8px 12px;border-top:1px solid var(--line-default);vertical-align:top;overflow-wrap:break-word;min-width:96px;max-width:360px}
.report-table code,.report-card code{font-family:var(--font-mono);font-size:12px;background:var(--bg-subtle);border-radius:6px;padding:1px 5px}
.report-table .text-button,.report-card .text-button{display:block;min-height:32px;margin-top:2px;font-weight:500}
.report-table .muted,.report-card .muted{font-size:12px}
.report-gap{color:var(--warning)}
.report-table td.wide{min-width:240px}
.report-quote{margin-top:6px}.report-quote summary{font-size:12px;color:var(--accent-ink);cursor:pointer}
.report-quote cite{display:block;font-style:normal;font-size:12px;color:var(--ink-secondary);margin-top:6px}
.report-quote pre{margin:4px 0 0;padding:8px 10px;background:var(--bg-subtle);border-radius:8px;font-family:var(--font-mono);font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere;max-width:640px}
.report-pager{display:flex;align-items:center;gap:12px;margin-top:8px;font-size:13px}
.report-pager button{min-height:36px;border:0;border-radius:var(--radius-pill);background:var(--bg-subtle);color:var(--ink);padding:6px 16px}
.report-list{max-width:960px;margin:0;padding-left:20px;font-size:13px;color:var(--ink-secondary)}
.report-list li{margin:4px 0;overflow-wrap:break-word}
.report-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,420px),1fr));gap:16px}
.report-card{border:1px solid var(--line-default);border-radius:var(--radius-lg);padding:16px 18px;font-size:13px;line-height:1.45}
.report-card .report-fields{grid-template-columns:minmax(120px,max-content) minmax(0,1fr)}
.report-card ul{margin:4px 0 0;padding-left:18px}
.report-card h4{margin:16px 0 6px}.report-card h4:first-child{margin-top:0}.report-card>p{margin:0 0 10px}
.report-empty{color:var(--ink-secondary);font-size:13px}
@media(max-width:700px){.report-bar .tabs{width:100%}.report-panel{padding:12px 16px 32px}.report-fields,.report-card .report-fields{grid-template-columns:1fr}}
'''

REPORT_SCRIPT = r'''
const report=JSON.parse($('process-report').textContent);
const logging=report.logging||{},sites=array(logging.sites),tabs=[...document.querySelectorAll('#report-tabs [role=tab]')];
const panels={map:$('panel-map')};for(const tab of tabs)if(tab.dataset.tab!=='map')panels[tab.dataset.tab]=$('panel-'+tab.dataset.tab);
const tabTitles=Object.fromEntries(tabs.map(tab=>[tab.dataset.tab,tab.textContent]));
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
 $('report-return').textContent='← Вернуться: '+tabTitles[returnTo.tab];setMode('atomic');selectNode(node,true);
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
 if(nodeId){const node=byId.get(nodeId);if(node){setTab('map');setMode('atomic');selectNode(node,true);}else notice('Операция «'+nodeId+'» не найдена на карте.');}
}
window.addEventListener('hashchange',applyHash);
mapTabHook=()=>{returnTo={tab:activeTab,rowId:null};notice('');setTab('map');writeHash('tab=map');$('report-return').textContent='← Вернуться: '+tabTitles[returnTo.tab];};

const L=(names,value)=>names[value]||text(value)||'—';
const stageById=new Map(array(report.stages).map(row=>[row.id,row]));
const roleNames={request:'Запрос',queued:'Постановка в очередь',start:'Начало работы',result:'Результат',refusal:'Отказ по правилу',error:'Ошибка исполнения',retry:'Повтор попытки',duplicate:'Дубль или повторная доставка',cancellation:'Отмена',compensation:'Компенсация',state_change:'Изменение состояния',diagnostic:'Служебная запись',unknown:'Роль не установлена'};
const momentNames={before_action:'до действия',after_action:'после действия',before_commit:'до сохранения',after_commit:'после сохранения',on_response:'при ответе',on_failure:'при сбое',unknown:'момент не установлен'};
const repeatNames={once_per_execution:'одна запись на выполнение',per_attempt:'запись на каждую попытку',per_delivery:'может повториться при доставке',unknown:'повторяемость не установлена'};
const reviewNames={confirmed:'Смысл подтверждён по строкам кода',no_operation:'Служебная запись, не бизнес-факт',unresolved:'Смысл не установлен',conflict:'Противоречивые интерпретации',rejected:'Ответ модели отклонён проверками',deferred:'Не рассмотрено: исчерпан лимит',not_reviewed:'Смысл не рассматривался'};
const keyNames={literal:'текст в вызове',resolved_constant:'значение константы',symbolic:'имя переменной, значение не установлено',dynamic:'вычисляется при исполнении',unparsed:'не разобран'};
const callNames={log_call:'Запись в лог',generic_emit:'Публикация события',configured_call:'Настроенный вызов',wrapper_call:'Вызов обёртки логирования'};
const axisTitles={emit:'Запись',semantics:'Смысл',fields:'Поля',correlation:'Экземпляр',timing:'Момент',time_source:'Время',runtime:'Данные выполнения',quality:'Качество данных'};
const axisNames={found:'Запись найдена',not_found:'Записи нет',unexplained_candidates:'Есть нерассмотренные места',outside_application:'Вне приложения',
 compatible:'Соответствует границе',incompatible:'Не соответствует границе',available:'Есть',partial:'Частично',missing:'Нет',not_required:'Не нужно',
 identified:'Идентификатор есть',consistent:'Соответствует',premature:'Запись раньше результата',event_time_field:'Время события в поле',record_time:'Только время записи',
 not_provided:'Логи не переданы',records_present:'В файле есть записи с этим ключом',no_records:'В файле нет записей с этим ключом',key_unknown:'Ключ не установлен',
 not_assessed:'Не оценивалось',no_issues_found:'Нарушений не найдено',issues:'Есть нарушения',unknown:'Не установлено',not_applicable:'—'};
const gapValues=new Set(['not_found','incompatible','missing','partial','premature','no_records','issues']);
const actionNames={map_existing:'Закрепить существующую запись',join:'Соединить данные',enrich:'Обогатить запись',add_field:'Добавить поле',add_event:'Добавить запись',move_emit:'Перенести запись',fix_delivery:'Исправить доставку'};
const keyStatusNames={proposed:'предложен, не согласован',existing:'существующий',not_set:'не задан'};
const timingNames={before_commit:'до сохранения',after_commit:'после сохранения',after_response:'после ответа',on_failure:'при сбое',unknown:'не установлен'};
const unitNames={case:'экземпляр процесса',operation_execution:'выполнение операции',attempt:'попытка',event:'запись',other:'другое'};
const readinessNames={proposed:'Предложена, не согласована',available:'Все факты наблюдаются в коде',incomplete:'Есть неопределённые требования',specified:'Правила и границы указаны',established:'Установлено для всех фактов',gaps:'Есть пробелы',
 not_provided:'Данные не переданы',sample_only:'Только выборка',not_implemented:'Не реализовано'};
const componentTitles={methodology:'Методика',definition:'Правила и границы определения',facts:'Факты',semantics:'Смысл записей',correlation:'Связь с экземпляром',timing:'Момент и время',fields:'Поля',data:'Данные',calculator:'Вычисление'};
const familyNames={volume:'объём',duration:'длительность',outcome:'исходы',quality:'качество',cost:'стоимость',other:'другое'};
const sourceNames={scenario:'описание процесса',code:'код',proposal:'предложение',hypothesis:'гипотеза',map:'карта'};
const executionNames={not_run:'Не запускался',running:'Выполняется',completed:'Выполнен',failed:'Ошибка',blocked:'Заблокирован зависимостью',interrupted:'Прерван лимитом'};
const resultNames={none:'Нет результата',complete:'Полный',partial:'Частичный',stale:'Устарел'};
const facts=array(report.facts),factById=new Map(facts.map(fact=>[fact.id,fact]));
const coverageById=new Map(array(report.coverage?.facts).map(row=>[row.fact_id,row]));
const bindingById=new Map(array(logging.event_bindings).map(row=>[row.id,row]));

function cell(content,cls){const td=el('td',cls);if(content instanceof Node)td.append(content);else td.textContent=text(content)||'—';return td;}
function block(...children){const div=el('div');for(const child of children)if(child)div.append(typeof child==='string'?el('div','',child):child);return div;}
function opButtons(container,ids,rowId){for(const id of array(ids)){const node=byId.get(text(id)),button=el('button','text-button',(node?.name||id)+' →');button.type='button';button.setAttribute('aria-label','Показать на карте: '+(node?.name||id));button.title='Показать на карте';button.addEventListener('click',()=>showOnMap(id,rowId));container.append(button);}}
function quotes(container,refs,label='Строки кода'){const list=array(refs).filter(ref=>ref&&ref.path);if(!list.length)return;const details=el('details','report-quote');details.append(el('summary','',label));
 for(const ref of list){details.append(el('cite','',ref.path+':'+ref.line_start+(ref.line_end!==ref.line_start?'–'+ref.line_end:'')));if(ref.quote)details.append(el('pre','',ref.quote));}container.append(details);}
function stats(container,pairs){const dl=el('dl','report-stats');for(const [label,value] of pairs){if(value===undefined||value===null)continue;const row=el('div');row.append(el('dt','',label),el('dd','',value));dl.append(row);}container.append(dl);}
function fields(container,pairs){const dl=el('dl','report-fields');for(const [label,value] of pairs){if(value===undefined||value===null||value==='')continue;dl.append(el('dt','',label));const dd=el('dd');if(value instanceof Node)dd.append(value);else dd.textContent=text(value);dl.append(dd);}container.append(dl);return dl;}
function list(container,items,empty){const ul=el('ul','report-list');for(const item of array(items))ul.append(el('li','',item));if(!ul.children.length&&empty)ul.append(el('li','',empty));container.append(ul);}
function stageState(container,ids){for(const id of ids){const row=stageById.get(id);if(!row||(row.execution_status==='completed'&&row.result_status==='complete'))continue;
 const p=el('p','report-notice',(row.title||id)+': '+L(executionNames,row.execution_status)+(row.result_status!=='none'?', результат '+L(resultNames,row.result_status).toLowerCase():'')+(array(row.reasons).length?'. '+array(row.reasons).join(' '):''));p.style.margin='0 0 12px';container.append(p);}}
function table(container,headers){const wrap=el('div','report-table-wrap'),t=el('table','report-table'),head=el('thead'),tr=el('tr');for(const label of headers){const th=el('th','',label);th.scope='col';tr.append(th);}head.append(tr);const body=el('tbody');t.append(head,body);wrap.append(t);container.append(wrap);return body;}
function pager(container,render){const bar=el('div','report-pager'),prev=el('button','','Назад'),label=el('span'),next=el('button','','Далее');prev.type=next.type='button';bar.append(prev,label,next);container.append(bar);
 const state={page:0,size:50};prev.addEventListener('click',()=>{state.page=Math.max(0,state.page-1);render();});next.addEventListener('click',()=>{state.page+=1;render();});
 state.update=total=>{const pages=Math.max(1,Math.ceil(total/state.size));state.page=Math.min(state.page,pages-1);label.textContent=total?'Строки '+(state.page*state.size+1)+'–'+Math.min(total,(state.page+1)*state.size)+' из '+total:'';prev.disabled=state.page===0;next.disabled=state.page>=pages-1;return [state.page*state.size,(state.page+1)*state.size];};return state;}
function axisCell(row,axis){const value=row?.axes?.[axis];return cell(L(axisNames,value),gapValues.has(value)?'report-gap':value==='unknown'||value==='unexplained_candidates'||value==='key_unknown'?'muted':'');}

// Logging: facts first, because they say why a record matters.
(()=>{const root=$('logging-content'),counts=logging.counts||{},scope=logging.scope||{},summary=report.coverage?.summary;
 root.append(el('p','report-lead','Места логирования найдены статическим поиском в файлах, которые прочитал исследователь. Связь с операцией установлена по расположению вызова; смысл записи определён по выданным строкам кода и проверен программно. Необходимые факты и их покрытие — предложения: перечень не согласован, поэтому общий процент покрытия не рассчитывается.'));
 stageState(root,['logging_inventory','event_operation_mapping','event_binding_review','fact_requirements','logging_coverage']);
 stats(root,[['файлов просмотрено',scope.files_scanned],['мест логирования',sites.length],['связано с операциями по расположению',counts.linked],
  ['смысл подтверждён',array(logging.event_bindings).filter(row=>row.status==='confirmed').length],['необходимых фактов',summary?.facts],
  ['наблюдаются в коде',summary?.observable_in_code],['с пробелами',summary?.with_gaps],['с неизвестными',summary?.with_unknowns]]);
 root.append(el('h3','','Необходимые факты и покрытие'));
 if(!facts.length)root.append(el('p','report-empty','Перечень необходимых фактов не построен.'));
 else{const body=table(root,['Факт','Операции','Граница',...Object.values(axisTitles)]);
  for(const fact of facts){const row=coverageById.get(fact.id),tr=el('tr');tr.id='fact-'+fact.id;
   const name=block(el('div','',fact.id+' · '+fact.meaning));if(fact.applicability!=='applicable')name.append(el('div','muted',fact.applicability==='conditional'?'При условии: '+fact.condition:'Применимость не установлена'));
   const ops=el('td');if(fact.outside_application)ops.append(el('div','muted','Вне приложения'));opButtons(ops,fact.operation_ids,tr.id);
   tr.append(cell(name),ops,cell(L(roleNames,fact.lifecycle)),...Object.keys(axisTitles).map(axis=>axisCell(row,axis)));body.append(tr);}
  const requested=array(report.requested_facts);if(requested.length){root.append(el('h4','','Факты, которые запросили определения метрик'));list(root,requested.map(fact=>fact.meaning+' — '+L(roleNames,fact.lifecycle)+' (метрика '+fact.requested_by+', '+(fact.status==='assessed'?'оценён: '+fact.fact_id:'не оценивался')+')'));}}
 root.append(el('h3','','Места логирования'));
 const controls=el('div','report-controls'),statusLabel=el('label','','Смысл записи '),statusSelect=el('select'),searchLabel=el('label','','Поиск '),search=el('input');
 for(const [value,label] of [['all','Все места'],...Object.entries(reviewNames)]){const option=el('option','',label);option.value=value;statusSelect.append(option);}
 search.type='search';search.placeholder='Файл, ключ, операция';statusLabel.append(statusSelect);searchLabel.append(search);controls.append(statusLabel,searchLabel);root.append(controls);
 const body=table(root,['Место в коде','Вызов','Ключ (кандидат)','Поля в вызове','Смысл записи','Операция карты']),empty=el('p','report-empty','Мест логирования по выбранным условиям нет.');root.append(empty);
 const bindingsBySite=new Map();for(const binding of array(logging.event_bindings)){if(!bindingsBySite.has(binding.site_id))bindingsBySite.set(binding.site_id,[]);bindingsBySite.get(binding.site_id).push(binding);}
 let status='all',query='';
 const siteText=site=>[site.path,site.event_key,site.callee,site.symbol,...array(site.operation_ids).map(id=>byId.get(text(id))?.name||id),...array(bindingsBySite.get(site.id)).map(row=>row.business_meaning)].map(value=>text(value).toLowerCase()).join(' ');
 const pages=pager(root,render);
 function render(){const rows=sites.filter(site=>(status==='all'||(site.review_status||'not_reviewed')===status)&&(!query||siteText(site).includes(query)));
  const [from,to]=pages.update(rows.length);body.replaceChildren();empty.hidden=rows.length>0;
  for(const site of rows.slice(from,to)){const tr=el('tr');tr.id='site-'+text(site.id);
   const place=block(text(site.path)+':'+site.line_start+(site.line_end!==site.line_start?'–'+site.line_end:''));if(site.symbol)place.append(el('div','muted',site.symbol));place.append(el('div','muted',site.read_by_researcher?'Прочитано исследователем':'Не прочитано исследователем'));
   const call=block(callNames[site.kind]||site.kind,el('code','',site.callee));
   const key=el('div');if(site.event_key)key.append(el('code','',site.event_key));key.append(el('div','muted',L(keyNames,site.key_status)));
   const flds=block(array(site.fields).length?array(site.fields).join(', '):'Не найдены');if(array(site.context_fields).length)flds.append(el('div','muted','Из контекста MDC выше вызова: '+array(site.context_fields).join(', ')));
   if(site.fields_status==='partial'&&array(site.fields).length)flds.append(el('div','muted','Перечень может быть неполным'));
   const meaning=block(el('div','',L(reviewNames,site.review_status||'not_reviewed')));
   for(const reason of array(site.review_reasons))meaning.append(el('div','muted',reason));
   const operations=new Set();
   for(const binding of array(bindingsBySite.get(site.id))){
    if(binding.decision==='bind'||binding.decision==='no_operation')meaning.append(el('div','',L(roleNames,binding.event_role)+' · '+L(momentNames,binding.lifecycle_moment)+' · '+L(repeatNames,binding.repeat_semantics)));
    if(binding.business_meaning)meaning.append(el('div','',binding.business_meaning));if(binding.condition)meaning.append(el('div','muted','Условие: '+binding.condition));
    if(array(binding.case_fields).length)meaning.append(el('div','muted','Экземпляр процесса: '+array(binding.case_fields).join(', ')));
    if(binding.question)meaning.append(el('div','muted','Вопрос: '+binding.question));quotes(meaning,binding.evidence);if(binding.operation_id)operations.add(binding.operation_id);}
   const op=el('td');op.append(el('div','muted',{linked:'По расположению — одна операция',ambiguous:'По расположению — несколько операций',unlinked:'Вне оснований операций'}[site.link_status]||'Связь не оценивалась'));
   opButtons(op,operations.size?[...operations]:site.operation_ids,tr.id);
   tr.append(cell(place),cell(call),cell(key),cell(flds),cell(meaning,'wide'),op);body.append(tr);}}
 statusSelect.addEventListener('change',()=>{status=statusSelect.value;pages.page=0;render();});
 search.addEventListener('input',()=>{query=search.value.trim().toLowerCase();pages.page=0;render();});render();
 root.append(el('h3','','Ограничения каталога'));list(root,logging.limitations,'Каталог мест логирования не построен.');
 const hints=array(logging.config_hints);if(hints.length){const details=el('details','report-quote');details.append(el('summary','','Подсказки конфигурации и доставки логов'));const ul=el('ul','report-list');
  for(const hint of hints){const li=el('li');li.append(el('code','',text(hint.path)+':'+hint.line_start),document.createTextNode(' '+text(hint.text)));ul.append(li);}details.append(ul);root.append(details);}
})();

// Changes: a draft for developers; nothing is applied to the analysed code.
(()=>{const root=$('changes-content'),changes=array(report.instrumentation);
 root.append(el('p','report-lead','Минимальные доработки, после которых необходимые факты можно наблюдать. Это проект изменений для разработчиков: исходники не меняются, задачи никуда не отправляются. Ключи новых записей — предложения до согласования. Место изменения, которое не видно в прочитанном коде, остаётся неопределённым с задачей поиска.'));
 stageState(root,['instrumentation_plan']);
 stats(root,[['доработок',changes.length],['место определено',changes.filter(change=>change.location?.status==='anchored').length],['место не определено',changes.filter(change=>change.location?.status!=='anchored').length]]);
 if(!changes.length){root.append(el('p','report-empty',stageById.get('instrumentation_plan')?.execution_status==='completed'?'Доработки не потребовались или не предложены.':'Доработки не строились.'));return;}
 const controls=el('div','report-controls'),label=el('label','','Вид '),select=el('select');for(const [value,name] of [['all','Все виды'],...Object.entries(actionNames)]){const option=el('option','',name);option.value=value;select.append(option);}label.append(select);controls.append(label);root.append(controls);
 const body=table(root,['Доработка','Вид','Факты','Где изменить','Контракт записи','Зависит от','Проверка','Операции']);
 function render(){body.replaceChildren();for(const change of changes.filter(change=>select.value==='all'||change.action===select.value)){const tr=el('tr');tr.id='change-'+change.id;
  const what=block(el('div','',change.id+' · '+change.change),el('div','muted','Вопрос: '+change.question));if(change.why_existing_insufficient)what.append(el('div','muted','Почему не хватает существующих записей: '+change.why_existing_insufficient));
  if(change.existing_binding_id){const binding=bindingById.get(change.existing_binding_id);if(binding)what.append(el('div','muted','Существующая запись: '+binding.business_meaning));}
  const factsCell=block();for(const id of array(change.fact_ids))factsCell.append(el('div','',id+' · '+(factById.get(id)?.meaning||'')));
  const where=el('div');const location=change.location||{};
  if(location.status==='anchored'){where.append(el('div','',location.path+':'+location.line_start+(location.line_end!==location.line_start?'–'+location.line_end:'')));quotes(where,[location]);}
  else{where.append(el('div','report-gap','Место не определено'));if(location.search_task)where.append(el('div','muted','Найти: '+location.search_task));}
  const event=change.event||{},contract=el('div');
  if(event.key){contract.append(el('code','',event.key));contract.append(el('div','muted','Ключ '+L(keyStatusNames,event.key_status)));}else contract.append(el('div','muted','Ключ не задан'));
  contract.append(el('div','',L(roleNames,event.lifecycle)+' · '+L(timingNames,event.timing)));
  for(const field of array(event.fields))contract.append(el('div','',field.name+(field.type?' ('+field.type+')':'')+(field.purpose?' — '+field.purpose:'')+(field.source?'; источник: '+field.source:'')));
  if(event.identity)contract.append(el('div','muted','Экземпляр и попытка: '+event.identity));if(event.dedup)contract.append(el('div','muted','Повтор и дубль: '+event.dedup));
  const ops=el('td');opButtons(ops,change.operation_ids,tr.id);
  tr.append(cell(what,'wide'),cell(L(actionNames,change.action)),cell(factsCell),cell(where),cell(contract,'wide'),cell(array(change.depends_on).join(', ')||'—'),cell(change.validation,'wide'),ops);body.append(tr);}}
 select.addEventListener('change',render);render();
})();

// Metrics: definitions and reasons they are not ready; no values exist.
(()=>{const root=$('metrics-content'),metrics=array(report.metrics);
 root.append(el('p','report-lead','Определения метрик для вопросов измерения. Значения не вычисляются: методика должна быть согласована специалистом, а для расчёта нужны данные выполнения и вычислитель. Готовность разложена на причины.'));
 stageState(root,['metric_catalog']);
 if(!metrics.length){root.append(el('p','report-empty','Определения метрик не построены.'));return;}
 const cards=el('div','report-cards');root.append(cards);
 for(const metric of metrics){const card=el('article','report-card');card.id='metric-'+metric.id;card.append(el('h4','',metric.id+' · '+metric.name),el('p','',metric.purpose));
  if(metric.profile_direction)card.append(el('p','muted',metric.source==='profile'?'Направление профиля: определение взято из профиля, модель его не уточнила.':'Направление профиля: '+metric.profile_direction));
  const factList=ids=>array(ids).map(id=>id+' · '+(factById.get(id)?.meaning||'')).join('; ');
  fields(card,[['Определение',metric.definition],['Единица счёта',L(unitNames,metric.counting_unit)],['Единица измерения',metric.measure_unit],['Какие экземпляры',metric.population],['Период',metric.window],
   ['Начало интервала',metric.start_fact_id?factList([metric.start_fact_id]):''],['Конец интервала',metric.finish_fact_id?factList([metric.finish_fact_id]):''],
   ['Незавершённые',metric.rules?.open_cases],['Поздние записи',metric.rules?.late_events],['Повторы попыток',metric.rules?.retries],['Повторные доставки',metric.rules?.duplicates],
   ['Факты',factList(metric.fact_ids)],['Доработки',array(metric.change_ids).join(', ')],['Значение','Не вычисляется']]);
  const readiness=metric.readiness||{},components=el('dl','report-fields');for(const [key,title] of Object.entries(componentTitles)){components.append(el('dt','',title),el('dd','',L(readinessNames,readiness.components?.[key])));}
  card.append(el('h4','','Готовность: не готова'));card.append(components);list(card,readiness.blockers);
  if(array(metric.open_questions).length){card.append(el('h4','','Вопросы владельцу методики'));list(card,metric.open_questions);}
  if(array(metric.missing_facts).length){card.append(el('h4','','Нужны дополнительные факты'));list(card,metric.missing_facts.map(fact=>fact.meaning+' — '+L(roleNames,fact.lifecycle)));}
  cards.append(card);}
})();

// Basis: context, provenance, stage states and limits of this report.
(()=>{const root=$('basis-content'),context=report.process_context,runNames={completed:'Исследование завершено',partial:'Частичная карта',failed:'Исследование не выполнено'};
 const delivery=report.delivery||{},handoff=delivery.handoff||{},deliveryNames={completed:'Сохранён',partial:'Сохранены готовые части',failed:'Не сохранён',not_run:'Не строился'},traceNames={disabled:'Не включён',complete:'Доступные спаны отправлены',partial:'Часть спанов не отправлена',failed:'Не удалось отправить',pending:'Отправка не подтверждена'};
 const handoffText=handoff.status==='completed'?'Папка handoff рядом с этим файлом: описание процесса, onboarding.json, CSV событий, доработок и метрик, manifest с контрольными суммами.':(deliveryNames[handoff.status]||'Сохранение комплекта не подтверждено')+(handoff.reason?' — '+handoff.reason:'');
 fields(root,[['Карта',L(runNames,report.map_status)],['Ревизия карты',report.map_revision],['Запуск',report.run_id],['Версия отчёта',report.report_version],
  ['Запросы к модели',report.budget?report.budget.used_requests+' из '+report.budget.max_requests+(report.budget.cache_hits?'; повторно использовано проверенных ответов: '+report.budget.cache_hits:''):'Этапы с моделью не выполнялись'],
  ['Сохранение отчёта',deliveryNames[delivery.report?.status]||'Не подтверждено'],['Комплект передачи',handoffText],
  ['Локальный журнал',delivery.audit?.status==='complete'?'Записан до публикации результата':delivery.audit?.status==='partial'?'Есть ошибки записи до публикации результата':'Состояние не передано'],
  ['Экспорт трассы',delivery.trace_export?L(traceNames,delivery.trace_export.status)+' (до публикации результата)':'Состояние не передано']]);
 if(delivery.report?.reason)root.append(el('p','report-notice',delivery.report.reason));
 root.append(el('h3','','Контекст процесса'));
 if(!context)root.append(el('p','report-empty','Контекст процесса не построен.'));
 else{fields(root,[['Цель',context.goal],['Начало',context.start],['Завершение',context.finish],['Экземпляр процесса',context.case?.subject],
   ['Идентификаторы',array(context.case?.identifiers).map(row=>row.name+' — '+L(sourceNames,row.source)+(row.where?' ('+row.where+')':'')).join('; ')||'Не установлены'],
   ['Исходы',array(context.outcomes).map(row=>row.name+' ('+array(row.operation_ids).map(id=>byId.get(id)?.name||id).join(', ')+')').join('; ')],
   ['Предметный профиль',report.domain_profile?report.domain_profile.title+', версия '+report.domain_profile.version:'Не использовался']]);
  root.append(el('h4','','Вопросы измерения'));list(root,array(context.measurement_questions).map(row=>row.id+' · '+row.question+' — '+L(familyNames,row.family)+', источник: '+L(sourceNames,row.source)));
  if(array(context.glossary).length){root.append(el('h4','','Словарь'));list(root,context.glossary.map(row=>row.term+' — '+row.meaning+' ('+L(sourceNames,row.source)+')'));}
  if(array(context.hypotheses).length){root.append(el('h4','','Гипотезы'));list(root,context.hypotheses);}
  if(array(context.open_questions).length){root.append(el('h4','','Открытые вопросы'));list(root,context.open_questions);}}
 root.append(el('h3','','Состояние этапов'));
 const body=table(root,['Этап','Выполнение','Результат','Причины']);
 for(const stage of array(report.stages)){const reasons=block();for(const reason of array(stage.reasons))reasons.append(el('div','',reason));if(!array(stage.reasons).length)reasons.append(el('span','muted','—'));
  const tr=el('tr');tr.append(cell(stage.title||stage.id),cell(L(executionNames,stage.execution_status)),cell(L(resultNames,stage.result_status)),cell(reasons));body.append(tr);}
 root.append(el('h3','','Происхождение выводов'));
 list(root,['Основания операций и связей различаются: подтверждение кодом, вывод или неизвестность указаны у каждого элемента карты. Отметка «Подтверждено кодом» не означает, что исполнение наблюдалось в работающей системе.',
  'Места логирования — статические кандидаты детектора: включённость логирования, достижимость вызова и доставка записи не установлены.',
  'Смысл записи предложен моделью по выданным строкам кода и проверен программно: ссылки на строки, операции-кандидаты, поля и непротиворечивость. Бизнес-смысл не согласован специалистом.',
  'Контекст, необходимые факты, доработки и определения метрик — предложения. Покрытие рассчитано программно по каждой оси отдельно.']);
 const runtime=report.runtime_profile;root.append(el('h3','','Данные выполнения'));
 if(!runtime)root.append(el('p','report-empty','Файл реальных логов не передавался: отсутствие данных не означает отсутствие событий.'));
 else{fields(root,[['Файл',runtime.name],['Записей',runtime.record_count],['Ключей событий',Object.keys(runtime.event_counts||{}).length],['Экземпляров с идентификатором',runtime.identified_case_count]]);list(root,runtime.limitations);}
 root.append(el('h3','','Методика этапов'));list(root,Object.entries(report.methodology||{}).map(([stage,digest])=>stage+' · '+digest.slice(0,12)),'Этапы с моделью не выполнялись.');
})();
applyHash();
'''
