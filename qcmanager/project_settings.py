"""Administrator project edits, durable configuration, and immutable audit ownership."""
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time

import yaml

from .config import ConfigError, UniqueLoader, validate_config
from .clients import permitted
from .service import RequestError

FIELDS = ('id', 'organization', 'repository', 'host', 'run_folder', 'prefix')


def identity(project):
    return project['organization'], project['repository']


def atomic_write(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.qcfm-config-', delete=False) as stream:
            temporary = stream.name
            os.chmod(temporary, 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


class ProjectSettings:
    def __init__(self, config, db, manager):
        self.config, self.db, self.manager = config, db, manager
        self.lock = threading.RLock()
        self.path = Path(config['_source_path']) if config.get('_source_path') else None
        self.digest = config.get('_source_digest')
        self.broken = False
        with db.connect() as connection:
            for project in config['projects']:
                old = connection.execute('SELECT definition FROM project_catalog WHERE id=?', (project['id'],)).fetchone()
                if old and identity(json.loads(old[0])) != identity(project):
                    raise ConfigError('Project ID ownership changed; use a new project ID')
                if not old or json.loads(old[0]) != project:
                    connection.execute('DELETE FROM records WHERE project=?', (project['id'],))
                    connection.execute('DELETE FROM scans WHERE project=?', (project['id'],))
                connection.execute('INSERT OR REPLACE INTO project_catalog VALUES(?,?)', (project['id'], json.dumps(project)))

    def history_allowed(self, actor):
        with self.db.connect() as db:
            projects = [json.loads(row[0]) for row in db.execute('SELECT definition FROM project_catalog')]
        return {p['id']: p for p in projects if permitted(actor, p)}

    def snapshot(self):
        return dict(revision=self.digest, writable=bool(self.path and not self.broken),
                    projects=[{k: p[k] for k in FIELDS} for p in self.manager.projects.values()],
                    hosts=[dict(id=name, transport=host['transport'], hostname=host.get('hostname'))
                           for name,host in self.config['hosts'].items()])

    def save(self, body, actor):
        if not self.path or self.broken:
            raise RequestError('專案設定目前無法寫入。', 409)
        if not isinstance(body, dict) or set(body) - {'revision','action','project','id'}:
            raise RequestError('專案設定請求格式不正確。')
        if body.get('revision') != self.digest:
            raise RequestError('設定已被其他管理員修改，請重新載入專案清單。', 409)
        action = body.get('action')
        if action not in ('create','update','delete'):
            raise RequestError('不支援此專案操作。')
        manager = self.manager
        previous = None
        projects = copy.deepcopy(self.config['projects'])
        if action == 'delete':
            pid = body.get('id')
            if not isinstance(pid,str):
                raise RequestError('專案 ID 不正確。')
            previous = manager.projects.get(pid)
            if previous is None:
                raise RequestError('找不到專案。', 404)
            projects = [p for p in projects if p['id'] != pid]
            current = None
        else:
            current = body.get('project')
            if not isinstance(current,dict) or set(current) != set(FIELDS) or any(not isinstance(v,str) for v in current.values()):
                raise RequestError('請填寫所有專案欄位。')
            pid = current['id']
            previous = manager.projects.get(pid)
            if action == 'create' and previous is not None:
                raise RequestError('專案 ID 已存在。',409)
            if action == 'update' and previous is None:
                raise RequestError('找不到專案。',404)
            projects = [current if p['id']==pid else p for p in projects]
            if action == 'create':
                projects.append(current)
        with manager.guard, manager.disks.lock:
            sizes_busy = pid in manager.pending_size_projects or any((manager.db.record(rid) or {}).get('project') == pid for rid in manager.pending_sizes)
            lock = manager.locks.get(pid)
            if pid in manager.pending_scans or sizes_busy or pid in manager.disks.pending or (lock and lock.locked()):
                raise RequestError('此專案的背景工作仍在執行，請等待完成後再儲存。',409)
        with self.db.connect() as db:
            old = db.execute('SELECT definition FROM project_catalog WHERE id=?',(pid,)).fetchone()
            if current and old and identity(json.loads(old[0])) != identity(current):
                raise RequestError('專案歸屬不可變更，請使用新的專案 ID 新增專案。',409)
            if db.execute("SELECT 1 FROM operations WHERE project=? AND state IN ('queued','running','unknown','partial')",(pid,)).fetchone():
                raise RequestError('此專案仍有待核對的清理工作，請先由管理員確認結果。',409)
        try:
            original = self.path.read_text(encoding='utf-8-sig')
            if hashlib.sha256(original.encode('utf-8')).hexdigest() != self.digest:
                raise RequestError('設定檔已在外部修改，請重啟服務後再編輯。',409)
            document = yaml.load(original, Loader=UniqueLoader)
            document['projects'] = projects
            validated = validate_config(copy.deepcopy(document), self.path)
            rendered = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        except RequestError:
            raise
        except ConfigError as error:
            # Validation messages contain controlled field names, never credentials.
            raise RequestError(str(error)) from error
        except Exception:
            raise RequestError('無法讀取或驗證設定檔，請查看伺服器設定。',409)
        written = False
        try:
            with self.db.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('DELETE FROM records WHERE project=?',(pid,))
                db.execute('DELETE FROM scans WHERE project=?',(pid,))
                if current:
                    db.execute('INSERT OR REPLACE INTO project_catalog VALUES(?,?)',(pid,json.dumps(current)))
                db.execute('INSERT INTO project_config_audit(at,actor,action,project,previous,current) VALUES(?,?,?,?,?,?)',
                           (time.time(),json.dumps({'id':actor['id'],'login':actor['login']}),action,pid,
                            json.dumps(previous),json.dumps(current)))
                # Detect external edits again immediately before replacing the file.
                if self.path.read_text(encoding='utf-8-sig') != original:
                    raise RequestError('設定檔已在外部修改，請重啟服務後再編輯。',409)
                atomic_write(self.path, rendered)
                written = True
        except Exception as error:
            if written:
                try:
                    atomic_write(self.path, original)
                except Exception:
                    self.broken = True
                    logging.getLogger('qcmanager').critical('project config rollback failed; restart required')
            if isinstance(error,RequestError):
                raise
            raise RequestError('專案設定儲存失敗，請檢查設定檔目錄權限與可用空間。',500) from error
        self.digest = hashlib.sha256(rendered.encode('utf-8')).hexdigest()
        self.config['_source_digest'] = self.digest
        self.config['projects'] = validated['projects']
        if current:
            manager.projects[pid] = next(p for p in validated['projects'] if p['id']==pid)
            manager.locks.setdefault(pid,threading.Lock())
        else:
            manager.projects.pop(pid,None)
            manager.locks.pop(pid,None)
        with manager.guard:
            manager.scan_finished.pop(pid,None)
        with self.db.connect() as db:
            db.execute('DELETE FROM size_scans WHERE project=?',(pid,))
        with manager.disks.lock:
            manager.disks.items.pop(pid,None)
            manager.disks.invalidated.pop(pid,None)
        logging.getLogger('qcmanager').info('project settings changed actor=%s action=%s project=%s',actor['login'],action,pid)
        return self.snapshot()
