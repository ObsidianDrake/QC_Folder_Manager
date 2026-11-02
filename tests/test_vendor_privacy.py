import json
import time

import pytest

from conftest import login_client


@pytest.mark.parametrize('admin', [False, True])
def test_browser_endpoints_never_return_internal_storage_or_historical_secrets(web, monkeypatch, admin):
    app, gitea, _ = web
    gitea.actor.update(is_admin=admin, orgs=['ExampleVendor', 'private-org-marker'])
    manager=app.extensions['manager'];db=manager.db;pid='example-project'
    private='INTERNAL_STORAGE_MARKER'
    p=manager.projects[pid]
    p.update(host='internal-host-'+private, run_folder='/internal/'+private, prefix='private-prefix')
    manager.config['hosts'][p['host']]={'transport':'local','environment':private,'password':private}
    row={'name':'qc_1234','number':1234,'identity':[1,2],'root_identity':[1,3],
         'bytes':42,'error':None,'drone_status':'success','drone_checked_at':time.time(),
         'drone_url':'http://drone.test/ExampleVendor/Example_QC/1234',
         'size_error':private,'new_internal_field':private}
    db.start_scan(pid);db.finish_scan(pid,{'records':[row],'quarantines':[private]})
    stored=db.inventory({pid})[0][0]
    target=dict(stored, host=p['host'],path=p['run_folder']+'/qc_1234', organization=p['organization'],repository=p['repository'])
    oid=db.enqueue(gitea.actor,target,'batch')
    db.update_operation(oid,'failed',private)
    db.finish_scan(pid,error='Permission denied '+private)
    monkeypatch.setattr(manager.disks,'refresh',lambda *a,**k:0)
    manager.disks.items[pid]={'mount_id':private,'mount':'/mount/'+private,
        'total':100,'used':60,'available':40,'updated':time.time(),'attempted':time.time()}
    client,_=login_client(app,gitea)
    responses={}
    forbidden={'host','hostname','path','run_folder','mount','mount_id','environment','identity',
               'root_identity','ssh','password','orgs','prefix','new_internal_field','size_ticket'}
    def inspect(value):
        if isinstance(value,dict):
            assert not forbidden.intersection(value)
            for v in value.values():inspect(v)
        elif isinstance(value,list):
            for v in value:inspect(v)
    for endpoint in ('me','records','disks','operations','audit'):
        response=client.get('/api/'+endpoint)
        assert response.status_code==200
        assert private not in response.get_data(as_text=True)
        assert 'private-org-marker' not in response.get_data(as_text=True)
        responses[endpoint]=response.json
        inspect(response.json)
    assert responses['records']['projects']==[{'id':pid,'organization':'ExampleVendor','repository':'Example_QC'}]
    assert responses['records']['scans'][pid]['quarantine_count']==1
    assert responses['records']['records'][0]['name']=='qc_1234'
    assert responses['disks']['disks'][0]['available']==40
    assert responses['operations']['operations'][0]['actor']['login']=='vendor-one'
    assert db.operations({pid})[0]['target']['path']==target['path']
    assert private in json.dumps(db.audit({pid}))


def test_no_cross_org_disk_details(web, monkeypatch):
    app,gitea,_=web
    manager=app.extensions['manager']
    monkeypatch.setattr(manager.disks,'refresh',lambda *a,**k:0)
    gitea.actor['orgs']=['OtherVendor']
    client,_=login_client(app,gitea)
    assert client.get('/api/disks').json=={'disks':[]}
    assert client.get('/api/records').json['projects']==[]
