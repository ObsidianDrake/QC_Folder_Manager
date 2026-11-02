import errno
import io
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from qcmanager import fs_helper
from qcmanager.storage import Storage, StorageError


def remote(configuration):
    configuration['hosts']['local'] = {'transport':'ssh', 'hostname':'test-worker',
        'ssh':{'python':'python3','timeout_seconds':30}, 'environment':'test'}
    return Storage(configuration), configuration['projects'][0]


@pytest.mark.parametrize('stderr,code', [
    ('Permission denied (publickey).','permission_denied'),
    ('Host key verification failed.','host_key'),
    ('python3: Command not found.','python_missing'),
    ('SyntaxError: invalid syntax','python_syntax'),
    ('Could not resolve hostname test-worker','dns'),
    ('Connection refused','refused'),
])
def test_ssh_failure_is_actionable_without_raw_output(configuration, monkeypatch, stderr, code):
    storage, project = remote(configuration)
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0],255,'secret-stdout',stderr+' secret-token')
    monkeypatch.setattr(subprocess,'run',run)
    with pytest.raises(StorageError) as caught:
        storage.request(project,'scan')
    error = caught.value
    assert error.details == {'exit_code':255,'reason':code}
    assert 'secret' not in str(error.details) + str(error) + error.ui_message


def test_remote_helper_reports_missing_root():
    result = subprocess.run([sys.executable, str(Path(fs_helper.__file__))],
        input=json.dumps({'action':'scan','root':'/qcfm-nonexistent-test/root','prefix':'qc'}),
        text=True,capture_output=True,check=True)
    body=json.loads(result.stdout)
    assert body['error_type']=='FileNotFoundError'
    assert body['error_errno']==errno.ENOENT
    assert '/qcfm' not in result.stdout


@pytest.mark.parametrize('remote_mode',[False,True])
def test_filesystem_errno_survives_transport(configuration,monkeypatch,remote_mode):
    project=configuration['projects'][0]
    if remote_mode:
        storage,project=remote(configuration)
        monkeypatch.setattr(subprocess,'run',lambda *a,**k: subprocess.CompletedProcess(a[0],0,
            json.dumps({'error':'failed','error_type':'PermissionError','error_errno':errno.EACCES}),''))
    else:
        storage=Storage(configuration)
        def fail(body):raise PermissionError(errno.EACCES,'secret-path')
        monkeypatch.setattr(fs_helper,'execute',fail)
    with pytest.raises(StorageError) as caught:storage.request(project,'scan')
    assert caught.value.details['errno_name']=='EACCES'
    assert 'secret-path' not in str(caught.value.details)


def test_timeout_preserves_delete_uncertainty(configuration,monkeypatch):
    storage,project=remote(configuration)
    def fail(*a,**k):raise subprocess.TimeoutExpired('secret-command',30,output='secret')
    monkeypatch.setattr(subprocess,'run',fail)
    with pytest.raises(StorageError) as caught:storage.request(project,'delete')
    assert caught.value.uncertain
    assert caught.value.details=={'timeout_seconds':30}


def test_scan_persists_specific_ui_error_and_logs_context(web, monkeypatch):
    app,_,_=web
    manager=app.extensions['manager']
    stream=io.StringIO();handler=logging.StreamHandler(stream)
    manager.log.addHandler(handler)
    def fail(*a,**k):
        raise StorageError('safe',details={'reason':'python_missing','exit_code':127},
                           ui_message='Remote Python not found')
    monkeypatch.setattr(manager.storage,'request',fail)
    try:
        manager.db.start_scan('example-project')
        manager._scan('example-project')
    finally:manager.log.removeHandler(handler)
    _,scans=manager.db.inventory({'example-project'})
    assert scans['example-project']['error']=='Remote Python not found'
    details=json.loads(stream.getvalue().split('diagnostic=',1)[1])
    assert details['stage']=='filesystem'
    assert details['root']==manager.projects['example-project']['run_folder']
    assert details['storage']=={'reason':'python_missing','exit_code':127}
    assert details['exception']['frames']


def test_unexpected_exception_does_not_log_secrets(web,monkeypatch):
    app,_,_=web;manager=app.extensions['manager']
    stream=io.StringIO();handler=logging.StreamHandler(stream);manager.log.addHandler(handler)
    def fail(*a,**k):raise RuntimeError('secret-token')
    monkeypatch.setattr(manager.storage,'request',fail)
    try:
        manager.db.start_scan('example-project');manager._scan('example-project')
    finally:manager.log.removeHandler(handler)
    assert 'RuntimeError' in stream.getvalue()
    assert 'secret-token' not in stream.getvalue()
