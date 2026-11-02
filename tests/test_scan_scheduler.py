import copy
from pathlib import Path
import threading
import time

import pytest

from conftest import login_client, wait_for
from qcmanager.config import ConfigError, validate_config
from qcmanager.scan_scheduler import ScanScheduler


def scheduler(web):
    app, _, _ = web
    manager = app.extensions['manager']
    return manager, ScanScheduler(manager, app.extensions['project_settings'], 300)


def completed(manager, pid='example-project'):
    return pid in manager.scan_finished and pid not in manager.pending_scans and not manager.pending_sizes


def test_startup_and_periodic_scans_without_browser_or_gitea_login(web, monkeypatch):
    app, gitea, _ = web
    manager, _ = scheduler(web)
    root = Path(manager.projects['example-project']['run_folder'])
    (root/'qc_1').mkdir()
    monkeypatch.setattr(gitea, 'identity', lambda *_: pytest.fail('Automatic scan must not require a user session'))
    manager.start_scanning(app.extensions['project_settings'])
    wait_for(lambda: completed(manager))
    assert len(manager.db.inventory({'example-project'})[0]) == 1
    thread = manager.scan_scheduler.thread
    manager.start_scanning(app.extensions['project_settings'])
    assert manager.scan_scheduler.thread is thread
    (root/'qc_2').mkdir()
    with manager.guard:
        manager.scan_finished['example-project'] -= 301
    wait_for(lambda: len(manager.db.inventory({'example-project'})[0]) == 2)
    wait_for(lambda: completed(manager))
    manager.shutdown()
    assert not thread.is_alive()


def test_auto_rescan_refreshes_metadata_and_drone_but_keeps_cached_sizes(web):
    manager, auto = scheduler(web)
    root = Path(manager.projects['example-project']['run_folder'])
    (root/'qc_1').mkdir(); (root/'qc_1'/'data').write_text('first')
    web[2].status = 'running'
    auto.poll(); wait_for(lambda: completed(manager))
    before = manager.db.inventory({'example-project'})[0][0]
    assert before['drone_status'] == 'running'
    (root/'qc_2').mkdir(); (root/'qc_1'/'data').write_text('more'*10000)
    web[2].status = 'success'
    auto.poll()  # The interval has not elapsed.
    assert len(manager.db.inventory({'example-project'})[0]) == 1
    manager.scan_finished['example-project'] -= 301
    auto.poll(); wait_for(lambda: completed(manager))
    rows = manager.db.inventory({'example-project'})[0]
    assert len(rows) == 2
    after = next(row for row in rows if row['number'] == 1)
    assert after['drone_status'] == 'success'
    assert after['scanned_at'] > before['scanned_at']
    assert (after['bytes'], after['size_at']) == (before['bytes'], before['size_at'])
    assert all(row['size_at'] for row in rows)


def test_manual_rescan_bypasses_interval_but_deduplicates_active_scan(web, monkeypatch):
    manager, auto = scheduler(web)
    entered, release = threading.Event(), threading.Event()
    original = manager.storage.request
    calls = []

    def request(project, action, **kwargs):
        if action == 'scan':
            calls.append(action); entered.set()
            assert release.wait(5)
        return original(project, action, **kwargs)

    monkeypatch.setattr(manager.storage, 'request', request)
    client, headers = login_client(web[0], web[1])
    try:
        auto.poll(); assert entered.wait(5)
        auto.poll()
        assert client.post('/api/scan', json={'projects':['example-project']}, headers=headers).status_code == 202
        assert calls == ['scan']
    finally:
        release.set()
    wait_for(lambda: completed(manager))
    auto.poll(); assert calls == ['scan']
    assert client.post('/api/scan', json={'projects':['example-project']}, headers=headers).status_code == 202
    wait_for(lambda: completed(manager) and len(calls) == 2)
    web[1].actor['orgs'] = []
    assert client.post('/api/scan', json={'projects':['example-project']}, headers=headers).status_code == 404


