import copy
import logging
import signal
import socket
from types import SimpleNamespace

import pytest

from qcmanager.config import ConfigError, validate_config
from qcmanager.route_registration import RouteAPIError, RouteRegistration
from qcmanager.server import bind_listener, run_service


def enabled(configuration):
    config = copy.deepcopy(configuration)
    config['server']['public_url'] = 'http://tools.example.com/qc_folder_mgt'
    config['traefik_api'] = {'enable': True, 'base_url': 'http://api.example.com:8000',
                             'token': 'private-test-route-token-24'}
    return validate_config(config, config['_source_path'])


@pytest.mark.parametrize('port', [None, 0, 9080, 65535])
def test_port_selection_config(configuration, port):
    configuration['server']['port'] = port
    result = validate_config(configuration, configuration['_source_path'])
    assert result['server']['port'] == (port or 0)


def test_missing_port_and_disabled_registration(configuration, monkeypatch):
    del configuration['server']['port']
    result = validate_config(configuration, configuration['_source_path'])
    assert result['server']['port'] == 0
    route = RouteRegistration(result)
    monkeypatch.setattr(route, 'request', lambda *args: pytest.fail('Disabled integration called API'))
    route.register(23456)
    route.unregister()


@pytest.mark.parametrize('section,key,value', [
    ('server', 'port', True), ('server', 'port', '9080'), ('server', 'port', -1),
    ('server', 'port', 65536), ('traefik_api', 'enable', 'false'),
    ('traefik_api', 'token', ''), ('traefik_api', 'token', 'secret\nvalue'),
    ('traefik_api', 'base_url', 'http://user:secret@api.test'),
    ('traefik_api', 'base_url', 'http://api.test:bad'),
    ('traefik_api', 'base_url', 'http://[invalid'),
    ('traefik_api', 'route_name', 'Bad_Name'), ('traefik_api', 'route_name', '../other'),
    ('traefik_api', 'upstream_host', '0.0.0.0'), ('traefik_api', 'upstream_host', '::'),
    ('traefik_api', 'upstream_host', 'http://server:9000'),
    ('traefik_api', 'timeout_seconds', True), ('traefik_api', 'timeout_seconds', 0),
])
def test_invalid_route_configuration(configuration, section, key, value):
    config = enabled(configuration)
    config[section][key] = value
    with pytest.raises(ConfigError):
        validate_config(config, config['_source_path'])


def test_wildcard_listener_requires_reachable_upstream(configuration):
    config = enabled(configuration)
    config['server']['listen'] = '0.0.0.0'
    del config['traefik_api']['upstream_host']
    with pytest.raises(ConfigError, match='upstream_host'):
        validate_config(config, config['_source_path'])
    config['traefik_api']['upstream_host'] = 'worker.example.com'
    validate_config(config, config['_source_path'])


@pytest.mark.parametrize('public,upstream,prefix,origin', [
    ('http://TOOLS.example.com:8080/qc_folder_mgt', 'worker.example.com', '/qc_folder_mgt', 'http://worker.example.com:23456'),
    ('https://tools.example.com', '::1', '/', 'http://[::1]:23456'),
])
def test_payload_and_canonical_name_cleanup(configuration, monkeypatch, public, upstream, prefix, origin):
    config = enabled(configuration)
    config['server']['public_url'] = public
    config['traefik_api']['upstream_host'] = upstream
    config = validate_config(config, config['_source_path'])
    registration = RouteRegistration(config)
    calls, saved = [], {}

    def request(method, name, payload=None):
        calls.append((method, name, payload))
        if method == 'PUT':
            saved.update(name='retained-name', route=payload, status='written')
        return saved if method != 'DELETE' else None

    monkeypatch.setattr(registration, 'request', request)
    registration.register(23456)
    assert saved['route'] == {'host': 'tools.example.com', 'pathPrefix': prefix, 'stripPrefix': False,
                              'upstreams': [origin], 'probePath': (prefix.rstrip('/') + '/health')}
    registration.unregister()
    registration.unregister()  # No second deletion.
    assert [(m, n) for m, n, _ in calls] == [('PUT', 'qc-folder-manager'), ('GET', 'retained-name'), ('DELETE', 'retained-name')]


