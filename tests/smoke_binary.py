"""Run the packaged binary against local mock providers and an SSH protocol shim.

Usage: python tests/smoke_binary.py dist/qc-folder-manager
Never connects to company hosts or deletes production data.
"""
import base64
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit, urlencode
from urllib.request import build_opener, HTTPCookieProcessor, Request

import yaml


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


def main(binary):
    with tempfile.TemporaryDirectory(prefix='qcfm-binary-smoke-') as directory:
        root=Path(directory)
        app_port=free_port()
        public='http://127.0.0.1:%d/qc_folder_mgt'%app_port
        trace=[]
        class Provider(BaseHTTPRequestHandler):
            challenge=None
            def log_message(self,*args):pass
            def send_json(self,body,status=200):
                trace.append((self.command,urlsplit(self.path).path,status))
                raw=json.dumps(body).encode()
                self.send_response(status);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_GET(self):
                parsed=urlsplit(self.path)
                if parsed.path=='/gitea/login/oauth/authorize':
                    query=parse_qs(parsed.query)
                    Provider.challenge=query['code_challenge'][0]
                    self.send_response(302)
                    self.send_header('Location',public+'/auth/callback?'+urlencode({'code':'smoke-code','state':query['state'][0]}))
                    self.end_headers();return
                if parsed.path.startswith('/gitea/api/'):
                    if self.headers.get('Authorization')!='Bearer mock-user-token':
                        return self.send_json({},401)
                    if parsed.path.endswith('/user'):
                        return self.send_json({'id':42,'login':'vendor-smoke','is_admin':False})
                    if parsed.path.endswith('/user/orgs'):
                        return self.send_json([{'name':'ExampleVendor'}] if parse_qs(parsed.query).get('page')==['1'] else [])
                if parsed.path.startswith('/drone/api/'):
                    if self.headers.get('Authorization')!='Bearer mock-drone-token':
                        return self.send_json({},401)
                    if parsed.path.endswith('/user'):
                        return self.send_json({'login':'qc-service'})
                    number=int(parsed.path.rsplit('/',1)[1])
                    return self.send_json({'number':number,'status':'running' if number==1235 else 'success','created':100})
                self.send_json({},404)
            def do_POST(self):
                form=parse_qs(self.rfile.read(int(self.headers.get('Content-Length',0))).decode())
                verifier=form.get('code_verifier',[''])[0]
                challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
                if self.path!='/gitea/login/oauth/access_token' or challenge!=Provider.challenge:
                    return self.send_json({},400)
                self.send_json({'access_token':'mock-user-token'})
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Provider)
        threading.Thread(target=upstream.serve_forever,daemon=True).start()
        origin='http://127.0.0.1:%d'%upstream.server_port
        runs=root/'runs';runs.mkdir()
        for number in (1234,1235):
            case=runs/('qc_%d'%number);case.mkdir();(case/'output').write_text('QC smoke result')
        shim=root/'bin';shim.mkdir()
        script=shim/'ssh'
        script.write_text('#!'+sys.executable+'\nimport os,shlex,sys\ncommand=shlex.split(sys.argv[-1])\nos.execvp(command[0],command)\n')
        script.chmod(0o700)
        cfg={'schema_version':1,'server':{'listen':'127.0.0.1','port':app_port,'public_url':public,'data_dir':str(root/'state')},
             'gitea':{'base_url':origin+'/gitea'},'drone':{'base_url':origin+'/drone'},
             'hosts':{'test-ssh':{'transport':'ssh','environment':'zone-b','hostname':'smoke-only',
                                'ssh':{'python':sys.executable,'user_mode':'current_user','timeout_seconds':30}}},
             'projects':[{'id':'test','organization':'ExampleVendor','repository':'Example_QC','host':'test-ssh',
                          'run_folder':str(runs),'prefix':'qc'}]}
        config=root/'config.yml';config.write_text(yaml.safe_dump(cfg))
        env=dict(os.environ,QC_FOLDER_GITEA_CLIENT_ID='mock-client',QC_FOLDER_GITEA_CLIENT_SECRET='mock-client-secret',
                 QC_FOLDER_DRONE_TOKEN='mock-drone-token',PATH=str(shim)+os.pathsep+os.environ['PATH'])
        command=([sys.executable,'main.py'] if binary=='--source' else [str(Path(binary).resolve())])
        proc=subprocess.Popen(command+['--config',str(config)],env=env,
                              stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        browser=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        csrf=''
        def api(path,body=None):
            data=None if body is None else json.dumps(body).encode()
            with browser.open(Request(public+path,data=data,headers={'Content-Type':'application/json','X-CSRF-Token':csrf}),timeout=10) as res:
                return json.load(res)
        def eventually(fn):
            until=time.monotonic()+20
            while time.monotonic()<until:
                try:
                    result=fn()
                    if result:return result
                except OSError:pass
                time.sleep(.05)
            raise AssertionError('Timed out waiting for packaged service')
        try:
            eventually(lambda:api('/health')['status']=='ok')
            with browser.open(public+'/auth/login',timeout=10) as res:
                assert b'QC Folder Manager' in res.read()
            me=api('/api/me');csrf=me['csrf'];assert me['user']['login']=='vendor-smoke'
            for name in ('app.js','style.css'):
                with browser.open(public+'/assets/'+name,timeout=10) as res:assert len(res.read())>100
            api('/api/scan',{'projects':['test']})
            inventory=eventually(lambda:(lambda x:x if x['scans'].get('test',{}).get('state')=='ready' else None)(api('/api/records')))
            assert len(inventory['records'])==2
            ids={r['number']:r['id'] for r in inventory['records']}
            assert not next(r for r in inventory['records'] if r['number']==1235)['deletable']
            # Submit both via API: backend must protect the running record independently of UI.
            api('/api/delete',{'records':[ids[1234],ids[1235]],'confirm':True})
            ops=eventually(lambda:(lambda x:x if len(x)==2 and all(o['state'] not in ('queued','running') for o in x) else None)(api('/api/operations')['operations']))
            assert {o['state'] for o in ops}=={'deleted','rejected'}
            assert not (runs/'qc_1234').exists() and (runs/'qc_1235'/'output').exists()
            assert all(o['actor']['login']=='vendor-smoke' for o in ops)
            audit=api('/api/audit')['events'];assert len(audit)>=5
            assert 'mock-user-token' not in json.dumps(audit)
            log=(root/'state'/'logs'/'qc-folder-manager.log').read_text()
            assert 'mock-user-token' not in log and 'mock-client-secret' not in log
            print('PASS: packaged OAuth+PKCE, subpaths, static UI, SSH helper, scan, protected delete, audit and logs')
        except Exception:
            print('Mock provider response trace:',trace)
            if (root/'state'/'logs'/'qc-folder-manager.log').exists():
                print((root/'state'/'logs'/'qc-folder-manager.log').read_text())
            raise
        finally:
            proc.terminate()
            try:proc.wait(timeout=20)
            except subprocess.TimeoutExpired:proc.kill();proc.wait();raise
            diagnostic=proc.stderr.read().decode()
            if diagnostic:print(diagnostic)
            upstream.shutdown();upstream.server_close()
            if proc.returncode not in (0,-15):
                raise AssertionError('Packaged service did not shut down cleanly')


if __name__=='__main__':main(sys.argv[1])
