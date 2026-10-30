import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote, urlsplit
from urllib.request import (Request, HTTPRedirectHandler, OpenerDirector, ProxyHandler,
                            HTTPHandler, HTTPSHandler, UnknownHandler,
                            HTTPDefaultErrorHandler, HTTPErrorProcessor)

TERMINAL = frozenset(('success', 'failure', 'error', 'killed', 'skipped', 'declined'))


class APIError(RuntimeError):
    def __init__(self, message='Upstream service unavailable', status=None):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_json(url, token=None, form=None):
    headers = {'Accept': 'application/json', 'User-Agent': 'QC-Folder-Manager/0.1'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = None
    if form is not None:
        data = urlencode(form).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    try:
        # Only initialize the transport actually used. HTTP endpoints do not
        # require an unrelated TLS context; HTTPS retains normal verification.
        opener = OpenerDirector()
        scheme = urlsplit(url).scheme
        if scheme not in ('http','https'):
            raise APIError('Unsupported upstream URL scheme')
        for handler in (ProxyHandler(), UnknownHandler(), HTTPHandler(),
                        HTTPDefaultErrorHandler(), NoRedirect(), HTTPErrorProcessor()):
            opener.add_handler(handler)
        if scheme == 'https':
            opener.add_handler(HTTPSHandler())
        with opener.open(Request(url, data=data, headers=headers), timeout=20) as res:
            raw = res.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise APIError('Upstream response too large')
            return json.loads(raw)
    except HTTPError as e:
        raise APIError('Upstream request denied (HTTP %d)' % e.code, e.code) from None
    except (URLError, TimeoutError, OSError, ValueError) as e:
        reason = getattr(e, 'reason', e)
        raise APIError('Upstream request failed (%s, errno=%s)' %
                       (type(reason).__name__, getattr(reason, 'errno', None))) from None


class Gitea:
    def __init__(self, config):
        self.config = config['gitea']
        self.base = self.config['base_url']
        self.callback = config['server']['public_url'] + '/auth/callback'

    def authorize_url(self, state, challenge):
        return self.base + '/login/oauth/authorize?' + urlencode({
            'client_id': os.environ[self.config['client_id_env']],
            'redirect_uri': self.callback, 'response_type': 'code',
            'scope': self.config['scopes'], 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
        })

    def exchange(self, code, verifier):
        result = http_json(self.base + '/login/oauth/access_token', form={
            'client_id': os.environ[self.config['client_id_env']],
            'client_secret': os.environ[self.config['client_secret_env']],
            'grant_type': 'authorization_code', 'redirect_uri': self.callback,
            'code': code, 'code_verifier': verifier,
        })
        if not isinstance(result, dict) or not isinstance(result.get('access_token'), str) or not result['access_token']:
            raise APIError('Invalid OAuth response')
        return result['access_token']

    def identity(self, token):
        person = http_json(self.base + '/api/v1/user', token)
        if (not isinstance(person, dict) or type(person.get('id')) is not int or person['id'] <= 0 or
                type(person.get('is_admin')) is not bool or not isinstance(person.get('login'), str) or not person['login']):
            raise APIError('Gitea profile is missing identity or administrator status')
        actor = {'id': person['id'], 'login': person['login'], 'is_admin': person['is_admin'], 'orgs': []}
        # Admin scope does not depend on organization enumeration.
        if actor['is_admin']:
            return actor
        seen_pages = set()
        for page in range(1, 1001):
            rows = http_json(self.base + '/api/v1/user/orgs?' + urlencode({'page': page, 'limit': 50}), token)
            if not isinstance(rows, list):
                raise APIError('Invalid organization response')
            if not rows:
                return actor
            names = []
            for row in rows:
                if not isinstance(row, dict):
                    raise APIError('Invalid organization response')
                name = row.get('name') or row.get('username')
                if not isinstance(name, str) or not name:
                    raise APIError('Invalid organization name')
                names.append(name)
            signature = tuple(names)
            if signature in seen_pages:
                raise APIError('Organization pagination did not advance')
            seen_pages.add(signature)
            actor['orgs'].extend(names)
        raise APIError('Organization pagination exceeded limit')


class Drone:
    def __init__(self, config):
        self.config = config['drone']

    def build(self, project, number):
        path = '/api/repos/%s/%s/builds/%d' % (
            quote(project['organization'], safe=''), quote(project['repository'], safe=''), number)
        result = http_json(self.config['base_url'] + path, os.environ.get(self.config['token_env']))
        if (not isinstance(result, dict) or type(result.get('number')) is not int or result['number'] != number or
                not isinstance(result.get('status'), str)):
            raise APIError('Drone response does not match build')
        return {k: result.get(k) for k in ('number', 'status', 'created', 'started', 'finished')}

    def link(self, project, number):
        return '%s/%s/%s/%d' % (self.config['base_url'], quote(project['organization'], safe=''),
                               quote(project['repository'], safe=''), number)


def permitted(actor, project):
    return actor['is_admin'] or project['organization'].casefold() in {x.casefold() for x in actor['orgs']}