def test_changed_route_is_not_deleted(configuration, monkeypatch):
    registration = RouteRegistration(enabled(configuration))
    registration.name, registration.route = 'ours', {'upstreams': ['http://old:123']}
    calls = []

    def request(method, name, payload=None):
        calls.append(method)
        return {'name': 'ours', 'route': {'upstreams': ['http://new:456']}}

    monkeypatch.setattr(registration, 'request', request)
    with pytest.raises(RouteAPIError, match='changed'):
        registration.unregister()
    assert calls == ['GET']


@pytest.mark.parametrize('response', [None, {}, {'name': '../bad', 'status': 'written'}, {'name': 'valid', 'status': 'written', 'route': {}}])
def test_invalid_registration_does_not_authorize_delete(configuration, monkeypatch, response):
    registration = RouteRegistration(enabled(configuration))
    calls = []
    monkeypatch.setattr(registration, 'request', lambda *args: calls.append(args) or response)
    with pytest.raises(RouteAPIError, match='invalid registration'):
        registration.register(12345)
    registration.unregister()
    assert len(calls) == 1


def test_socket_reserves_assigned_port():
    with bind_listener('127.0.0.1', 0) as listener:
        listener.listen()
        port = listener.getsockname()[1]
        assert port > 0
        with pytest.raises(OSError):
            bind_listener('127.0.0.1', port)
    with bind_listener('127.0.0.1', port):
        pass


@pytest.mark.parametrize('scenario', ['stop-during-registration', 'run-failure', 'register-failure', 'cleanup-failure'])
def test_lifecycle_releases_resources(configuration, monkeypatch, scenario):
    import waitress
    import qcmanager.web

    events = []
    config = enabled(configuration)
    original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    manager = SimpleNamespace(start_scanning=lambda settings: events.append('scan'), shutdown=lambda: events.append('shutdown'))
    app = SimpleNamespace(extensions={'manager': manager, 'project_settings': object()})
    monkeypatch.setattr(qcmanager.web, 'create_app', lambda *args, **kwargs: app)

    def create_server(*args, **kwargs):
        kwargs['sockets'][0].listen()

        def run():
            events.append('run')
            if scenario == 'run-failure':
                raise RuntimeError('test run failure')

        return SimpleNamespace(run=run, close=lambda: events.append('close'))

    monkeypatch.setattr(waitress, 'create_server', create_server)

    def request(self, method, name, payload=None):
        events.append(method)
        if method == 'PUT':
            if scenario == 'register-failure':
                raise RouteAPIError('PUT failed')
            if scenario == 'stop-during-registration':
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return {'name': name, 'route': payload, 'status': 'written'}
        if method == 'GET':
            return {'name': name, 'route': self.route}
        if scenario == 'cleanup-failure':
            raise RouteAPIError('DELETE failed')

    monkeypatch.setattr(RouteRegistration, 'request', request)
    if scenario == 'run-failure':
        with pytest.raises(RuntimeError, match='test run failure'):
            run_service(config, None, logging.getLogger('test'))
    else:
        assert run_service(config, None, logging.getLogger('test')) == (1 if scenario.endswith('failure') else 0)
    assert events[-2:] == ['close', 'shutdown']
    assert ('DELETE' in events) == (scenario != 'register-failure')
    if scenario in ('stop-during-registration', 'register-failure'):
        assert 'scan' not in events and 'run' not in events
    assert all(signal.getsignal(sig) == previous for sig, previous in original_handlers.items())


@pytest.mark.parametrize('raw', [b'null', b'[]', b'not-json'])
def test_malformed_get_is_not_mistaken_for_missing_route(configuration, monkeypatch, raw):
    import qcmanager.route_registration as module

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return raw

    monkeypatch.setattr(module.OpenerDirector, 'open', lambda *args, **kwargs: Response())
    registration = RouteRegistration(enabled(configuration))
    registration.name, registration.route = 'ours', {}
    with pytest.raises(RouteAPIError):
        registration.unregister()
    assert registration.name == 'ours'
