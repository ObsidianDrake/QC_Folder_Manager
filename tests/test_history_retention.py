import time
from conftest import login_client


def test_history_pages_and_admin_audit(web):
    app,gitea,_=web;db=app.extensions['manager'].db
    for i in range(105):
        db.enqueue(gitea.actor,dict(id=str(i),project='example-project',name='qc_'+str(i)), 'batch')
    client,_=login_client(app,gitea)
    pages=[client.get('/api/operations?page='+str(i)).json for i in (1,2,3)]
    assert [len(p['operations']) for p in pages]==[50,50,5]
    assert [p['has_more'] for p in pages]==[True,True,False]
    assert len({op['id'] for p in pages for op in p['operations']})==105
    assert client.get('/api/operations?page=bad').status_code==400
    assert client.get('/api/audit').status_code==403
    gitea.actor['is_admin']=True
    assert client.get('/api/audit').status_code==200
    gitea.actor.update(is_admin=False,orgs=[])
    assert client.get('/api/operations').json['operations']==[]


def test_purge_preserves_unresolved_work_and_recent_completion(web):
    app,gitea,_=web;db=app.extensions['manager'].db;now=time.time();ids={}
    for state in ('deleted','failed','rejected','cancelled','queued','running','unknown','partial'):
        oid=db.enqueue(gitea.actor,dict(id=state,project='example-project',name='qc_1'),'batch')
        db.update_operation(oid,state);ids[state]=oid
    recent=db.enqueue(gitea.actor,dict(id='recent',project='example-project',name='qc_2'),'batch')
    db.update_operation(recent,'deleted')
    with db.connect() as conn:
        conn.execute('UPDATE operations SET created=?,updated=? WHERE id!=?',(now-31*86400,now-31*86400,recent))
        conn.execute('UPDATE audit SET at=? WHERE operation!=?',(now-31*86400,recent))
    db.purge_history(now)
    remaining={o['id'] for o in db.operations({'example-project'})}
    assert remaining=={recent}|{ids[s] for s in ('queued','running','unknown','partial')}
    assert {e['operation'] for e in db.audit({'example-project'})}==remaining
    db.purge_history(now)
    assert {o['id'] for o in db.operations({'example-project'})}==remaining
