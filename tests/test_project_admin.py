import copy
import json
import os
from pathlib import Path

import pytest
import yaml

from conftest import login_client
from qcmanager import project_settings
from qcmanager.config import load_config
from qcmanager.web import create_app


def admin(web):
    app,gitea,_=web
    gitea.actor['is_admin']=True
    client,headers=login_client(app,gitea)
    return app,client,headers


def save(client,headers,action,project=None,pid=None,revision=None):
    if revision is None:revision=client.get('/api/admin/projects').json['revision']
    body={'action':action,'revision':revision}
    if project is not None:body['project']=project
    if pid is not None:body['id']=pid
    return client.post('/api/admin/projects',json=body,headers=headers)


def new_project(configuration):
    return dict(id='new-project',organization='OtherVendor',repository='Other_QC',host='local',
                run_folder=str(Path(configuration['projects'][0]['run_folder']).parent/'new-runs'),prefix='qc')


def test_vendor_denied_and_fresh_admin_revocation(web):
    app,gitea,_=web
    assert app.test_client().get('/api/admin/projects').status_code==401
    client,headers=login_client(app,gitea)
    assert client.get('/api/admin/projects').status_code==403
    assert client.post('/api/admin/projects',json={},headers=headers).status_code==403
    gitea.actor['is_admin']=True
    assert client.get('/api/admin/projects').status_code==200
    assert client.post('/api/admin/projects',json={}).status_code==403
    gitea.actor['is_admin']=False
    assert client.get('/api/admin/projects').status_code==403


def test_create_update_delete_persist_and_apply_without_restart(web,configuration):
    app,client,headers=admin(web)
    project=new_project(configuration)
    original=yaml.safe_load(Path(configuration['_source_path']).read_text())
    response=save(client,headers,'create',project)
    assert response.status_code==200
    assert project['id'] in app.extensions['manager'].projects
    assert project in load_config(configuration['_source_path'])['projects']
    assert len(client.get('/api/records').json['projects'])==2
    path=Path(configuration['_source_path'])
    written=yaml.safe_load(path.read_text())
    for section in ('gitea','drone','hosts','server'):assert written[section]==original[section]
    assert path.stat().st_mode & 0o777 == 0o600
    project['prefix']='run'
    assert save(client,headers,'update',project).status_code==200
    assert app.extensions['manager'].projects[project['id']]['prefix']=='run'
    assert save(client,headers,'delete',pid=project['id']).status_code==200
    assert project['id'] not in app.extensions['manager'].projects
    assert all(p['id']!=project['id'] for p in load_config(path)['projects'])
    assert client.get('/api/me').status_code==200
    with app.extensions['manager'].db.connect() as db:
        rows=db.execute('SELECT * FROM project_config_audit ORDER BY seq').fetchall()
    assert [r['action'] for r in rows]==['create','update','delete']
    assert all(json.loads(r['actor'])['login']=='vendor-one' for r in rows)


def test_empty_projects_and_restart(web,configuration):
    app,client,headers=admin(web)
    root=Path(configuration['projects'][0]['run_folder']);(root/'keep.txt').write_text('keep')
    assert save(client,headers,'delete',pid='example-project').status_code==200
    assert load_config(configuration['_source_path'])['projects']==[]
    assert (root/'keep.txt').read_text()=='keep'
    second=create_app(load_config(configuration['_source_path']))
    try:assert second.extensions['manager'].projects=={}
    finally:second.extensions['manager'].shutdown()


def test_update_invalidates_old_record_ids_and_disk_cache(web,configuration):
    app,client,headers=admin(web);manager=app.extensions['manager'];pid='example-project'
    manager.db.start_scan(pid)
    manager.db.finish_scan(pid,{'records':[{'name':'qc_1234','number':1234,'identity':[1,2],'root_identity':[1,3],'bytes':5}]})
    rid=manager.db.inventory({pid})[0][0]['id']
    manager.disks.items[pid]={'available':1}
    p=copy.deepcopy(manager.projects[pid]);p['run_folder']+='-new'
    assert save(client,headers,'update',p).status_code==200
    assert manager.db.record(rid) is None
    assert pid not in manager.disks.items
    assert client.post('/api/delete',json={'records':[rid],'confirm':True},headers=headers).status_code==404


