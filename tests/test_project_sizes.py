import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from conftest import login_client, wait_for
from qcmanager import fs_helper


def test_real_project_dust_one_process_returns_every_folder(tmp_path, monkeypatch):
    executable = shutil.which('dust')
    if not executable:
        pytest.skip('Install dust for integration test')
    for number in range(1, 151):
        folder = tmp_path / ('qc_%d' % number)
        folder.mkdir()
        (folder/'data').write_bytes(b'x'*8192)
    (tmp_path/'unmanaged').mkdir()
    (tmp_path/'qc_999').symlink_to(tmp_path/'unmanaged', target_is_directory=True)
    original = subprocess.run
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return original(command, **kwargs)
    monkeypatch.setattr(subprocess, 'run', run)
    result = fs_helper.execute(dict(action='project_sizes', root=str(tmp_path), prefix='qc',
                                   dust={'executable':executable}))
    assert len(calls) == 1
    assert calls[0][calls[0].index('-d')+1] == '1'
    assert len(result['records']) == 150
    for number in range(1, 151):
        name = 'qc_%d' % number
        assert result['records'][name]['bytes'] >= 8192
        assert result['records'][name]['identity'] == fs_helper.identity((tmp_path/name).stat())


def test_project_results_reject_replaced_and_disappeared_folders(tmp_path, monkeypatch):
    for name in ('qc_1','qc_2','qc_3'):
        (tmp_path/name).mkdir()
    def tree(fd, options, **kwargs):
        (tmp_path/'qc_1').rename(tmp_path/'saved')
        (tmp_path/'qc_1').mkdir()
        (tmp_path/'qc_2').rmdir()
        return dict(size=30, children=[dict(name='./'+name, size=10) for name in ('qc_1','qc_2','qc_3')])
    monkeypatch.setattr(fs_helper, 'dust_tree', tree)
    result = fs_helper.execute(dict(action='project_sizes', root=str(tmp_path), prefix='qc'))
    assert set(result['records']) == {'qc_3'}


def test_project_api_one_scan_for_all_records_and_shared_timestamp(web, monkeypatch):
    app,gitea,_ = web
    manager = app.extensions['manager'];project = manager.projects['example-project']
    root = Path(project['run_folder'])
    for number in range(1,4):
        (root/('qc_%d'%number)).mkdir()
    manager.db.start_scan(project['id'])
    manager.db.finish_scan(project['id'],manager.storage.request(project,'scan',metadata_only=True))
    original = manager.storage.request
    calls=[]; entered=threading.Event(); release=threading.Event()
    def request(p,action,**fields):
        if action == 'project_sizes':
            calls.append(p['id']);entered.set();assert release.wait(5)
        return original(p,action,**fields)
    monkeypatch.setattr(manager.storage,'request',request)
    client,headers=login_client(app,gitea)
    try:
        assert client.post('/api/sizes',json={'projects':[project['id']]},headers=headers).json['accepted']==1
        assert entered.wait(5)
        assert client.post('/api/sizes',json={'projects':[project['id']]},headers=headers).json['accepted']==0
        assert client.get('/api/records').json['size_scans'][project['id']]['state']=='running'
    finally:
        release.set()
    wait_for(lambda:not manager.pending_size_projects)
    rows=manager.db.inventory({project['id']})[0]
    stamp=manager.db.size_status({project['id']})[project['id']]['updated']
    assert calls == [project['id']]
    assert {r['size_at'] for r in rows} == {stamp}
    assert client.post('/api/sizes',json={'projects':[project['id']]}).status_code==403
    gitea.actor['orgs']=[]
    assert client.post('/api/sizes',json={'projects':[project['id']]},headers=headers).status_code==404
    assert client.get('/api/records').json['size_scans']=={}


def test_empty_project_has_completion_time_and_failure_keeps_it(web, monkeypatch):
    app,_,_=web;manager=app.extensions['manager'];pid='example-project'
    manager.queue_project_size(pid);wait_for(lambda:not manager.pending_size_projects)
    before=manager.db.size_status({pid})[pid]
    assert before['state']=='cached' and before['updated']
    def fail(*args,**kwargs):raise OSError('offline')
    monkeypatch.setattr(manager.storage,'request',fail)
    manager.queue_project_size(pid);wait_for(lambda:not manager.pending_size_projects)
    after=manager.db.size_status({pid})[pid]
    assert after['state']=='failed' and after['updated']==before['updated']
    manager.db.set_size_status(pid,'running');manager.db.recover()
    assert manager.db.size_status({pid})[pid]['state']=='interrupted'


@pytest.mark.parametrize('projects', [None,[],['example-project']*2,'example-project',[1]])
def test_project_size_api_rejects_invalid_projects(web,projects):
    client,headers=login_client(web[0],web[1])
    assert client.post('/api/sizes',json={'projects':projects},headers=headers).status_code==400
