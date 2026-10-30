import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, make_response, redirect, request, g, send_from_directory
from werkzeug.exceptions import HTTPException

from .clients import APIError, Gitea, Drone
from .database import Database
from .service import Manager, RequestError
from .storage import Storage


class Sessions:
    """Opaque browser cookies; OAuth tokens and PKCE verifiers remain in memory."""
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def create(self, data, lifetime):
        with self.lock:
            self.items = {k:v for k,v in self.items.items() if v['expires'] > time.time()}
            if len(self.items) >= 2000:
                raise RequestError('登入工作階段已達上限。',503)
            key = secrets.token_urlsafe(32)
            self.items[key] = dict(data,expires=time.time()+lifetime)
        return key

    def get(self, key):
        with self.lock:
            value = self.items.get(key)
            if not value or value['expires'] <= time.time():
                self.items.pop(key,None)
                return None
            return dict(value)

    def remove(self, key):
        with self.lock:
            return self.items.pop(key,None)


class Mount:
    """Accept direct prefixed requests and reverse-proxy stripped requests."""
    def __init__(self, app, prefix):
        self.app, self.prefix = app,prefix

    def __call__(self, env, start):
        path = env.get('PATH_INFO','')
        if self.prefix and (path == self.prefix or path.startswith(self.prefix+'/')):
            env['PATH_INFO'] = path[len(self.prefix):] or '/'
        env['SCRIPT_NAME'] = self.prefix
        return self.app(env,start)