def test_immutable_ownership_and_retired_history(web,configuration):
    app,client,headers=admin(web);manager=app.extensions['manager'];pid='example-project'
    p=copy.deepcopy(manager.projects[pid])
    target=dict(id='record',project=pid,name='qc_1234',number=1234,organization=p['organization'],repository=p['repository'])
    oid=manager.db.enqueue(web[1].actor,target,'batch');manager.db.update_operation(oid,'deleted')
    p['organization']='OtherVendor'
    assert save(client,headers,'update',p).status_code==409
    assert save(client,headers,'delete',pid=pid).status_code==200
    assert save(client,headers,'create',p).status_code==409
    assert client.get('/api/operations').json['operations'][0]['id']==oid
    web[1].actor.update(is_admin=False,orgs=['ExampleVendor'])
    assert client.get('/api/records').json['projects']==[]
    assert client.get('/api/audit').json['events']
    web[1].actor['orgs']=['OtherVendor']
    assert client.get('/api/audit').json['events']==[]


@pytest.mark.parametrize('busy_kind',['scan','size','disk','work'])
def test_busy_jobs_block_changes(web,configuration,busy_kind):
    app,client,headers=admin(web);manager=app.extensions['manager']
    pid='example-project'
    if busy_kind=='size':
        manager.db.start_scan(pid)
        manager.db.finish_scan(pid,{'records':[{'name':'qc_1','number':1}]})
        rid=manager.db.inventory({pid})[0][0]['id']
        manager.pending_sizes.add(rid)
    elif busy_kind=='scan':manager.pending_scans.add(pid)
    elif busy_kind=='disk':manager.disks.pending.add(pid)
    else:manager.locks[pid].acquire()
    try:
        assert save(client,headers,'delete',pid=pid).status_code==409
        assert save(client,headers,'create',new_project(configuration)).status_code==200
    finally:
        manager.pending_scans.clear();manager.pending_sizes.clear();manager.disks.pending.clear()
        if busy_kind=='work':manager.locks[pid].release()


def test_stale_revision_external_edit_invalid_root_and_atomic_failure(web,configuration,monkeypatch):
    app,client,headers=admin(web);project=new_project(configuration)
    revision=client.get('/api/admin/projects').json['revision']
    assert save(client,headers,'create',project).status_code==200
    assert save(client,headers,'delete',pid=project['id'],revision=revision).status_code==409
    path=Path(configuration['_source_path']);original=path.read_bytes()
    project['run_folder']='/'
    assert save(client,headers,'update',project).status_code==400
    assert path.read_bytes()==original
    project['run_folder']='/srv/test-changed'
    def fail(*a,**k):raise PermissionError('private-secret')
    monkeypatch.setattr(project_settings,'atomic_write',fail)
    result=save(client,headers,'update',project)
    assert result.status_code==500 and b'private-secret' not in result.data
    assert path.read_bytes()==original
    assert app.extensions['manager'].projects[project['id']]['run_folder']!='/srv/test-changed'
    path.write_bytes(original+b'\n# manually changed\n')
    assert save(client,headers,'delete',pid=project['id']).status_code==409


def test_admin_response_never_returns_credentials(web,configuration):
    app,client,headers=admin(web)
    configuration['hosts']['private-worker']={'transport':'ssh','hostname':'worker.test','ssh':{'password':'password-marker'}}
    response=client.get('/api/admin/projects')
    assert response.status_code==200
    for secret in ('password-marker','test-secret','test-drone-token'):
        assert secret not in response.get_data(as_text=True)
    assert all('ssh' not in h for h in response.json['hosts'])


def test_unresolved_cleanup_blocks_removal(web):
    app,client,headers=admin(web);manager=app.extensions['manager']
    oid=manager.db.enqueue(web[1].actor,dict(id='r',project='example-project'),'batch')
    manager.db.update_operation(oid,'unknown')
    assert save(client,headers,'delete',pid='example-project').status_code==409
