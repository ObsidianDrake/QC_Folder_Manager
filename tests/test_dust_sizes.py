import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from qcmanager import fs_helper
from qcmanager.config import ConfigError, validate_config
from qcmanager.storage import Storage, StorageError


def request(tmp_path, **options):
    folder = tmp_path / 'qc_1'
    folder.mkdir(exist_ok=True)
    options.setdefault('executable', '/tools/dust')
    return dict(action='size', root=str(tmp_path), prefix='qc', name='qc_1',
                identity=fs_helper.identity(folder.stat()),
                root_identity=fs_helper.identity(tmp_path.stat()), dust=options)


def test_dust_command_is_anchored_and_ignores_user_config(tmp_path, monkeypatch):
    body = request(tmp_path, executable='/opt/tools/dust', threads=3, timeout_seconds=40)
    def run(command, **kwargs):
        assert command[0] == '/opt/tools/dust'
        assert command[-3:] == ['3', '--', '.']
        assert '-s' not in command and '-L' not in command
        assert all(flag in command for flag in ('-j', '-x', '-P'))
        assert kwargs['timeout'] == 40
        assert kwargs['env']['LC_ALL'] == 'C'
        assert kwargs['encoding'] == 'utf-8'
        assert os.path.samefile(kwargs['cwd'], tmp_path/'qc_1')
        assert '--config' not in command
        assert os.listdir(kwargs['env']['HOME']) == []
        assert kwargs['env']['HOME'] == kwargs['env']['XDG_CONFIG_HOME']
        return SimpleNamespace(returncode=0, stdout='{"size":"16384B"}', stderr='')
    monkeypatch.setattr(fs_helper.subprocess, 'run', run)
    assert fs_helper.execute(body)['bytes'] == 16384


@pytest.mark.parametrize('output', ['{}', 'not json', '{"size":-1}', '{"size":true}',
                                   '{"size":"1.2G"}', '[]'])
def test_invalid_dust_output_never_becomes_zero(tmp_path, monkeypatch, output):
    monkeypatch.setattr(fs_helper.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=output, stderr=''))
    assert fs_helper.execute(request(tmp_path))['reason'] == 'dust_invalid_output'


@pytest.mark.parametrize('stderr,code,expected', [
    ('No such file or directory: ./gone\n', 0, None),
    ('Did not have permissions for all directories\n', 0, 'dust_permission_denied'),
    ('Unknown Error: input/output error\n', 0, 'dust_scan_error'),
    ('', 2, 'dust_exit_failed'),
    ('No such file or directory: ./gone\nUnknown Error: disk\n', 0, 'dust_scan_error'),
])
def test_dust_missing_entries_allowed_but_partial_error_rejected(tmp_path, monkeypatch, stderr, code, expected):
    monkeypatch.setattr(fs_helper.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=code, stdout='{"size":"4096B"}', stderr=stderr))
    result = fs_helper.execute(request(tmp_path))
    if expected:
        assert result['reason'] == expected and 'bytes' not in result
    else:
        assert result['bytes'] == 4096


@pytest.mark.parametrize('error,reason', [
    (FileNotFoundError(2, 'missing'), 'dust_not_found'),
    (PermissionError(13, 'denied'), 'dust_start_failed'),
    (subprocess.TimeoutExpired(['dust'], 7), 'dust_timeout'),
])
def test_dust_failure_provides_structured_diagnostics(tmp_path, monkeypatch, error, reason):
    def run(*args, **kwargs):
        raise error
    monkeypatch.setattr(fs_helper.subprocess, 'run', run)
    result = fs_helper.execute(request(tmp_path, timeout_seconds=7))
    assert result['reason'] == reason
    if reason == 'dust_timeout':
        assert result['timeout_seconds'] == 7