def create_app(config, db=None, gitea=None, drone=None, storage=None):
    static = Path(__file__).parent/'static'
    app = Flask(__name__,static_folder=None)
    app.config.update(MAX_CONTENT_LENGTH=65536, JSON_SORT_KEYS=False)
    prefix = config['server']['base_path']
    cookie_path = prefix+'/'
    cookie_secure = config['server']['public_url'].startswith('https://')
    cookie_name = 'qcfm_'+hashlib.sha256(config['server']['public_url'].encode()).hexdigest()[:12]
    sessions = Sessions()
    db = db or Database(Path(config['server']['data_dir'])/'qc-folder-manager.sqlite3')
    manager = Manager(config,db,gitea or Gitea(config),drone or Drone(config),storage or Storage(config))
    app.extensions.update(manager=manager,sessions=sessions,cookie_name=cookie_name)

    def set_cookie(response, key, lifetime):
        response.set_cookie(cookie_name,key,max_age=lifetime,httponly=True,samesite='Lax',
                            secure=cookie_secure,path=cookie_path)
        return response

    def require_login(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            login = sessions.get(request.cookies.get(cookie_name))
            if not login or 'token' not in login:
                raise RequestError('請先使用 Gitea 登入。',401)
            if request.method != 'GET':
                csrf = request.headers.get('X-CSRF-Token','')
                if not hmac.compare_digest(csrf,login['csrf']):
                    raise RequestError('表單驗證失敗，請重新整理。',403)
            try:
                actor = manager.refresh_actor(login['token'],login['actor'])
            except APIError as error:
                if error.status == 401:
                    sessions.remove(request.cookies.get(cookie_name))
                    raise RequestError('Gitea 登入已過期，請重新登入。',401)
                raise
            g.actor, g.login = actor,login
            return fn(*args,**kwargs)
        return wrapped

    @app.after_request
    def security(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.errorhandler(RequestError)
    def handle_request(error):
        return jsonify(error=str(error)),error.status

    @app.errorhandler(APIError)
    def handle_upstream(error):
        logging.getLogger('qcmanager').warning('upstream validation failed: %s',error)
        return jsonify(error='Gitea 身分驗證暫時無法完成；權限不明時停止操作，必要時請重新登入。'),503

    @app.errorhandler(Exception)
    def handle_error(error):
        if isinstance(error,HTTPException):
            return jsonify(error=error.name),error.code
        # Do not log raw URLs, exceptions, OAuth codes, tokens or request bodies.
        logging.getLogger('qcmanager').error('request failed endpoint=%s',request.endpoint or 'unknown')
        return jsonify(error='服務暫時無法完成操作，請查看執行 log。'),500

    @app.get('/health')
    def health():
        return jsonify(status='ok',version='0.1.0')

    @app.get('/')
    def index():
        content = (static/'index.html').read_text().replace('__BASE__',prefix)
        return make_response(content)

    @app.get('/assets/<name>')
    def asset(name):
        if name not in ('app.js','style.css','i18n.js'):
            raise RequestError('Not found',404)
        return send_from_directory(str(static),name)

    @app.get('/auth/login')
    def login():
        state, verifier = secrets.token_urlsafe(32),secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        sessions.remove(request.cookies.get(cookie_name))
        key = sessions.create({'state':state,'verifier':verifier},600)
        return set_cookie(redirect(manager.gitea.authorize_url(state,challenge)),key,600)

    @app.get('/auth/callback')
    def callback():
        key = request.cookies.get(cookie_name)
        saved = sessions.get(key)
        state = request.args.get('state','')
        if not saved or 'state' not in saved or not hmac.compare_digest(state,saved['state']):
            raise RequestError('登入驗證已過期或不正確，請重新登入。',400)
        sessions.remove(key)  # Single-use OAuth state even if exchange fails.
        code = request.args.get('code')
        if not code or len(code)>4096 or request.args.get('error'):
            raise RequestError('Gitea 登入未完成。',400)
        token = manager.gitea.exchange(code,saved['verifier'])
        actor = manager.gitea.identity(token)
        lifetime = config['server']['session_hours']*3600
        session_key = sessions.create({'token':token,'actor':actor,'csrf':secrets.token_urlsafe(32)},lifetime)
        return set_cookie(redirect(config['server']['public_url']+'/'),session_key,lifetime)

    @app.post('/auth/logout')
    def logout():
        saved = sessions.get(request.cookies.get(cookie_name))
        if not saved or not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),saved.get('csrf','invalid')):
            raise RequestError('表單驗證失敗。',403)
        sessions.remove(request.cookies.get(cookie_name))
        response = jsonify(ok=True)
        response.delete_cookie(cookie_name,path=cookie_path)
        return response

    @app.get('/api/me')
    @require_login
    def me():
        return jsonify(user=g.actor,csrf=g.login['csrf'])

    @app.get('/api/records')
    @require_login
    def records():
        return jsonify(manager.inventory(g.actor))

    @app.post('/api/scan')
    @require_login
    def scan():
        body = request.get_json(silent=True) or {}
        projects = body.get('projects')
        if not isinstance(projects,list) or not 1<=len(projects)<=100 or any(not isinstance(x,str) for x in projects):
            raise RequestError('請選擇要掃描的專案。')
        if any(x not in manager.allowed(g.actor) for x in projects):
            raise RequestError('包含無權限的專案。',404)
        for pid in set(projects):
            manager.scan(pid,g.actor)
        return jsonify(accepted=True),202

    @app.get('/api/disks')
    @require_login
    def disks():
        return jsonify(disks=manager.disks.snapshot(manager.allowed(g.actor)))

    @app.post('/api/disks/refresh')
    @require_login
    def refresh_disks():
        body = request.get_json(silent=True) or {}
        ids = body.get('projects')
        if not isinstance(ids,list) or not 1<=len(ids)<=100 or any(not isinstance(x,str) for x in ids):
            raise RequestError('請選擇要掃描的專案。')
        if any(pid not in manager.allowed(g.actor) for pid in ids):
            raise RequestError('包含無權限的專案。',404)
        return jsonify(accepted=manager.disks.refresh(set(ids),manual=True)),202

    @app.post('/api/sizes')
    @require_login
    def sizes():
        body = request.get_json(silent=True) or {}
        return jsonify(manager.sizes(body.get('records'),g.actor)),202

    @app.post('/api/delete')
    @require_login
    def delete():
        body = request.get_json(silent=True) or {}
        if body.get('confirm') is not True:
            raise RequestError('請確認要刪除的執行資料。')
        return jsonify(manager.delete(body.get('records'),g.actor,g.login['token'])),202

    @app.get('/api/operations')
    @require_login
    def operations():
        return jsonify(operations=db.operations(manager.allowed(g.actor)))

    @app.get('/api/audit')
    @require_login
    def audit():
        before = request.args.get('before',type=int)
        if before is not None and not 1 <= before < 2**63:
            raise RequestError('Invalid audit cursor')
        rows = db.audit(manager.allowed(g.actor),before)
        return jsonify(events=rows,next_before=rows[-1]['seq'] if len(rows)==100 else None)

    app.wsgi_app = Mount(app.wsgi_app,prefix)
    return app
