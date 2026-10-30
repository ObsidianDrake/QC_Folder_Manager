import logging
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .clients import APIError, TERMINAL, permitted
from .storage import StorageError


class RequestError(RuntimeError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Manager:
    def __init__(self, config, db, gitea, drone, storage):
        self.config, self.db, self.gitea, self.drone, self.storage = config, db, gitea, drone, storage
        self.projects = {p['id']: p for p in config['projects']}
        self.pool = ThreadPoolExecutor(max_workers=config['server']['workers'], thread_name_prefix='qc-worker')
        self.locks = {key: threading.Lock() for key in self.projects}
        self.size_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='qc-size')
        self.size_capacity = threading.BoundedSemaphore(200)
        self.pending_sizes = set()
        self.pending_scans = set()
        self.guard = threading.Lock()
        self.capacity = threading.BoundedSemaphore(200)
        self.log = logging.getLogger('qcmanager')

    def allowed(self, actor):
        return {key: p for key,p in self.projects.items() if permitted(actor,p)}

    def refresh_actor(self, token, original):
        actor = self.gitea.identity(token)
        if actor['id'] != original['id']:
            raise RequestError('Login identity changed',403)
        return actor

    def submit(self, fn, *args):
        if not self.capacity.acquire(blocking=False):
            raise RequestError('工作佇列已滿，請稍後重試。',429)
        def run():
            try:
                fn(*args)
            finally:
                self.capacity.release()
        try:
            return self.pool.submit(run)
        except BaseException:
            self.capacity.release()
            raise

    def scan(self, pid, actor):
        if pid not in self.allowed(actor):
            raise RequestError('找不到可存取的專案。',404)
        with self.guard:
            if pid in self.pending_scans:
                return
            self.pending_scans.add(pid)
        try:
            self.db.start_scan(pid)
            self.submit(self._scan,pid)
        except Exception:
            with self.guard:
                self.pending_scans.discard(pid)
            self.db.finish_scan(pid,error='Scan could not be scheduled')
            raise

    def _scan(self, pid):
        try:
            with self.locks[pid]:
                project = self.projects[pid]
                result = self.storage.request(project,'scan',metadata_only=True)
                for row in result['records']:
                    row.update(drone_checked_at=None, drone_status='unknown', drone_error=None, build_created=None,
                               drone_url=self.drone.link(project,row['number']))
                # Publish directory names first. Recursive size I/O uses a separate worker.
                self.db.finish_scan(pid,result=result,state="checking")
                snapshots, _ = self.db.inventory({pid})
                self.queue_sizes([row for row in snapshots if row.get('bytes') is None and not row.get('error')])
                for row in snapshots:
                    fields = {'drone_checked_at': time.time()}
                    try:
                        build = self.drone.build(project,row['number'])
                        fields.update(drone_status=build['status'], build_created=build.get('created'))
                    except APIError:
                        fields.update(drone_status='unknown', drone_error='Drone 狀態無法確認')
                    self.db.patch_record(row, fields)
                self.db.scan_ready(pid)
                self.log.info('scan completed project=%s records=%d',pid,len(result['records']))
        except Exception:
            self.db.finish_scan(pid,error='掃描失敗；請檢查 filesystem、SSH、Python 與服務連線。')
            self.log.warning('scan failed project=%s',pid)
        finally:
            with self.guard:
                self.pending_scans.discard(pid)

    def sizes(self, ids, actor):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(not isinstance(x,str) for x in ids) or len(set(ids)) != len(ids):
            raise RequestError('請選擇 1–100 筆不重複的紀錄。')
        allowed = self.allowed(actor)
        rows = [self.db.record(rid) for rid in ids]
        if any(not row or row['project'] not in allowed for row in rows):
            raise RequestError('包含無權限或不存在的紀錄。',404)
        return {'accepted': self.queue_sizes(rows)}

    def queue_sizes(self, rows):
        if not rows:
            return 0
        if not self.size_capacity.acquire(blocking=False):
            return 0
        jobs = []
        try:
            with self.guard:
                for row in rows:
                    if row['id'] in self.pending_sizes:
                        continue
                    ticket = uuid.uuid4().hex
                    if self.db.patch_record(row, {'size_state':'queued','size_error':None,'size_ticket':ticket}):
                        self.pending_sizes.add(row['id'])
                        jobs.append((row,ticket))
            if not jobs:
                self.size_capacity.release()
                return 0
            self.size_pool.submit(self._sizes, jobs)
            return len(jobs)
        except Exception:
            for row,ticket in jobs:
                self.db.patch_record(row, {'size_state':'interrupted'},ticket)
                with self.guard:
                    self.pending_sizes.discard(row['id'])
            self.size_capacity.release()
            raise

    def _sizes(self, jobs):
        try:
            for row,ticket in jobs:
                try:
                    if not self.db.patch_record(row, {'size_state':'running'},ticket):
                        continue
                    result = self.storage.request(self.projects[row['project']], 'size',
                        name=row['name'], identity=row['identity'], root_identity=row['root_identity'])
                    self.db.patch_record(row, {'bytes':result['bytes'],'size_at':time.time(),
                                              'size_state':'cached','size_error':None},ticket)
                except Exception:
                    self.db.patch_record(row, {'size_state':'failed','size_error':'Capacity calculation failed'},ticket)
                finally:
                    with self.guard:
                        self.pending_sizes.discard(row['id'])
        finally:
            self.size_capacity.release()

    def inventory(self, actor):
        allowed = self.allowed(actor)
        rows, scans = self.db.inventory(allowed)
        for row in rows:
            project = allowed[row['project']]
            row['organization'] = project['organization']
            row['repository'] = project['repository']
            row['host'] = project['host']
            row['path'] = project['run_folder']+'/'+row['name']
            scan_ok = scans.get(row['project'],{}).get('state') == 'ready'
            row['deletable'] = bool(scan_ok and row.get('drone_status') in TERMINAL and not row.get('error') and not row.get('operation_state'))
            if row.get('operation_state'):
                reason = '清理工作處理中或待人工核對'
            elif not scan_ok:
                reason = '請完成一次成功掃描'
            elif row.get('error'):
                reason = '無法安全檢查目錄'
            elif row.get('drone_status') not in TERMINAL:
                reason = 'Drone 尚未結束或狀態未知'
            else:
                reason = ''
            row['delete_reason'] = reason
            row.pop('identity',None)
            row.pop('root_identity',None)
        return {'records':sorted(rows,key=lambda x:x['number'],reverse=True), 'scans':scans,
                'projects':[dict(p, environment=self.config['hosts'][p['host']]['environment']) for p in allowed.values()]}

    def delete(self, ids, actor, token):
        if not isinstance(ids,list) or not 1 <= len(ids) <= 100 or any(not isinstance(x,str) for x in ids) or len(set(ids)) != len(ids):
            raise RequestError('請選擇 1–100 筆不重複的紀錄。')
        allowed = self.allowed(actor)
        targets = []
        for rid in ids:
            row = self.db.record(rid)
            if not row or row['project'] not in allowed:
                # Persist refusal without returning target details to the caller.
                self.db.rejected(actor,row['project'] if row else None,{'reason':'unauthorized or missing record'})
                raise RequestError('包含無權限或不存在的紀錄。',404)
            if row.get('drone_checked_at') is None:
                raise RequestError('請完成一次成功掃描',409)
            if row.get('error'):
                self.db.rejected(actor,row['project'],{'reason':'unsafe inspection','record':rid})
                raise RequestError('目錄無法安全檢查，請重新掃描。',409)
            p = allowed[row['project']]
            targets.append(dict(row,organization=p['organization'],repository=p['repository'],host=p['host'],path=p['run_folder']+'/'+row['name']))
        batch = uuid.uuid4().hex
        results = []
        for row in targets:
            try:
                oid = self.db.enqueue(actor,row,batch)
            except sqlite3.IntegrityError:
                results.append({'record':row['id'],'error':'已有清理工作或結果待核對'})
                continue
            try:
                self.submit(self._delete,oid,row,actor,token)
            except Exception:
                self.db.update_operation(oid,'failed','工作無法排入佇列，未執行刪除')
            results.append({'record':row['id'],'operation':oid})
        return {'batch':batch,'results':results}

    def _delete(self, oid, row, original_actor, token):
        pid = row['project']
        filesystem_started = False
        try:
            with self.locks[pid]:
                project = self.projects[pid]
                actor = self.refresh_actor(token,original_actor)
                if not permitted(actor,project):
                    self.db.update_operation(oid,'rejected','Gitea 權限已變更')
                    return
                build = self.drone.build(project,row['number'])
                if build['status'] not in TERMINAL:
                    self.db.update_operation(oid,'rejected','Drone 執行中、等待中或狀態未知',build['status'])
                    return
                # If the build number was reused, don't delete the old snapshot.
                if row.get('build_created') is not None and build.get('created') != row['build_created']:
                    self.db.update_operation(oid,'rejected','Drone build 已被替換，請重新掃描',build['status'])
                    return
                self.db.update_operation(oid,'running','已通過最新權限與 Drone 狀態檢查',build['status'])
                filesystem_started = True
                result = self.storage.request(project,'delete',name=row['name'],identity=row['identity'],
                                              root_identity=row['root_identity'],operation_id=oid)
                state = result.get('status')
                if state not in ('deleted','failed','partial'):
                    state = 'unknown'
                self.db.update_operation(oid,state,result.get('error') or ('已刪除' if state=='deleted' else '需核對清理結果'))
                self.log.info('delete result operation=%s state=%s',oid,state)
        except (APIError, RequestError):
            self.db.update_operation(oid,'rejected','無法重新確認 Gitea 身分或 Drone 狀態；未執行刪除')
        except StorageError as e:
            self.db.update_operation(oid,'unknown' if e.uncertain else 'failed',str(e))
        except Exception:
            # The persisted running event survives even if updating the DB fails.
            try:
                self.db.update_operation(oid,'unknown' if filesystem_started else 'failed','工作中斷；請核對紀錄與 filesystem')
            except Exception:
                pass
            self.log.error('delete interrupted operation=%s filesystem_started=%s',oid,filesystem_started)

    def shutdown(self):
        self.pool.shutdown(wait=True)
        self.size_pool.shutdown(wait=True)
