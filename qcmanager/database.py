"""Durable inventory, work items and append-only audit events. Tokens never enter SQLite."""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, name TEXT NOT NULL,
                    payload TEXT NOT NULL, UNIQUE(project,name));
                CREATE TABLE IF NOT EXISTS scans (
                    project TEXT PRIMARY KEY, updated REAL, state TEXT, error TEXT,
                    quarantines TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, batch TEXT NOT NULL, record TEXT NOT NULL,
                    project TEXT NOT NULL, actor TEXT NOT NULL, target TEXT NOT NULL,
                    state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    message TEXT, drone_status TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS active_record ON operations(record)
                    WHERE state IN ('queued','running','unknown','partial');
                CREATE TABLE IF NOT EXISTS audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, operation TEXT, project TEXT,
                    actor TEXT NOT NULL, event TEXT NOT NULL, at REAL NOT NULL, detail TEXT NOT NULL);
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=20000')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        with self.connect() as db:
            for item in db.execute('SELECT id,payload FROM records').fetchall():
                row = json.loads(item['payload'])
                if row.get('size_state') in ('queued','running'):
                    row.update(size_state='interrupted', size_ticket=None)
                    db.execute('UPDATE records SET payload=? WHERE id=?', (json.dumps(row), item['id']))
            rows = db.execute("SELECT * FROM operations WHERE state IN ('queued','running')").fetchall()
            for row in rows:
                state = 'failed' if row['state'] == 'queued' else 'unknown'
                message = 'Service restarted; request cancelled' if state == 'failed' else 'Service restarted; reconcile filesystem before any retry'
                db.execute('UPDATE operations SET state=?, message=?, updated=? WHERE id=?', (state,message,time.time(),row['id']))
                self.event(db, row['id'], row['project'], json.loads(row['actor']), state, {'message':message})
            db.execute("UPDATE scans SET state='error',error='Service restarted; scan again' WHERE state IN ('running','checking')")

    def event(self, db, operation, project, actor, event, detail):
        db.execute('INSERT INTO audit(operation,project,actor,event,at,detail) VALUES(?,?,?,?,?,?)',
                   (operation,project,json.dumps(actor),event,time.time(),json.dumps(detail)))

    def rejected(self, actor, project, detail):
        with self.connect() as db:
            self.event(db,None,project,actor,'rejected',detail)

    def start_scan(self, project):
        with self.connect() as db:
            db.execute("INSERT INTO scans(project,state) VALUES(?,'running') ON CONFLICT(project) DO UPDATE SET state='running',error=NULL", (project,))

    def finish_scan(self, project, result=None, error=None, state="ready"):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if error:
                db.execute("UPDATE scans SET state='error',error=? WHERE project=?", (error,project))
                return
            existing = {row['name']:json.loads(row['payload']) for row in db.execute('SELECT name,payload FROM records WHERE project=?',(project,))}
            db.execute('DELETE FROM records WHERE project=?',(project,))
            for row in result['records']:
                old = existing.get(row['name'], {})
                same = old.get('identity') == row.get('identity') and old.get('root_identity') == row.get('root_identity')
                rid = old.get('id', uuid.uuid4().hex) if same else uuid.uuid4().hex
                if same:
                    for key in ('bytes','size_at','size_state','size_error','size_ticket'):
                        if key in old:
                            row[key] = old[key]
                row.setdefault('size_state', 'pending' if row.get('bytes') is None else 'cached')
                row = dict(row, id=rid, project=project, scanned_at=time.time())
                db.execute('INSERT INTO records VALUES(?,?,?,?)',(rid,project,row['name'],json.dumps(row)))
            db.execute("UPDATE scans SET state=?,error=NULL,updated=?,quarantines=? WHERE project=?",
                       (state,time.time(),json.dumps(result.get('quarantines',[])),project))

    def scan_ready(self, project):
        with self.connect() as db:
            db.execute("UPDATE scans SET state='ready' WHERE project=?", (project,))

    def patch_record(self, snapshot, fields, ticket=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            found = db.execute('SELECT payload FROM records WHERE id=?', (snapshot['id'],)).fetchone()
            if not found:
                return False
            row = json.loads(found['payload'])
            if any(row.get(k) != snapshot.get(k) for k in ('identity','root_identity')):
                return False
            if ticket is not None and row.get('size_ticket') != ticket:
                return False
            row.update(fields)
            db.execute('UPDATE records SET payload=? WHERE id=?', (json.dumps(row), row['id']))
            return True

    def record(self, rid):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM records WHERE id=?',(rid,)).fetchone()
            return json.loads(row['payload']) if row else None

    def inventory(self, projects):
        with self.connect() as db:
            rows = [json.loads(r['payload']) for r in db.execute('SELECT * FROM records') if r['project'] in projects]
            scans = {r['project']:dict(r) for r in db.execute('SELECT * FROM scans') if r['project'] in projects}
            active = {r['record']:r['state'] for r in db.execute("SELECT record,state FROM operations WHERE state IN ('queued','running','unknown','partial')")}
        for row in rows:
            row['operation_state'] = active.get(row['id'])
        for scan in scans.values():
            scan['quarantines'] = json.loads(scan['quarantines'])
        return rows, scans

    def enqueue(self, actor, target, batch):
        oid = uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (oid,batch,target['id'],target['project'],json.dumps(actor),json.dumps(target),'queued',now,now,None,None))
            self.event(db,oid,target['project'],actor,'requested',{'target':target})
        return oid

    def update_operation(self, oid, state, message=None, drone_status=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM operations WHERE id=?',(oid,)).fetchone()
            db.execute('UPDATE operations SET state=?,message=?,drone_status=COALESCE(?,drone_status),updated=? WHERE id=?',
                       (state,message,drone_status,time.time(),oid))
            self.event(db,oid,row['project'],json.loads(row['actor']),state,
                       {'message':message,'drone_status':drone_status})
            if state == 'deleted':
                db.execute('DELETE FROM records WHERE id=?',(row['record'],))

    def operations(self, allowed, limit=200):
        if not allowed:
            return []
        marks = ','.join('?' for _ in allowed)
        with self.connect() as db:
            rows = db.execute('SELECT * FROM operations WHERE project IN (%s) ORDER BY created DESC LIMIT ?' % marks,
                              tuple(allowed)+(limit,)).fetchall()
        return [dict(r,actor=json.loads(r['actor']),target=json.loads(r['target'])) for r in rows]

    def audit(self, allowed, before=None):
        if not allowed:
            return []
        marks = ','.join('?' for _ in allowed)
        with self.connect() as db:
            rows = db.execute('SELECT * FROM audit WHERE project IN (%s) AND seq < ? ORDER BY seq DESC LIMIT 100' % marks,
                              tuple(allowed)+(before or 2**63-1,)).fetchall()
        return [dict(r,actor=json.loads(r['actor']),detail=json.loads(r['detail'])) for r in rows]
