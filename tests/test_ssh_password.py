import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from qcmanager.config import load_config, ConfigError
from qcmanager.ssh_auth import password_environment
from qcmanager.storage import Storage


def test_askpass_preserves_password_and_removes_helper(tmp_path):
    password = ' spaces $`"\\中文\t '
    env = os.environ.copy()
    with password_environment(env,password,tmp_path) as child:
        helper = Path(child['SSH_ASKPASS'])
        assert password not in helper.read_text()
        assert helper.stat().st_mode & 0o777 == 0o700
        result = subprocess.run([str(helper), "user@host's password: "],env=child,capture_output=True,check=True)
        assert result.stdout == (password+'\n').encode('utf-8')
        assert subprocess.run([str(helper),'Confirm host key?'],env=child,capture_output=True).returncode == 1
    assert not helper.exists()
    assert 'QC_FOLDER_SSH_PASSWORD' not in env


@pytest.mark.parametrize('failure',[False,True])
def test_password_command_and_cleanup(configuration,monkeypatch,failure):
    secret='only-in-child-env'
    known=Path(configuration['server']['data_dir'])/'ssh_known_hosts'
    configuration['hosts']['local']={'transport':'ssh','hostname':'worker.test','environment':'test',
        'ssh':{'python':'python3','timeout_seconds':30,'password':secret,
               'host_key_checking':'accept-new','known_hosts':str(known)}}
    helpers=[]
    def run(command, **kwargs):
        assert 'BatchMode=no' in command and 'StrictHostKeyChecking=accept-new' in command
        assert 'PreferredAuthentications=password' in command
        assert 'UserKnownHostsFile="'+str(known)+'"' in command
        assert secret not in json.dumps(command) + kwargs['input']
        assert kwargs['start_new_session']
        assert kwargs['env']['QC_FOLDER_SSH_PASSWORD']==secret
        helpers.append(Path(kwargs['env']['SSH_ASKPASS']))
        assert helpers[0].exists()
        if failure:raise subprocess.TimeoutExpired(command,30)
        return subprocess.CompletedProcess(command,0,json.dumps({'root_identity':[1,2]}),'')
    monkeypatch.setattr(subprocess,'run',run)
    if failure:
        from qcmanager.storage import StorageError
        with pytest.raises(StorageError):Storage(configuration).request(configuration['projects'][0],'probe')
    else:Storage(configuration).request(configuration['projects'][0],'probe')
    assert not helpers[0].exists()


def test_config_managed_known_hosts(configuration,tmp_path):
    configuration['hosts']['local']={'transport':'ssh','hostname':'worker.test','environment':'test',
        'ssh':{'password':'test-password','host_key_checking':'accept-new'}}
    path=tmp_path/'settings.yml';path.write_text(yaml.safe_dump(configuration))
    cfg=load_config(path)
    assert cfg['hosts']['local']['ssh']['known_hosts']==str(Path(cfg['server']['data_dir'])/'ssh_known_hosts')


@pytest.mark.parametrize('settings',[{'password':''},{'password':123},{'password':'x\ny'},
                                     {'host_key_checking':'no'},{'known_hosts':'/dev/null'}])
def test_invalid_ssh_auth_config(configuration,tmp_path,settings):
    configuration['hosts']['local']={'transport':'ssh','hostname':'worker.test','environment':'test','ssh':settings}
    path=tmp_path/'settings.yml';path.write_text(yaml.safe_dump(configuration))
    with pytest.raises(ConfigError):load_config(path)
