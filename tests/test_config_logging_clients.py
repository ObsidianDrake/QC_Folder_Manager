import copy
import json
import logging
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from qcmanager import clients
from qcmanager.clients import APIError,Drone,Gitea
from qcmanager.config import ConfigError,load_config
from qcmanager.database import Database
from qcmanager.logging_setup import setup_logging
from qcmanager.storage import Storage,StorageError


@pytest.mark.parametrize('edit',[
    lambda c:c['projects'][0].update(run_folder='/'),
    lambda c:c['projects'][0].update(prefix='../danger'),
    lambda c:c['projects'].append(copy.deepcopy(c['projects'][0])),
    lambda c:c['server'].update(data_dir=c['projects'][0]['run_folder']+'/data'),
    lambda c:c.update(logging={'backup_count':0}),
    lambda c:c.update(logging={'max_bytes':0}),
    lambda c:c['gitea'].update(base_url='http://user:password@server'),
])
def test_invalid_configuration_rejected(configuration,tmp_path,edit):
    edit(configuration)
    path=tmp_path/'bad.yml';path.write_text(yaml.safe_dump(configuration))
    with pytest.raises(ConfigError):load_config(path)


def test_duplicate_yaml_key_rejected(tmp_path):
    path=tmp_path/'bad.yml';path.write_text('schema_version: 1\nschema_version: 1\n')
    with pytest.raises(ConfigError,match='Duplicate'):load_config(path)


def test_log_rotation_bounds_and_preserves_audit(configuration):
    configuration['logging'].update(max_bytes=1024,backup_count=2)
    db=Database(Path(configuration['server']['data_dir'])/'test.sqlite3')
    actor={'id':1,'login':'operator','is_admin':False,'orgs':['ExampleVendor']}
    db.rejected(actor,'example-project',{'reason':'protected'})
    logdir=Path(configuration['logging']['directory']);logdir.mkdir(parents=True,exist_ok=True)
    (logdir/'qc-folder-manager.log.9').write_text('old backup')
    (logdir/'unrelated.log').write_text('keep')
    logger=setup_logging(configuration)
    assert not (logdir/'qc-folder-manager.log.9').exists()
    assert (logdir/'unrelated.log').exists()
    for n in range(200):logger.info('safe operation %s %s',n,'x'*100)
    for h in logger.handlers:h.flush()
    logs=list(Path(configuration['logging']['directory']).glob('qc-folder-manager.log*'))
    assert len(logs)==3 and all(p.stat().st_size<=1024 for p in logs)
    assert '199' in (Path(configuration['logging']['directory'])/'qc-folder-manager.log').read_text()
    assert len(db.audit({'example-project'}))==1


def test_restart_marks_running_unknown_without_retry(configuration):
    db=Database(Path(configuration['server']['data_dir'])/'test.sqlite3')
    actor={'id':1,'login':'a'}
    running=db.enqueue(actor,{'id':'r','project':'example-project'},'batch')
    queued=db.enqueue(actor,{'id':'s','project':'example-project'},'batch')
    db.update_operation(running,'running')
    db.recover()
    states={o['id']:o['state'] for o in db.operations({'example-project'})}
    assert states[running]=='unknown' and states[queued]=='failed'


def test_gitea_pagination_uses_all_pages_even_small_server_limit(configuration,monkeypatch):
    calls=[]
    def fetch(url,token):
        calls.append(url)
        if url.endswith('/api/v1/user'):return {'id':1,'login':'a','is_admin':False}
        if 'page=1&' in url:return [{'name':'Other'}]
        if 'page=2&' in url:return [{'name':'ExampleVendor'}]
        return []
    monkeypatch.setattr(clients,'http_json',fetch)
    actor=Gitea(configuration).identity('secret')
    assert actor['orgs']==['Other','ExampleVendor'] and len(calls)==4
    assert all('/gitea/api/v1/' in u for u in calls)


def test_missing_admin_flag_denied(configuration,monkeypatch):
    monkeypatch.setattr(clients,'http_json',lambda *args:{'id':1,'login':'a'})
    with pytest.raises(APIError):Gitea(configuration).identity('secret')


def test_drone_subpath_and_number_validation(configuration,monkeypatch):
    urls=[]
    def fetch(url,token):
        urls.append(url);return {'number':1234,'status':'running'}
    monkeypatch.setattr(clients,'http_json',fetch)
    drone=Drone(configuration)
    assert drone.build(configuration['projects'][0],1234)['status']=='running'
    assert urls==['http://drone.test/drone/api/repos/ExampleVendor/Example_QC/builds/1234']
    with pytest.raises(APIError):drone.build(configuration['projects'][0],1235)


def test_ssh_uses_current_user_and_json_stdin(configuration,monkeypatch):
    configuration['hosts']['local']={'transport':'ssh','hostname':'remote-worker','environment':'zone-b',
                                    'ssh':{'python':'python3','timeout_seconds':20}}
    def run(command,**kwargs):
        assert command[0]=='ssh' and 'BatchMode=yes' in command and 'StrictHostKeyChecking=yes' in command
        assert command[command.index('-l')+1]==__import__('pwd').getpwuid(os.geteuid()).pw_name
        body=json.loads(kwargs['input'])
        assert body['root']==configuration['projects'][0]['run_folder']
        assert body['root'] not in command[-1]
        assert 'shell' not in kwargs
        return subprocess.CompletedProcess(command,0,json.dumps({'root_identity':[1,2]}),'')
    monkeypatch.setattr(subprocess,'run',run)
    assert Storage(configuration).request(configuration['projects'][0],'probe')['root_identity']==[1,2]


def test_ssh_helper_transport_runs_actual_helper(configuration,monkeypatch):
    root=Path(configuration['projects'][0]['run_folder']);(root/'qc_1234').mkdir()
    configuration['hosts']['local']={'transport':'ssh','hostname':'remote-worker','environment':'zone-b',
                                    'ssh':{'python':'python3','timeout_seconds':20}}
    original=subprocess.run
    def loopback(command,**kwargs):
        # Exercise the actual encoded helper + stdin protocol in a local subprocess.
        import shlex
        return original(shlex.split(command[-1]),**kwargs)
    monkeypatch.setattr(subprocess,'run',loopback)
    result=Storage(configuration).request(configuration['projects'][0],'scan')
    assert result['records'][0]['number']==1234


def test_prefix_mount_and_assets(configuration):
    from qcmanager.web import create_app
    configuration['server']['public_url']='http://localhost/qc'
    configuration['server']['base_path']='/qc'
    app=create_app(configuration)
    try:
        client=app.test_client()
        assert client.get('/qc/health').status_code==200
        page=client.get('/qc/')
        assert b'/qc/assets/app.js' in page.data
        assert client.get('/qc/assets/app.js').status_code==200
        assert 'frame-ancestors' in page.headers['Content-Security-Policy']
    finally:app.extensions['manager'].shutdown()
