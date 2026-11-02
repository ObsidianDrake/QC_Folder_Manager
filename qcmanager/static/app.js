'use strict';
const {t}=window.qcI18n;
// Decorative icons use fixed local SVG paths; no user content enters SVG markup.
const uiIcons={
 folder:'<path d="M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9H3Z"/><path d="M3 10h18"/>',
 disk:'<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M3 14h18M7 17h.01M11 17h.01"/>',
 clean:'<path d="M9 5V3h6v2M4 7h16M6 7l1 14h10l1-14M10 11v6M14 11v6"/>',
 shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9S4 17 4 12V6Z"/><path d="m8 12 3 3 5-6"/>',
 refresh:'<path d="M3 12a9 9 0 0 1 15-7l3 3M21 3v5h-5M21 12a9 9 0 0 1-15 7l-3-3M3 21v-5h5"/>',
 history:'<path d="M3 11a9 9 0 1 1 2 7M3 4v7h7M12 7v5l3 2"/>',
 list:'<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6M9 12h6M9 16h4"/>',
 close:'<path d="m7 7 10 10M17 7 7 17"/>',
 check:'<path d="m5 12 4 4L19 6"/>',
 play:'<path d="m9 5 10 7-10 7Z"/>',
 logout:'<path d="M9 4H4v16h5M9 12h12M17 8l4 4-4 4"/>'
};
function icon(name){
  const el=document.createElement('span');el.className='ui-icon';el.setAttribute('aria-hidden','true');
  el.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" focusable="false">'+uiIcons[name]+'</svg>';
  return el;
}
for(const el of document.querySelectorAll('[data-icon]'))el.prepend(icon(el.dataset.icon));
for(const [i,name] of ['folder','disk','clean','shield'].entries()){
  const symbol=icon(name);symbol.classList.add('metric-icon');document.querySelectorAll('.metrics>div')[i].append(symbol);
}

