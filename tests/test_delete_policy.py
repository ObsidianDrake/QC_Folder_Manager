import copy

import pytest

from qcmanager.config import ConfigError, validate_config, load_config
from test_web_service import scanned, finished
from test_project_admin import admin, save


def test_project_policy_persists(web, configuration):
    app, client, headers = admin(web)
    project = client.get('/api/admin/projects').json['projects'][0]
    assert project['allow_user_delete'] is True
    project['allow_user_delete'] = False
    assert save(client, headers, 'update', project).status_code == 200
    assert load_config(configuration['_source_path'])['projects'][0]['allow_user_delete'] is False
    # Older clients omitting the setting must not accidentally reopen deletion.
    del project['allow_user_delete']
    assert save(client, headers, 'update', project).status_code == 200
    assert client.get('/api/admin/projects').json['projects'][0]['allow_user_delete'] is False


@pytest.mark.parametrize('value', ['false', 0, None, []])
def test_policy_requires_boolean(configuration, value):
    cfg = copy.deepcopy(configuration)
    cfg['projects'][0]['allow_user_delete'] = value
    with pytest.raises(ConfigError, match='allow_user_delete'):
        validate_config(cfg, configuration['_source_path'])


@pytest.mark.parametrize('is_admin,status,expected', [(False,'success','denied'), (True,'success','deleted'), (True,'running','rejected')])
def test_policy_enforced_and_admin_still_respects_drone(web, configuration, is_admin, status, expected):
    app, gitea, drone = web
    case, client, headers, rid = scanned(web, configuration)
    manager = app.extensions['manager']
    manager.projects['example-project']['allow_user_delete'] = False
    gitea.actor['is_admin'] = is_admin
    drone.status = status
    row = client.get('/api/records').json['records'][0]
    if not is_admin:
        assert not row['deletable']
        assert '未開放一般使用者刪除' in row['delete_reason']
    result = client.post('/api/delete', json={'records':[rid], 'confirm':True}, headers=headers)
    if expected == 'denied':
        assert result.status_code == 403
        assert not manager.db.operations({'example-project'})
    else:
        assert result.status_code == 202
        assert finished(app)['state'] == expected
    assert case.exists() == (expected != 'deleted')


def test_queued_delete_rechecks_policy(web, configuration, monkeypatch):
    app, gitea, _ = web
    case, client, headers, rid = scanned(web, configuration)
    manager = app.extensions['manager']
    queued = []
    monkeypatch.setattr(manager, 'submit', lambda fn,*args: queued.append((fn,args)))
    assert client.post('/api/delete', json={'records':[rid], 'confirm':True}, headers=headers).status_code == 202
    manager.projects['example-project']['allow_user_delete'] = False
    fn,args = next(item for item in queued if item[0] == manager._delete)
    fn(*args)
    assert finished(app)['state'] == 'rejected'
    assert case.exists()


def test_execution_uses_refreshed_admin_role(web, configuration, monkeypatch):
    app, gitea, _ = web
    case, client, headers, rid = scanned(web, configuration)
    manager = app.extensions['manager']
    manager.projects['example-project']['allow_user_delete'] = False
    gitea.actor['is_admin'] = True
    queued = []
    monkeypatch.setattr(manager, 'submit', lambda fn,*args: queued.append((fn,args)))
    assert client.post('/api/delete', json={'records':[rid], 'confirm':True}, headers=headers).status_code == 202
    gitea.actor['is_admin'] = False
    fn,args = next(item for item in queued if item[0] == manager._delete)
    fn(*args)
    assert finished(app)['state'] == 'rejected'
    assert case.exists()
