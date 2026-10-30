import json
from pathlib import Path

import pytest

from qcmanager.clients import APIError
from qcmanager.storage import StorageError
from conftest import login_client,wait_for


def scanned(web,configuration):
    app,gitea,drone=web
    case=Path(configuration['projects'][0]['run_folder'])/'qc_1234'
    case.mkdir();(case/'output').write_text('QC result')
    client,headers=login_client(app,gitea)
    assert client.post('/api/scan',json={'projects':['example-project']},headers=headers).status_code==202
    wait_for(lambda: app.extensions['manager'].db.inventory({'example-project'})[1].get('example-project',{}).get('state')=='ready')
    rid=client.get('/api/records').json['records'][0]['id']
    return case,client,headers,rid


def finished(app):
    return wait_for(lambda: next((o for o in app.extensions['manager'].db.operations({'example-project'}) if o['state'] not in ('queued','running')),None))


def test_login_required_and_csrf(web):
    app,gitea,_=web
    assert app.test_client().get('/api/records').status_code==401
    client,_=login_client(app,gitea)
    assert client.post('/api/delete',json={'records':['x'],'confirm':True}).status_code==403


def test_delete_persists_actor_and_audit_after_folder_removed(web,configuration):
    app,gitea,_=web
    case,client,headers,rid=scanned(web,configuration)
    result=client.post('/api/delete',json={'records':[rid],'confirm':True},headers=headers)
    assert result.status_code==202
    op=finished(app)
    assert op['state']=='deleted' and not case.exists()
    assert op['actor']['login']=='vendor-one' and op['actor']['id']==1
    audit=client.get('/api/audit').json['events']
    assert {e['event'] for e in audit}>={'requested','running','deleted'}
    assert all('secret-token' not in json.dumps(e) for e in audit)


@pytest.mark.parametrize('status',['running','pending','blocked','waiting_on_dependencies','unexpected'])
def test_admin_cannot_bypass_drone_state(web,configuration,status):
    app,gitea,drone=web
    case,client,headers,rid=scanned(web,configuration)
    gitea.actor.update(is_admin=True,orgs=[])
    drone.status=status
    assert client.post('/api/delete',json={'records':[rid],'confirm':True},headers=headers).status_code==202
    assert finished(app)['state']=='rejected'
    assert case.exists()


def test_cross_org_cannot_list_delete_or_read_audit(web,configuration):
    app,gitea,_=web
    case,client,headers,rid=scanned(web,configuration)
    gitea.actor['orgs']=['OtherVendor']
    assert client.get('/api/records').json['records']==[]
    assert client.post('/api/delete',json={'records':[rid],'confirm':True},headers=headers).status_code==404
    assert client.get('/api/audit').json['events']==[]
    assert case.exists()


def test_admin_can_delete_other_org(web,configuration):
    app,gitea,_=web
    case,client,headers,rid=scanned(web,configuration)
    gitea.actor.update(is_admin=True,orgs=['Other'])
    assert len(client.get('/api/records').json['records'])==1
    client.post('/api/delete',json={'records':[rid],'confirm':True},headers=headers)
    assert finished(app)['state']=='deleted'


def test_upstream_failure_blocks_filesystem(web,configuration,monkeypatch):
    app,_,drone=web
    case,client,headers,rid=scanned(web,configuration)
    monkeypatch.setattr(drone,'build',lambda *a: (_ for _ in ()).throw(APIError()))
    client.post('/api/delete',json={'records':[rid],'confirm':True},headers=headers)
    assert finished(app)['state']=='rejected' and case.exists()


def test_audit_write_failure_prevents_delete(web,configuration,monkeypatch):
    app,_,_=web
    case,client,headers,rid=scanned(web,configuration)
    monkeypatch.setattr(app.extensions['manager'].db,'enqueue',lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    assert client.post('/api/delete',json={'records':[rid],'confirm':True},headers=headers).status_code==500
    assert case.exists()


def test_recheck_membership_when_worker_starts(web,configuration,monkeypatch):
    app,gitea,_=web
    case,client,headers,rid=scanned(web,configuration)
    pending=[]
    manager=app.extensions['manager']
    monkeypatch.setattr(manager,'submit',lambda fn,*args: pending.append((fn,args)))
    client.post('/api/delete',json={'records':[rid],'confirm':True},headers=headers)
    gitea.actor['orgs']=[]
    pending[0][0](*pending[0][1])
    assert finished(app)['state']=='rejected' and case.exists()


def test_duplicate_requests_blocked_and_ssh_uncertainty_retained(web,configuration,monkeypatch):
    app,_,_=web
    case,client,headers,rid=scanned(web,configuration)
    manager=app.extensions['manager'];pending=[]
    monkeypatch.setattr(manager,'submit',lambda fn,*args: pending.append((fn,args)))
    body={'records':[rid],'confirm':True}
    first=client.post('/api/delete',json=body,headers=headers).json
    second=client.post('/api/delete',json=body,headers=headers).json
    assert first['results'][0].get('operation') and second['results'][0].get('error')
    monkeypatch.setattr(manager.storage,'request',lambda *a,**k: (_ for _ in ()).throw(StorageError('timeout',True)))
    pending[0][0](*pending[0][1])
    assert finished(app)['state']=='unknown' and case.exists()
    assert client.post('/api/delete',json=body,headers=headers).json['results'][0].get('error')


def test_reused_build_number_rejected(web,configuration):
    app,_,drone=web
    case,client,headers,rid=scanned(web,configuration)
    drone.created=101
    client.post('/api/delete',json={'records':[rid],'confirm':True},headers=headers)
    assert finished(app)['state']=='rejected' and case.exists()


def test_oauth_state_single_use_and_cookie_security(web):
    app,_,_=web;client=app.test_client()
    first=client.get('/auth/login')
    assert first.status_code==302
    from urllib.parse import urlsplit,parse_qs
    state=parse_qs(urlsplit(first.location).query)['state'][0]
    assert client.get('/auth/callback?code=anything&state=bad').status_code==400
    callback=client.get('/auth/callback',query_string={'code':'valid','state':state})
    assert callback.status_code==302
    assert 'HttpOnly' in callback.headers['Set-Cookie'] and 'SameSite=Lax' in callback.headers['Set-Cookie']
    assert 'private-oauth-token' not in callback.headers['Set-Cookie']
    assert client.get('/api/me').json['user']['login']=='vendor-one'
    assert client.get('/auth/callback',query_string={'code':'valid','state':state}).status_code==400
