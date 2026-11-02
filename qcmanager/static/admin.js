'use strict';
let adminSettings=null, editingProject=null, adminSaving=false, adminEpoch=0;
const projectFields=['id','organization','repository','host','run_folder','prefix'];
function clearAdmin(){
  adminEpoch++;adminSettings=null;editingProject=null;adminOpen=false;
  $('admin-tab').hidden=true;$('admin-panel').hidden=true;$('admin-tab').classList.remove('active');
  $('records-panel').hidden=false;$('audit-panel').hidden=true;$('records-tab').classList.add('active');$('audit-tab').classList.remove('active');
  $('admin-projects').replaceChildren();$('edit-host').replaceChildren();$('project-form').reset();$('project-form').hidden=true;$('admin-error').hidden=true;
}
window.addEventListener('qc-user',event=>{clearAdmin();$('admin-tab').hidden=!event.detail?.is_admin;});
$('admin-tab').hidden=!isAdmin;
function adminError(error){$('admin-error').textContent=t(error.message);$('admin-error').hidden=false;}
function renderAdmin(){
  $('admin-projects').replaceChildren();
  if(!adminSettings)return;
  for(const p of adminSettings.projects){
    const card=node('article',undefined,'admin-project'),details=node('div');
    details.append(node('strong',p.organization+'/'+p.repository),node('small',p.id+' · '+p.host),node('code',p.run_folder),node('small','Prefix: '+p.prefix));
    const actions=node('div',undefined,'selection-actions'),edit=node('button','編輯','button'),remove=node('button','移除專案','button danger');
    edit.disabled=remove.disabled=!adminSettings.writable||adminSaving;
    edit.addEventListener('click',()=>openProject(p));
    remove.addEventListener('click',async()=>{
      if(!window.confirm(t('移除專案 {project}？此操作不會刪除 QC 資料夾。',{project:p.organization+'/'+p.repository})))return;
      await saveProject({action:'delete',id:p.id});
    });
    actions.append(edit,remove);card.append(details,actions);$('admin-projects').append(card);
  }
  if(!adminSettings.projects.length)$('admin-projects').append(node('p','尚未設定專案，請新增專案。','muted'));
  $('admin-new').disabled=!adminSettings.writable||adminSaving;
}
async function loadAdmin(){
  const epoch=++adminEpoch;
  $('admin-error').hidden=true;
  try{
    const data=await api('/api/admin/projects');
    if(epoch!==adminEpoch||!isAdmin||!adminOpen)return;
    adminSettings=data;renderAdmin();
  }catch(error){if(epoch===adminEpoch)adminError(error);}
}
function openProject(project){
  editingProject=project?.id||null;
  $('project-form').reset();$('project-form-title').textContent=t(project?'編輯專案':'新增專案');
  $('edit-host').replaceChildren();
  for(const host of adminSettings.hosts)$('edit-host').add(new Option(host.id+(host.hostname?' · '+host.hostname:''),host.id));
  for(const key of projectFields){const field=$('edit-'+key);if(project)field.value=project[key];if(['id','organization','repository'].includes(key))field.readOnly=!!project;}
  $('project-form').hidden=false;$('edit-'+(project?'run_folder':'id')).focus();
}
async function saveProject(payload){
  if(adminSaving)return;
  adminSaving=true;$('admin-save').disabled=true;renderAdmin();$('admin-error').hidden=true;
  const epoch=adminEpoch;
  try{
    const data=await api('/api/admin/projects',{...payload,revision:adminSettings.revision});
    if(epoch!==adminEpoch||!isAdmin)return;
    adminSettings=data;$('project-form').hidden=true;editingProject=null;
    selected.clear();inventory={records:[],projects:[],scans:{}};diskRows=[];operationRows=[];auditRows=[];
    notice('專案設定已儲存並生效，系統將自動重新掃描。');
  }catch(error){if(epoch===adminEpoch)adminError(error);}
  finally{adminSaving=false;$('admin-save').disabled=false;renderAdmin();}
}
$('admin-tab').addEventListener('click',async()=>{
  if(!isAdmin)return;
  adminOpen=true;auditOpen=false;$('records-panel').hidden=true;$('audit-panel').hidden=true;$('admin-panel').hidden=false;
  $('records-tab').classList.remove('active');$('audit-tab').classList.remove('active');$('admin-tab').classList.add('active');
  await loadAdmin();
});
for(const id of ['records-tab','audit-tab'])$(id).addEventListener('click',()=>{
  adminOpen=false;adminEpoch++;$('admin-panel').hidden=true;$('admin-tab').classList.remove('active');
  adminSettings=null;$('admin-projects').replaceChildren();$('project-form').reset();$('project-form').hidden=true;$('edit-host').replaceChildren();
  refresh();
});
$('admin-new').addEventListener('click',()=>openProject(null));
$('admin-reload').addEventListener('click',()=>{if(adminSaving)return;$('project-form').hidden=true;loadAdmin();});
$('admin-cancel').addEventListener('click',()=>{$('project-form').hidden=true;});
$('project-form').addEventListener('submit',event=>{event.preventDefault();const project={};for(const key of projectFields)project[key]=$('edit-'+key).value.trim();saveProject({action:editingProject?'update':'create',project});});
window.addEventListener('qc-language-change',()=>{renderAdmin();$('project-form-title').textContent=t(editingProject?'編輯專案':'新增專案');});
