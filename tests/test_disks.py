import threading
from types import SimpleNamespace

from conftest import login_client, wait_for
from qcmanager import fs_helper
from qcmanager.disks import DiskCache


def test_disk_query_reads_available_blocks_without_walking(tmp_path,monkeypatch):
    monkeypatch.setattr(fs_helper.os,'listdir',lambda *a: (_ for _ in ()).throw(AssertionError('must not walk')))
    monkeypatch.setattr(fs_helper.os,'fstatvfs',lambda fd:SimpleNamespace(f_frsize=4096,f_blocks=100,f_bfree=30,f_bavail=20))
    result=fs_helper.execute({'action':'disk','root':str(tmp_path)})
    assert result['total']==409600 and result['available']==81920 and result['used']==286720
    assert result['mount'].startswith('/') and result['mount_id']


def project(pid,host='worker'):
    return {'id':pid,'host':host,'organization':'Org-'+pid,'repository':'QC'}


def test_grouping_filters_projects_and_keeps_hosts_separate():
    projects={p['id']:p for p in [project('a'),project('b'),project('c','other')]}
    cache=DiskCache(None,projects)
    try:
        import time
        for pid in projects:
            cache.items[pid]={'mount_id':'7','mount':'/data','updated':time.time(),'attempted':time.time(),
                              'available':40,'total':100,'used':60}
        assert len(cache.snapshot(projects))==2
        one=cache.snapshot({'a':projects['a']})
        assert len(one)==1 and [p['id'] for p in one[0]['projects']]==['a']
        assert 'Org-b' not in str(one)
    finally:cache.shutdown()


def test_slow_query_returns_pending_and_failure_preserves_cache():
    entered,release=threading.Event(),threading.Event()
    class Storage:
        fail=False
        def request(self,*a):
            entered.set();assert release.wait(5)
            if self.fail:raise OSError('offline')
            return {'mount_id':'1','mount':'/data','total':100,'used':60,'available':40}
    storage=Storage();projects={'a':project('a')};cache=DiskCache(storage,projects)
    try:
        rows=cache.snapshot(projects)
        assert rows[0]['refreshing'] and rows[0]['available'] is None
        assert entered.wait(5)
        assert cache.refresh(projects,manual=True)==0
        release.set();wait_for(lambda:not cache.pending)
        assert cache.snapshot(projects)[0]['available']==40
        storage.fail=True;cache.invalidate('a');wait_for(lambda:not cache.pending)
        row=cache.snapshot(projects)[0]
        assert row['available']==40 and row['error'] and row['stale']
    finally:release.set();cache.shutdown()


def test_disk_api_authorization_and_csrf(web):
    app,gitea,_=web;client,headers=login_client(app,gitea)
    assert app.test_client().get('/api/disks').status_code==401
    assert client.post('/api/disks/refresh',json={'projects':['example-project']}).status_code==403
    assert client.post('/api/disks/refresh',json={'projects':['other']},headers=headers).status_code==404
    assert client.get('/api/disks').status_code==200
    gitea.actor['orgs']=[]
    assert client.get('/api/disks').json['disks']==[]
    assert client.post('/api/disks/refresh',json={'projects':['example-project']},headers=headers).status_code==404
