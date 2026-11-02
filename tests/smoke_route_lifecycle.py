"""Run the real CLI/binary against an isolated loopback Traefik-API stub."""
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler

import yaml


TOKEN = 'isolated-smoke-route-token-24'


def eventually(check, process=None):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError):
            pass
        if process is not None and process.poll() is not None:
            raise AssertionError('Service exited before ready: %s' % process.stderr.read().decode())
        time.sleep(.05)
    raise AssertionError('Timed out waiting for lifecycle event')


def main(binary):
    command = [sys.executable, 'main.py'] if binary == '--source' else [str(Path(binary).resolve())]
    with tempfile.TemporaryDirectory(prefix='qcfm-route-smoke-') as directory:
        root = Path(directory)
        state = {}
        calls = []

        class API(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, body=None):
                raw = json.dumps(body).encode() if body is not None else b''
                self.send_response(status)
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(raw)

            def auth(self):
                assert self.headers['Authorization'] == 'Bearer ' + TOKEN
                calls.append((self.command, self.path))

            def do_PUT(self):
                self.auth()
                assert self.path == '/control/v1/routes/qc-folder-manager'
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                origin = urlsplit(payload['upstreams'][0])
                assert origin.port > 0
                # The advertised listener is already reserved when PUT arrives.
                with socket.create_connection((origin.hostname, origin.port), timeout=1):
                    pass
                if state.get('reject'):
                    return self.respond(state['reject'], {'detail': 'private-response-must-not-be-logged'})
                state['route'] = payload
                self.respond(201, {'name': 'canonical-route', 'requestedName': 'qc-folder-manager',
                                   'status': 'written', 'route': payload})

            def do_GET(self):
                if self.path == '/api/user':
                    return self.respond(200, {'login': 'smoke-drone'})
                self.auth()
                assert self.path == '/control/v1/routes/canonical-route'
                route = dict(state['route'])
                if state.get('changed'):
                    route['upstreams'] = ['http://replacement.example.com:9000']
                self.respond(200, {'name': 'canonical-route', 'entryPoint': 'web', 'route': route})

            def do_DELETE(self):
                self.auth()
                assert self.path == '/control/v1/routes/canonical-route'
                state['deleted'] = True
                self.respond(state.get('delete_status', 204))

        api = ThreadingHTTPServer(('127.0.0.1', 0), API)
        threading.Thread(target=api.serve_forever, daemon=True).start()
        origin = 'http://127.0.0.1:%d' % api.server_port
        opener = build_opener(ProxyHandler({}))
        try:
            for case in ('auto-term', 'fixed-int', 'disabled-auto', 'reject-409', 'reject-401',
                         'changed-route', 'delete-404', 'delete-503', 'occupied-port'):
                state.clear(); calls.clear()
                config = {
                    'schema_version': 1,
                    'server': {'listen': '127.0.0.1', 'public_url': 'http://tools.example.com/qc_folder_mgt',
                               'data_dir': str(root/case)},
                    'traefik_api': {'enable': case != 'disabled-auto', 'base_url': origin+'/control', 'token': TOKEN},
                    'gitea': {'base_url': origin, 'client_id': 'test', 'client_secret': 'test-secret'},
                    'drone': {'base_url': origin, 'token': 'test-drone'}, 'hosts': {}, 'projects': [],
                }
                held = None
                if case in ('fixed-int', 'occupied-port'):
                    held = socket.socket()
                    held.bind(('127.0.0.1', 0)); held.listen()
                    config['server']['port'] = held.getsockname()[1]
                    if case == 'fixed-int':
                        held.close(); held = None
                if case.startswith('reject-'):
                    state['reject'] = int(case.split('-')[1])
                state['changed'] = case == 'changed-route'
                if case.startswith('delete-'):
                    state['delete_status'] = int(case.split('-')[1])
                path = root/(case+'.yml')
                path.write_text(yaml.safe_dump(config))
                if case == 'auto-term':
                    for flag in ('--check-config', '--preflight'):
                        checked = subprocess.run(command+['--config', str(path), flag], capture_output=True, timeout=20)
                        assert checked.returncode == 0, checked.stderr
                        assert not calls, 'Validation/preflight mutated routes'
                process = subprocess.Popen(command+['--config', str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                log = root/case/'logs'/'qc-folder-manager.log'
                try:
                    if case.startswith('reject-') or case == 'occupied-port':
                        assert process.wait(timeout=15) == 1
                        assert all(method != 'DELETE' for method, _ in calls)
                        if case == 'occupied-port':
                            assert not calls
                        else:
                            assert len(calls) == 1
                    else:
                        if case == 'disabled-auto':
                            match = eventually(lambda: re.search(r'service listening host=127.0.0.1 port=(\d+)', log.read_text()) if log.exists() else None, process)
                            port = int(match.group(1))
                        else:
                            route = eventually(lambda: state.get('route'), process)
                            port = urlsplit(route['upstreams'][0]).port
                            assert route == {'host': 'tools.example.com', 'pathPrefix': '/qc_folder_mgt',
                                             'stripPrefix': False, 'upstreams': ['http://127.0.0.1:%d' % port],
                                             'probePath': '/qc_folder_mgt/health'}
                            if case == 'fixed-int':
                                assert port == config['server']['port']

                        def healthy():
                            with opener.open('http://127.0.0.1:%d/qc_folder_mgt/health' % port, timeout=1) as response:
                                return json.load(response)['status'] == 'ok'

                        eventually(healthy, process)
                        process.send_signal(signal.SIGINT if case == 'fixed-int' else signal.SIGTERM)
                        assert process.wait(timeout=15) == (1 if case in ('changed-route', 'delete-503') else 0)
                        if case == 'disabled-auto':
                            assert not calls
                        elif case == 'changed-route':
                            assert [m for m, _ in calls] == ['PUT', 'GET']
                        else:
                            assert [m for m, _ in calls] == ['PUT', 'GET', 'DELETE']
                        with socket.socket() as probe:
                            assert probe.connect_ex(('127.0.0.1', port)) != 0
                    diagnostic = log.read_text() if log.exists() else ''
                    assert TOKEN not in diagnostic and 'private-response-must-not-be-logged' not in diagnostic
                    print('PASS route lifecycle: '+case)
                except BaseException:
                    if log.exists():
                        print(log.read_text())
                    raise
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            process.kill(); process.wait()
                    process.stdout.close(); process.stderr.close()
                    if held is not None:
                        held.close()
        finally:
            api.shutdown(); api.server_close()


if __name__ == '__main__':
    main(sys.argv[1])
