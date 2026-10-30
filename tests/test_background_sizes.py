import threading
from pathlib import Path

from conftest import login_client, wait_for
from qcmanager import fs_helper


def test_quick_scan_never_walks_contents(tmp_path, monkeypatch):
    (tmp_path/'qc_1').mkdir()
    monkeypatch.setattr(fs_helper, 'disk_size', lambda *a: (_ for _ in ()).throw(AssertionError('recursive scan')))
    result = fs_helper.execute({'action':'scan','root':str(tmp_path),'prefix':'qc','metadata_only':True})
    assert result['records'][0]['bytes'] is None
    assert result['records'][0]['error'] is None


def test_slow_size_does_not_block_inventory_and_cached_rescan(web, configuration, monkeypatch):
    app,gitea,_ = web
    m=app.extensions['manager']; root=Path(configuration['projects'][0]['run_folder'])
    (root/'qc_1').mkdir()
    entered,release=threading.Event(),threading.Event()
    original=m.storage.request; calls=[]
    def request(project,action,**fields):
        calls.append(action)
        if action=='size':
            entered.set()
            assert release.wait(5)
        return original(project,action,**fields)
    monkeypatch.setattr(m.storage,'request',request)
    client,headers=login_client(app,gitea)
    try:
        client.post('/api/scan',json={'projects':['example-project']},headers=headers)
        assert entered.wait(5)
        wait_for(lambda: m.db.inventory({'example-project'})[1]['example-project']['state']=='ready')
        row=client.get('/api/records').json['records'][0]
        assert row['bytes'] is None and row['size_state']=='running'
        assert row['drone_status']=='success'
        for _ in range(3):assert client.get('/api/records').status_code==200
        assert calls.count('scan')==1 and calls.count('size')==1
        assert client.post('/api/sizes',json={'records':[row['id']]},headers=headers).json['accepted']==0
        release.set()
        wait_for(lambda: m.db.record(row['id']).get('size_at'))
        wait_for(lambda: not m.pending_scans)
        m.scan('example-project',gitea.actor)
        wait_for(lambda: not m.pending_scans)
        assert calls.count('size')==1
        assert m.db.record(row['id'])['bytes'] is not None
        assert client.post('/api/sizes',json={'records':[row['id']]}).status_code==403
        gitea.actor['orgs']=[]
        assert client.post('/api/sizes',json={'records':[row['id']]},headers=headers).status_code==404
    finally:
        release.set()


def test_late_size_result_cannot_restore_deleted_record(web, configuration):
    app,_,_=web;m=app.extensions['manager'];p=configuration['projects'][0]
    (Path(p['run_folder'])/'qc_1').mkdir()
    result=m.storage.request(p,'scan',metadata_only=True)
    m.db.start_scan(p['id']);m.db.finish_scan(p['id'],result)
    row=m.db.inventory({p['id']})[0][0]
    m.db.patch_record(row,{'size_ticket':'old'})
    result['records'][0]['identity']=[999,999]
    m.db.finish_scan(p['id'],result)
    assert not m.db.patch_record(row,{'bytes':999},ticket='old')
    assert m.db.inventory({p['id']})[0][0]['bytes'] is None


def test_size_failure_keeps_previous_value_and_restart_clears_busy(web, configuration, monkeypatch):
    app,_,_=web;m=app.extensions['manager'];p=configuration['projects'][0]
    (Path(p['run_folder'])/'qc_1').mkdir()
    m.db.start_scan(p['id']);m.db.finish_scan(p['id'],m.storage.request(p,'scan',metadata_only=True))
    row=m.db.inventory({p['id']})[0][0]
    m.db.patch_record(row,{'bytes':123,'size_at':10})
    monkeypatch.setattr(m.storage,'request',lambda *a,**kw: (_ for _ in ()).throw(OSError('offline')))
    m.queue_sizes([row]);wait_for(lambda:m.db.record(row['id']).get('size_state')=='failed')
    assert m.db.record(row['id'])['bytes']==123
    m.db.patch_record(row,{'size_state':'queued','size_ticket':'x'})
    m.db.recover()
    assert m.db.record(row['id'])['size_state']=='interrupted'
    assert m.db.record(row['id'])['size_ticket'] is None
