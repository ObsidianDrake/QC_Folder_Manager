from urllib.parse import parse_qs, urlsplit

import pytest

from qcmanager.clients import Gitea
from qcmanager.web import create_app
from conftest import FakeDrone, FakeGitea


@pytest.mark.parametrize('strip_prefix', [False, True])
@pytest.mark.parametrize('scheme', ['http', 'https'])
def test_proxy_login_assets_api_and_logout(configuration, monkeypatch, strip_prefix, scheme):
    prefix = '/qc_folder_mgt'
    origin = scheme + '://tools.example.com:8080'
    configuration['server'].update(public_url=origin+prefix, base_path=prefix)
    fake = FakeGitea()
    gitea = Gitea(configuration)
    monkeypatch.setenv(configuration['gitea']['client_id_env'], 'test-client')
    monkeypatch.setattr(gitea, 'exchange', fake.exchange)
    monkeypatch.setattr(gitea, 'identity', fake.identity)
    app = create_app(configuration, gitea=gitea, drone=FakeDrone())
    app.config['TESTING'] = True
    if strip_prefix:
        backend = app.wsgi_app

        def proxy(env, start):
            # Browser still sees the external URL and scopes cookies to it.
            path = env['PATH_INFO']
            assert path == prefix or path.startswith(prefix+'/')
            env['PATH_INFO'] = path[len(prefix):] or '/'
            env['HTTP_X_FORWARDED_PREFIX'] = prefix
            return backend(env, start)

        app.wsgi_app = proxy
    client = app.test_client()

    def get(path, **kwargs):
        return client.get(prefix+path, base_url=origin, **kwargs)

    try:
        for path in ('', '/'):
            page = get(path)
            assert page.status_code == 200
            assert (prefix+'/assets/app.js').encode() in page.data
            assert (prefix+'/assets/style.css').encode() in page.data
        for path in ('/health', '/assets/app.js', '/assets/style.css'):
            assert get(path).status_code == 200
        assert get('/api/records').status_code == 401
        login = get('/auth/login', headers={
            'X-Forwarded-Host': 'untrusted.example.com',
            'X-Forwarded-Proto': 'http',
            'X-Forwarded-Prefix': '/untrusted',
        })
        query = parse_qs(urlsplit(login.location).query)
        assert query['redirect_uri'] == [origin+prefix+'/auth/callback']
        assert query['code_challenge_method'] == ['S256']
        cookie = login.headers['Set-Cookie']
        assert 'Path='+prefix+'/' in cookie
        assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie
        assert ('; Secure' in cookie) == (scheme == 'https')
        callback = get('/auth/callback', query_string={'code': 'valid', 'state': query['state'][0]})
        assert callback.status_code == 302
        assert callback.location == origin+prefix+'/'
        me = get('/api/me')
        assert me.json['user']['login'] == 'vendor-one'
        assert get('/api/records').status_code == 200
        logout = client.post(prefix+'/auth/logout', base_url=origin,
                             headers={'X-CSRF-Token': me.json['csrf']})
        assert logout.status_code == 200
        assert 'Path='+prefix+'/' in logout.headers['Set-Cookie']
        assert get('/api/me').status_code == 401
    finally:
        app.extensions['manager'].shutdown()


def test_prefix_boundary_is_not_stripped(configuration):
    configuration['server'].update(public_url='http://tools.example.com/qc_folder_mgt',
                                   base_path='/qc_folder_mgt')
    app = create_app(configuration, gitea=FakeGitea(), drone=FakeDrone())
    try:
        assert app.test_client().get('/qc_folder_mgt-other/health').status_code == 404
    finally:
        app.extensions['manager'].shutdown()
