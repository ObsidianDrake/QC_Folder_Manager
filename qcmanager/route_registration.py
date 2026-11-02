"""Lifecycle integration with ObsidianDrake/Traefik-API."""
import json
import re
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (Request, OpenerDirector, ProxyHandler, UnknownHandler,
                            HTTPHandler, HTTPSHandler, HTTPDefaultErrorHandler,
                            HTTPErrorProcessor)

from .clients import NoRedirect


class RouteAPIError(RuntimeError):
    pass


class RouteRegistration:
    def __init__(self, config):
        self.settings = config.get('traefik_api', {'enable': False})
        self.server = config['server']
        self.name = None
        self.route = None

    def request(self, method, name, payload=None):
        settings = self.settings
        endpoint = settings['base_url'] + '/v1/routes/' + name
        opener = OpenerDirector()
        # Local management traffic must not follow redirects or environment proxies.
        for handler in (ProxyHandler({}), UnknownHandler(), HTTPHandler(),
                        HTTPDefaultErrorHandler(), NoRedirect(), HTTPErrorProcessor()):
            opener.add_handler(handler)
        if urlsplit(endpoint).scheme == 'https':
            opener.add_handler(HTTPSHandler())
        headers = {'Authorization': 'Bearer ' + settings['token'], 'Accept': 'application/json'}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        try:
            with opener.open(Request(endpoint, data=body, headers=headers, method=method),
                             timeout=settings['timeout_seconds']) as response:
                expected = {'PUT': (200, 201), 'GET': (200,), 'DELETE': (204,)}[method]
                if response.status not in expected:
                    raise RouteAPIError('Traefik-API %s returned unexpected HTTP %d' % (method, response.status))
                if method == 'DELETE':
                    return None
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise RouteAPIError('Traefik-API response too large')
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise RouteAPIError('Traefik-API returned an invalid JSON object')
                return result
        except HTTPError as error:
            status = error.code
            error.close()
            if method in ('GET', 'DELETE') and status == 404:
                return None
            # Never log response bodies, credentials, or request URLs.
            raise RouteAPIError('Traefik-API %s failed (HTTP %d)' % (method, status)) from None
        except (URLError, OSError, ValueError, HTTPException) as error:
            raise RouteAPIError('Traefik-API %s failed (%s)' % (method, type(error).__name__)) from None

    def register(self, port):
        if not self.settings['enable']:
            return
        host = self.settings['upstream_host']
        authority = '[%s]' % host if ':' in host else host
        prefix = self.server['base_path']
        route = {
            'host': urlsplit(self.server['public_url']).hostname.lower().rstrip('.'),
            'pathPrefix': prefix or '/', 'stripPrefix': False,
            'upstreams': ['http://%s:%d' % (authority, port)],
            'probePath': prefix + '/health',
        }
        result = self.request('PUT', self.settings['route_name'], route)
        if (not isinstance(result, dict) or result.get('status') != 'written'
                or not isinstance(result.get('name'), str)
                or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', result['name'])
                or result.get('route') != route):
            raise RouteAPIError('Traefik-API returned an invalid registration response')
        # A takeover can retain the previous route's name. Use the returned name.
        self.name, self.route = result['name'], route

    def unregister(self):
        if self.name is None:
            return
        current = self.request('GET', self.name)
        if current is not None:
            if not isinstance(current, dict) or current.get('name') != self.name or current.get('route') != self.route:
                raise RouteAPIError('Traefik-API route changed; automatic deletion skipped')
            self.request('DELETE', self.name)
        self.name, self.route = None, None