def test_failure_retries_after_interval_and_other_projects_continue(web, monkeypatch):
    manager, auto = scheduler(web)
    second = dict(manager.projects['example-project'], id='other', repository='Other', organization='OtherVendor')
    manager.projects['other'] = second
    manager.locks['other'] = threading.Lock()
    original = manager.storage.request
    calls = []

    def request(project, action, **kwargs):
        if action == 'scan':
            calls.append(project['id'])
            if project['id'] == 'example-project':
                raise OSError('private connection diagnostic')
        return original(project, action, **kwargs)

    monkeypatch.setattr(manager.storage, 'request', request)
    auto.poll(); wait_for(lambda: completed(manager) and completed(manager, 'other'))
    assert manager.db.inventory({'example-project'})[1]['example-project']['state'] == 'error'
    assert manager.db.inventory({'other'})[1]['other']['state'] == 'ready'
    auto.poll(); assert len(calls) == 2
    manager.scan_finished['example-project'] -= 301
    auto.poll(); wait_for(lambda: completed(manager) and calls.count('example-project') == 2)
    assert calls.count('other') == 1
    client, _ = login_client(web[0], web[1])
    result = client.get('/api/records').get_data(as_text=True)
    assert 'private connection diagnostic' not in result and 'OtherVendor' not in result


def test_scheduler_respects_settings_lock_and_broken_state(web, monkeypatch):
    manager, auto = scheduler(web)
    calls = []
    monkeypatch.setattr(manager, '_queue_scan', calls.append)
    auto.settings.broken = True
    auto.poll(); assert calls == []
    auto.settings.broken = False
    started, finished = threading.Event(), threading.Event()

    def poll():
        started.set(); auto.poll(); finished.set()

    with auto.settings.lock:
        thread = threading.Thread(target=poll); thread.start()
        assert started.wait(1)
        assert not finished.wait(.05)
        manager.projects.clear()
    thread.join(2)
    assert finished.is_set() and calls == []


def test_admin_changes_trigger_scan_and_removal_cancels_future_scans(web):
    from test_project_admin import admin, new_project, save
    app, client, headers = admin(web)
    manager, auto = scheduler(web)
    auto.poll(); wait_for(lambda: completed(manager))
    project = new_project(manager.config)
    Path(project['run_folder']).mkdir()
    assert save(client, headers, 'create', project).status_code == 200
    auto.poll(); wait_for(lambda: completed(manager, project['id']))
    project['prefix'] = 'new'
    (Path(project['run_folder'])/'new_9').mkdir()
    assert save(client, headers, 'update', project).status_code == 200
    auto.poll(); wait_for(lambda: completed(manager, project['id']))
    assert manager.db.inventory({project['id']})[0][0]['name'] == 'new_9'
    assert save(client, headers, 'delete', pid=project['id']).status_code == 200
    auto.poll()
    assert project['id'] not in manager.scan_finished
    assert manager.db.inventory({project['id']})[0] == []


def test_queue_saturation_backs_off_and_stop_prevents_new_work(web, monkeypatch):
    manager, auto = scheduler(web)
    calls = []

    def full(*args):
        calls.append(args)
        raise RuntimeError('queue full')

    monkeypatch.setattr(manager, 'submit', full)
    auto.poll(); assert not manager.pending_scans and len(calls) == 1
    auto.poll(); assert len(calls) == 1
    manager.scan_finished['example-project'] -= 301
    auto.stop_event.set(); auto.poll(); assert len(calls) == 1


@pytest.mark.parametrize('value', [0, 59, 86401, True, '300', 1.5])
def test_invalid_scan_interval_rejected(configuration, value):
    configuration['server']['scan_interval_seconds'] = value
    with pytest.raises(ConfigError, match='scan_interval_seconds'):
        validate_config(configuration, configuration['_source_path'])


def test_default_scan_interval_preserves_existing_configuration(configuration):
    config = copy.deepcopy(configuration)
    config['server'].pop('scan_interval_seconds')
    assert validate_config(config, config['_source_path'])['server']['scan_interval_seconds'] == 300
