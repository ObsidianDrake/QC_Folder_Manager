import os
import uuid
from pathlib import Path

import pytest

from qcmanager import fs_helper


def request(root,action='scan',**kwargs):
    return fs_helper.execute(dict(root=str(root),prefix='qc',action=action,**kwargs))


def deletion(row):
    return dict(name=row['name'],identity=row['identity'],root_identity=row['root_identity'],operation_id=uuid.uuid4().hex)


def test_exact_names_and_size(tmp_path):
    for name in ['qc_1234','qc_0','qc_001','qc_5_backup','other_4']:
        (tmp_path/name).mkdir()
    (tmp_path/'qc_1234'/'result').write_text('result')
    (tmp_path/'qc_9').write_text('file not folder')
    rows=request(tmp_path)['records']
    assert [r['number'] for r in rows]==[1234]
    assert rows[0]['bytes']>0


def test_delete_nested_tree_without_following_links(tmp_path):
    root=tmp_path/'root';root.mkdir()
    outside=tmp_path/'outside';outside.mkdir();(outside/'valuable').write_text('keep')
    case=root/'qc_1234';(case/'nested').mkdir(parents=True)
    (case/'nested'/'output').write_text('remove')
    (case/'external').symlink_to(outside,target_is_directory=True)
    (root/'qc_99').symlink_to(outside,target_is_directory=True)
    row=request(root)['records'][0]
    result=request(root,'delete',**deletion(row))
    assert result['status']=='deleted'
    assert not case.exists()
    assert (outside/'valuable').read_text()=='keep'
    assert (root/'qc_99').is_symlink()


def test_replaced_inode_is_rejected(tmp_path):
    case=tmp_path/'qc_1234';case.mkdir()
    row=request(tmp_path)['records'][0]
    case.rename(tmp_path/'saved');case.mkdir();(case/'new').write_text('keep')
    assert request(tmp_path,'delete',**deletion(row))['status']=='failed'
    assert (case/'new').exists()


@pytest.mark.parametrize('name',['../outside','qc_2/../outside','/','qc_1234_backup'])
def test_invalid_delete_target(tmp_path,name):
    case=tmp_path/'qc_1234';case.mkdir()
    row=request(tmp_path)['records'][0];params=deletion(row);params['name']=name
    assert request(tmp_path,'delete',**params)['status']=='failed'
    assert case.exists()


def test_symlink_root_rejected(tmp_path):
    actual=tmp_path/'actual';actual.mkdir()
    alias=tmp_path/'alias';alias.symlink_to(actual)
    with pytest.raises(OSError):
        request(alias)


def test_nested_mount_refused_before_delete(tmp_path,monkeypatch):
    case=tmp_path/'qc_1234';case.mkdir();(case/'child').mkdir()
    row=request(tmp_path)['records'][0]
    original=fs_helper.mount_id
    def mount(fd):
        target=os.readlink('/proc/self/fd/%d'%fd)
        return 'different-mount' if target.endswith('/child') else original(fd)
    monkeypatch.setattr(fs_helper,'mount_id',mount)
    assert request(tmp_path,'delete',**deletion(row))['status']=='failed'
    assert case.exists()


def test_partial_deletion_retains_quarantine(tmp_path,monkeypatch):
    case=tmp_path/'qc_1';case.mkdir();(case/'file').write_text('data')
    row=request(tmp_path)['records'][0]
    def fail(fd):
        raise PermissionError('simulated failure')
    monkeypatch.setattr(fs_helper,'empty_dir',fail)
    result=request(tmp_path,'delete',**deletion(row))
    assert result['status']=='partial'
    assert (tmp_path/result['quarantine']/'file').exists()
    assert request(tmp_path)['quarantines']==[result['quarantine']]