const base=document.querySelector('meta[name="app-base"]').content;
const $=id=>document.getElementById(id);
let isAdmin=false, adminOpen=false;
let csrf='', inventory={records:[],projects:[],scans:{}}, selected=new Set(), cursor=null, auditOpen=false, busy=false, loggedIn=false, operationRows=[], auditRows=[];
const stateNames={success:'成功',failure:'失敗',error:'錯誤',killed:'已終止',skipped:'已跳過',declined:'已拒絕',running:'執行中',pending:'排隊中',blocked:'等待核准',waiting_on_dependencies:'等待相依工作',unknown:'待確認',queued:'清理排隊中',deleted:'已刪除',failed:'清理失敗',partial:'部分刪除',rejected:'已阻擋',requested:'提出刪除'};
function node(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=t(text);if(cls)n.className=cls;return n;}
function notice(message){notice.message=message;$('notice').textContent=t(message);$('notice').hidden=false;clearTimeout(notice.timer);notice.timer=setTimeout(()=>$('notice').hidden=true,8000);}
function bytes(n){if(n===null||n===undefined)return t('待確認');let u=0;while(n>=1024&&u<4){n/=1024;u++;}return `${n.toFixed(u?1:0)} ${['B','KiB','MiB','GiB','TiB'][u]}`;}
const dateFormatter=new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Taipei',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
function date(timestamp){
  if(timestamp===null||timestamp===undefined)return t('尚未掃描');
  const parts=Object.fromEntries(dateFormatter.formatToParts(new Date(timestamp*1000)).map(part=>[part.type,part.value]));
  return `${parts.year}/${parts.month}/${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}
function badge(state){const result=node('span',stateNames[state]||state,'badge '+(['success','deleted'].includes(state)?'good':['running','pending','blocked','waiting_on_dependencies','queued'].includes(state)?'warn':['failure','error','failed','partial','unknown'].includes(state)?'bad':''));const symbol=result.classList.contains('good')?'check':result.classList.contains('warn')?'play':result.classList.contains('bad')?'close':'list';result.prepend(icon(symbol));return result;}
async function api(path,body){const options={credentials:'same-origin',headers:{'X-CSRF-Token':csrf}};if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}const r=await fetch(base+path,options);const data=await r.json();if(!r.ok){if(r.status===401)showLogin();if(r.status===403&&path.startsWith('/api/admin/')){isAdmin=false;window.dispatchEvent(new CustomEvent('qc-user',{detail:null}));}throw new Error(data.error||'操作失敗');}return data;}
function showLogin(){isAdmin=false;window.dispatchEvent(new CustomEvent('qc-user',{detail:null}));loggedIn=false;$('workspace').hidden=true;$('welcome').hidden=false;$('account').replaceChildren();}
function projectRecords(){return inventory.records.filter(r=>!$('project').value||r.project===$('project').value);}
function visible(){return projectRecords().filter(r=>(!$('search').value||`${r.number} ${r.name}`.toLowerCase().includes($('search').value.toLowerCase()))&&(!$('status').value||($('status').value==='ready'?r.deletable:!r.deletable)));}
function selection(){const eligible=new Set(inventory.records.filter(r=>r.deletable).map(r=>r.id));selected=new Set([...selected].filter(id=>eligible.has(id)));$('selected-count').textContent=t('已選取 {count} 筆',{count:selected.size});$('delete').disabled=!selected.size;$('update-sizes').disabled=!selected.size;const list=visible().filter(r=>r.deletable);$('select-all').checked=!!list.length&&list.every(r=>selected.has(r.id));$('select-all').indeterminate=list.some(r=>selected.has(r.id))&&!$('select-all').checked;}
// Refresh CW icon: Lucide (ISC), see LUCIDE-LICENSE.txt.
function sizeRefreshButton(record){
  const running=record.size_state==='running';
  const queued=record.size_state==='queued';
  const label=t(running?'容量計算中':queued?'容量排隊中':'更新容量');
  const button=node('button',undefined,'size-refresh'+(running?' is-running':''));
  button.type='button';
  button.title=label;
  button.setAttribute('aria-label',label+' · '+record.name);
  button.setAttribute('aria-busy',String(running||queued));
  button.disabled=running||queued;
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox','0 0 24 24');
  svg.setAttribute('aria-hidden','true');
  svg.setAttribute('focusable','false');
  for(const d of [
    'M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8',
    'M21 3v5h-5',
    'M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16',
    'M8 16H3v5'
  ]){
    const path=document.createElementNS(svg.namespaceURI,'path');
    path.setAttribute('d',d);svg.append(path);
  }
  button.append(svg);
  button.addEventListener('click',safe(async()=>{
    button.disabled=true;
    try{await updateSizes([record.id]);}
    finally{button.disabled=['queued','running'].includes(inventory.records.find(r=>r.id===record.id)?.size_state);}
  }));
  return button;
}
function render(){const list=visible();$('records').replaceChildren();for(const r of list){const tr=node('tr'),check=node('input');check.type='checkbox';check.checked=selected.has(r.id);check.disabled=!r.deletable;check.setAttribute('aria-label',t('選取 {name}',{name:r.name}));check.title=t(r.delete_reason);check.addEventListener('change',()=>{check.checked?selected.add(r.id):selected.delete(r.id);selection();});const c=node('td');c.append(check);tr.append(c);const detail=node('td'),link=node('a',r.name);link.prepend(icon('folder'));link.href=r.drone_url;link.target='_blank';link.rel='noopener noreferrer';detail.append(link,node('small',`${r.organization} / ${r.repository} · #${r.number}`));tr.append(detail);const status=node('td');status.append(badge(r.operation_state||r.drone_status));if(r.delete_reason)status.append(node('small',r.delete_reason));tr.append(status);const capacity=node('td',undefined,'capacity-cell'),sizeLine=node('div',undefined,'capacity-value');sizeLine.append(node('strong',bytes(r.bytes)));const sizeLabels={pending:'等待計算',queued:'容量排隊中',running:'容量計算中',cached:'快取容量',failed:'容量計算失敗',interrupted:'容量計算中斷'};const update=sizeRefreshButton(r);sizeLine.append(update);capacity.append(sizeLine);if(r.size_state&&r.size_state!=='cached')capacity.append(node('small',sizeLabels[r.size_state],'size-state'));if(r.size_at){const stamp=node('small',t('容量更新時間')+': '+date(r.size_at),'size-time');stamp.title=t('容量更新時間');capacity.append(stamp);}tr.append(capacity);$('records').append(tr);}$('empty').hidden=!!list.length;selection();const summary=projectRecords();$('total').textContent=summary.length;$('size').textContent=bytes(summary.reduce((s,r)=>s+(r.bytes||0),0));$('eligible').textContent=summary.filter(r=>r.deletable).length;$('protected').textContent=summary.filter(r=>!r.deletable).length;$('scan-status').replaceChildren();for(const p of inventory.projects){if($('project').value&&$('project').value!==p.id)continue;const s=inventory.scans[p.id];let text=`${p.organization}/${p.repository} — `+(s?(['running','checking'].includes(s.state)?t(s.state==='checking'?'確認 Drone 狀態中':'掃描中…'):t(s.error)||date(s.updated)):t('尚未掃描'));if(s?.quarantine_count)text+=' · '+t('{count} 個清理暫存目錄待人工核對',{count:s.quarantine_count});$('scan-status').append(node('div',text,s?.error||s?.quarantine_count?'problem':''));}}
async function refresh(){if(busy||!loggedIn||adminOpen)return;busy=true;try{inventory=await api('/api/records');const old=$('project').value;$('project').replaceChildren(new Option(t('全部可存取專案'),''));for(const p of inventory.projects)$('project').add(new Option(`${p.organization}/${p.repository}`,p.id));if(inventory.projects.some(p=>p.id===old))$('project').value=old;render();await refreshDisks();if(auditOpen)await operations();}catch(e){notice(e.message);}finally{busy=false;}}
async function operations(){const data=await api('/api/operations');operationRows=data.operations;renderOperations();}
function renderOperations(){const data={operations:operationRows};$('operations').replaceChildren();for(const op of data.operations){const div=node('div',undefined,'operation'),left=node('div');left.append(node('strong',`${op.target.name} · ${op.actor.login}`),node('small',`${op.target.organization||''}/${op.target.repository||''}`),node('small',`${date(op.created)} · ${op.id}`));const right=node('div');right.append(badge(op.state),node('small',op.message||'等待執行'));div.append(left,right);$('operations').append(div);}if(!data.operations.length)$('operations').append(node('p','尚無清理工作。','muted'));}
async function audit(more=false){const data=await api('/api/audit'+(more&&cursor?'?before='+cursor:''));auditRows=more?auditRows.concat(data.events):data.events;cursor=data.next_before;renderAudit();}
function projectName(id){const p=inventory.projects.find(p=>p.id===id);return p?`${p.organization}/${p.repository}`:'—';}
function renderAudit(){const data={events:auditRows,next_before:cursor};$('audit').replaceChildren();for(const e of data.events){const tr=node('tr');tr.append(node('td',date(e.at)),node('td',`${e.actor.login}${e.actor.is_admin?' · Admin':''}`),node('td',stateNames[e.event]||e.event),node('td',`${e.organization&&e.repository?e.organization+'/'+e.repository:projectName(e.project)} / ${e.operation||'—'}`),node('td',e.detail.message||e.detail.reason||e.detail.target?.name||''));$('audit').append(tr);}cursor=data.next_before;$('more-audit').hidden=!cursor;}
function safe(fn){return async()=>{try{await fn();}catch(e){notice(e.message);}};}
$('project').addEventListener('change',()=>{render();renderDisks();});$('search').addEventListener('input',render);$('status').addEventListener('change',render);
$('select-all').addEventListener('change',()=>{for(const r of visible().filter(r=>r.deletable))$('select-all').checked?selected.add(r.id):selected.delete(r.id);render();});
async function updateSizes(ids){const result=await api('/api/sizes',{records:ids});notice(t('已排入 {count} 筆容量計算',{count:result.accepted}));await refresh();}
$('update-sizes').addEventListener('click',safe(()=>updateSizes([...selected])));
$('scan').addEventListener('click',safe(async()=>{const ids=$('project').value?[$('project').value]:inventory.projects.map(p=>p.id);if(!ids.length)return notice('目前沒有可存取的專案。');await api('/api/scan',{projects:ids});notice('掃描已排入背景工作。');await refresh();}));
$('delete').addEventListener('click',()=>{$('confirm-list').replaceChildren();for(const r of inventory.records.filter(r=>selected.has(r.id)))$('confirm-list').append(node('div',`${r.organization}/${r.repository} · ${r.name} · #${r.number}`));$('confirm').showModal();});
$('cancel-delete').addEventListener('click',()=>$('confirm').close());
$('confirm-delete').addEventListener('click',safe(async()=>{$('confirm-delete').disabled=true;try{const result=await api('/api/delete',{records:[...selected],confirm:true});selected.clear();$('confirm').close();notice(t('已提交 {count} 筆清理工作；結果請見刪除紀錄。',{count:result.results.filter(r=>r.operation).length}));await refresh();}finally{$('confirm-delete').disabled=false;}}));
$('records-tab').addEventListener('click',()=>{auditOpen=false;$('records-panel').hidden=false;$('audit-panel').hidden=true;$('records-tab').classList.add('active');$('audit-tab').classList.remove('active');});
$('audit-tab').addEventListener('click',safe(async()=>{auditOpen=true;$('records-panel').hidden=true;$('audit-panel').hidden=false;$('audit-tab').classList.add('active');$('records-tab').classList.remove('active');await operations();await audit();}));
$('refresh-audit').addEventListener('click',safe(async()=>{await operations();await audit();}));$('more-audit').addEventListener('click',safe(()=>audit(true)));
async function boot(){try{const me=await api('/api/me');csrf=me.csrf;loggedIn=true;isAdmin=me.user.is_admin===true;window.dispatchEvent(new CustomEvent('qc-user',{detail:me.user}));$('welcome').hidden=true;$('workspace').hidden=false;const logout=node('button','登出','button');logout.prepend(icon('logout'));logout.addEventListener('click',safe(async()=>{await api('/auth/logout',{});showLogin();}));$('account').replaceChildren(node('span',me.user.login+(me.user.is_admin?' · Gitea Admin':'')),logout);await refresh();}catch(e){showLogin();if(!e.message.includes('請先'))notice(e.message);}}
window.addEventListener('qc-language-change',()=>{const all=$('project').querySelector('option[value=""]');if(all)all.textContent=t('全部可存取專案');const logout=$('account').querySelector('button');if(logout)logout.replaceChildren(icon('logout'),document.createTextNode(t('登出')));render();renderDisks();renderOperations();renderAudit();if(!$('notice').hidden)$('notice').textContent=t(notice.message);});
let diskRows=[];
function renderDisks(){
  $('disks').replaceChildren();
  const rows=diskRows.filter(d=>!$('project').value||d.projects.some(p=>p.id===$('project').value));
  for(const d of rows){
    const card=node('article',undefined,'disk-card'),head=node('div',undefined,'disk-card-head');
    head.append(icon('disk'),node('strong','磁碟空間'));card.append(head);
    const value=node('div',undefined,'disk-available');value.append(node('strong',d.available===null||d.available===undefined?'—':bytes(d.available)),node('span','可用空間'));card.append(value);
    if(typeof d.total==='number'&&d.total>0){
      const percent=Math.max(0,Math.min(100,d.used/d.total*100));
      const meter=node('progress');meter.max=100;meter.value=percent;meter.className=percent>=95?'critical':percent>=85?'warning':'';meter.setAttribute('aria-label',t('磁碟使用率'));card.append(meter);
      card.append(node('small',t('已使用 {percent} · 總容量 {total}',{percent:percent.toFixed(1)+'%',total:bytes(d.total)})));
    }
    card.append(node('p',d.projects.map(p=>p.organization+'/'+p.repository).join(' · '),'disk-projects'));
    const state=d.error?'查詢失敗，保留上次資料':d.refreshing?'更新中':d.stale?'資料已過期':'已更新';
    card.append(node('small',state,d.error||d.stale?'disk-stale':''));if(d.updated)card.append(node('small',date(d.updated)));
    $('disks').append(card);
  }
  if(!rows.length)$('disks').append(node('p','尚無磁碟資訊','muted'));
}
async function refreshDisks(){try{diskRows=(await api('/api/disks')).disks;renderDisks();}catch(e){notice(e.message);}}
$('refresh-disks').addEventListener('click',safe(async()=>{const projects=$('project').value?[$('project').value]:inventory.projects.map(p=>p.id);await api('/api/disks/refresh',{projects});await refreshDisks();}));
boot();setInterval(()=>{if(!document.hidden)refresh();},8000);