def test_storage_keeps_dust_reason(configuration, tmp_path, monkeypatch):
    monkeypatch.setattr(fs_helper, 'execute', lambda body: dict(error='failed', error_type='DustError',
                                                            reason='dust_timeout', timeout_seconds=20))
    with pytest.raises(StorageError) as error:
        Storage(configuration).request(configuration['projects'][0], 'size')
    assert error.value.details == {'reason':'dust_timeout', 'timeout_seconds':20}


@pytest.mark.parametrize('options', [dict(threads=0), dict(threads=True), dict(threads=65),
                                   dict(timeout_seconds=0), dict(executable='../dust'),
                                   dict(executable='-dust'), dict(other=True)])
def test_invalid_dust_settings(configuration, options):
    configuration['hosts']['local']['dust'] = options
    with pytest.raises(ConfigError, match='dust'):
        validate_config(configuration, configuration['_source_path'])


def test_real_dust_counts_hidden_sparse_hardlinks_without_following_symlinks(tmp_path, monkeypatch):
    executable = shutil.which('dust')
    if not executable:
        pytest.skip('Install dust to run the real binary integration test')
    home = tmp_path/'user-home'; home.mkdir()
    (home/'.dust.toml').write_text('display-apparent-size = true\nignore-hidden = true\n', encoding='utf-8')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home))
    body = request(tmp_path, executable=executable)
    folder = tmp_path/'qc_1'
    (folder/'.hidden').write_bytes(b'x'*8192)
    os.link(folder/'.hidden', folder/'hardlink')
    with (folder/'sparse').open('wb') as f:
        f.truncate(1024*1024*1024)
    (folder/'nested').mkdir()
    fd = os.open(str(folder), fs_helper.FLAGS)
    try:
        # Exact comparison without symlinks still checks hidden files, sparse
        # allocation, hard-link deduplication and isolation from user config.
        assert fs_helper.execute(body)['bytes'] == fs_helper.disk_size(fd)
        # A long link target allocates a block on ext4, unlike an inline link.
        # Some filesystems report zero blocks even for this long symlink.
        outside = tmp_path/('outside-' + 'x'*100); outside.mkdir()
        (outside/'large').write_bytes(b'x'*1048576)
        link = folder/'link'
        link.symlink_to(outside, target_is_directory=True)
        expected = fs_helper.disk_size(fd)
        link_bytes = link.lstat().st_blocks * 512
        # Dust 1.1.1 excludes the link inode; 1.2.7 includes its allocated
        # blocks. Neither may traverse/count the external 1 MiB file.
        assert fs_helper.execute(body)['bytes'] in {expected - link_bytes, expected}
    finally:
        os.close(fd)


def test_replaced_folder_rejected_before_dust(tmp_path, monkeypatch):
    body = request(tmp_path)
    (tmp_path/'qc_1').rename(tmp_path/'old')
    (tmp_path/'qc_1').mkdir()
    monkeypatch.setattr(fs_helper, 'dust_size', lambda *a: pytest.fail('must reject stale identity'))
    assert fs_helper.execute(body)['status'] == 'failed'


def test_remote_size_passes_dust_settings_and_shorter_timeout(configuration, monkeypatch):
    config = configuration
    config['hosts']['local'] = dict(transport='ssh', hostname='example.test',
        dust=dict(executable='/tools/dust', threads=2, timeout_seconds=100),
        ssh=dict(python='python3', timeout_seconds=40))
    def run(command, **kwargs):
        body = json.loads(kwargs['input'])
        assert body['dust'] == dict(executable='/tools/dust', threads=2, timeout_seconds=35)
        assert kwargs['timeout'] == 40
        return SimpleNamespace(returncode=0, stdout='{"root_identity":[1,2],"bytes":4096}', stderr='')
    monkeypatch.setattr(subprocess, 'run', run)
    result = Storage(config).request(config['projects'][0], 'size', name='qc_1',
                                     identity=[1,3], root_identity=[1,2])
    assert result['bytes'] == 4096


def test_missing_dust_on_path_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(fs_helper.shutil, 'which', lambda _: None)
    assert fs_helper.execute(request(tmp_path, executable='dust'))['reason'] == 'dust_not_found'
