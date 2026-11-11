import json
import logging
import os
import pwd
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .clients import APIError, TERMINAL, permitted
from .storage import StorageError
from .diagnostics import exception_details
from .disks import DiskCache


class RequestError(RuntimeError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Manager:
    def __init__(self, config, db, gitea, drone, storage):
        self.config, self.db, self.gitea, self.drone, self.storage = config, db, gitea, drone, storage
        self.projects = {p['id']: p for p in config['projects']}
        self.disks = DiskCache(storage,self.projects)
        self.pool = ThreadPoolExecutor(max_workers=config['server']['workers'], thread_name_prefix='qc-worker')
        self.locks = {key: threading.Lock() for key in self.projects}
        self.size_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='qc-size')
        self.size_capacity = threading.BoundedSemaphore(200)
        self.pending_sizes = set()
        self.pending_size_projects = set()
        self.pending_scans = set()
        self.scan_finished = {}
        self.scan_scheduler = None
        self.guard = threading.Lock()
        self.capacity = threading.BoundedSemaphore(200)
        self.log = logging.getLogger('qcmanager')

    def start_scanning(self, settings):
        from .scan_scheduler import ScanScheduler
        if self.scan_scheduler is None:
            self.scan_scheduler = ScanScheduler(self, settings, self.config['server']['scan_interval_seconds'])
            self.scan_scheduler.start()

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
        self._queue_scan(pid)

    def _queue_scan(self, pid):
        # Caller holds the project settings lock; automatic scans need no user token.
        with self.guard:
            if pid in self.pending_scans:
                return
            self.pending_scans.add(pid)
        try:
            self.db.start_scan(pid)
            self.submit(self._scan,pid)
        except Exception:
            with self.guard:
                self.scan_finished[pid] = time.monotonic()
                self.pending_scans.discard(pid)
            self.db.finish_scan(pid,error='Scan could not be scheduled')
            raise

    def _scan(self, pid):
        stage = 'filesystem'
        project = self.projects[pid]
        try:
            with self.locks[pid]:
                project = self.projects[pid]
                result = self.storage.request(project,'scan',metadata_only=True)
                stage = 'inventory'
                for row in result['records']:
                    row.update(drone_checked_at=None, drone_status='unknown', drone_error=None, build_created=None,
                               drone_url=self.drone.link(project,row['number']))
                # Publish directory names first. Recursive size I/O uses a separate worker.
                self.db.finish_scan(pid,result=result,state="checking")
                snapshots, _ = self.db.inventory({pid})
                self.queue_project_size(pid)
                stage = 'drone'
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
        except Exception as error:
            message = error.ui_message if isinstance(error,StorageError) else '掃描發生非預期錯誤，請查看服務 log 的診斷資訊。'
            host = self.config['hosts'][project['host']]
            details = dict(project=pid, stage=stage, user=pwd.getpwuid(os.geteuid()).pw_name,
                           host=host.get('hostname',project['host']),
                           transport=host['transport'], root=project['run_folder'],
                           python=host.get('ssh',{}).get('python'), exception=exception_details(error))
            if isinstance(error,StorageError):
                details['storage'] = error.details
            self.log.warning('scan failed diagnostic=%s',json.dumps(details,ensure_ascii=True))
            self.db.finish_scan(pid,error=message)
        finally:
            with self.guard:
                self.scan_finished[pid] = time.monotonic()
                self.pending_scans.discard(pid)

    def project_sizes(self, ids, actor):
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 100 or
                any(not isinstance(pid, str) for pid in ids) or len(set(ids)) != len(ids)):
            raise RequestError('請選擇要掃描的專案。')
        if any(pid not in self.allowed(actor) for pid in ids):
            raise RequestError('包含無權限的專案。',404)
        return {'accepted': sum(self.queue_project_size(pid) for pid in ids)}

    def queue_sizes(self, rows):
        # Compatibility for internal callers: always expand to whole projects.
        return sum(self.queue_project_size(pid) for pid in dict.fromkeys(row['project'] for row in rows))

    def queue_project_size(self, pid):
        if not self.size_capacity.acquire(blocking=False):
            raise RequestError('工作佇列已滿，請稍後重試。',429)
        jobs = []
        with self.guard:
            if pid in self.pending_size_projects:
                self.size_capacity.release()
                return 0
            self.pending_size_projects.add(pid)
        try:
            rows, _ = self.db.inventory({pid})
            with self.guard:
                for row in rows:
                    ticket = uuid.uuid4().hex
                    if self.db.patch_record(row, {'size_state':'queued','size_error':None,'size_ticket':ticket}):
                        self.pending_sizes.add(row['id'])
                        jobs.append((row,ticket))
            self.db.set_size_status(pid, 'queued')
            self.size_pool.submit(self._sizes, pid, jobs)
            return 1
        except Exception:
            for row,ticket in jobs:
                self.db.patch_record(row, {'size_state':'interrupted'},ticket)
            with self.guard:
                self.pending_size_projects.discard(pid)
                self.pending_sizes.difference_update(row['id'] for row,ticket in jobs)
            self.db.set_size_status(pid, 'interrupted')
            self.size_capacity.release()
            raise

    def _sizes(self, pid, jobs):
        try:
            with self.locks[pid]:
                self.db.set_size_status(pid, 'running')
                for row,ticket in jobs:
                    self.db.patch_record(row, {'size_state':'running'},ticket)
                result = self.storage.request(self.projects[pid], 'project_sizes')
                completed = time.time()
                for row,ticket in jobs:
                    found = result['records'].get(row['name'])
                    if (found and found['identity'] == row['identity'] and
                            result['root_identity'] == row['root_identity']):
                        self.db.patch_record(row, {'bytes':found['bytes'],'size_at':completed,
                                                  'size_state':'cached','size_error':None},ticket)
                    else:
                        # Do not attach a replaced/disappeared folder's measurement
                        # to an old inventory record. Discovery will reconcile it.
                        self.db.patch_record(row, {'size_state':'interrupted'},ticket)
                        self.log.info('size result skipped changed folder project=%s folder=%s',pid,row['name'])
                self.db.set_size_status(pid, 'cached', updated=completed)
        except Exception as error:
            project = self.projects.get(pid, {})
            host = self.config['hosts'].get(project.get('host'), {})
            details = dict(project=pid, host=host.get('hostname', project.get('host')),
                           transport=host.get('transport'), root=project.get('run_folder'),
                           exception=exception_details(error))
            if isinstance(error, StorageError):
                details['reason'] = error.ui_message
                details['storage'] = error.details
            self.log.warning('size calculation failed diagnostic=%s', json.dumps(details, ensure_ascii=True))
            for row,ticket in jobs:
                self.db.patch_record(row, {'size_state':'failed','size_error':'Capacity calculation failed'},ticket)
            self.db.set_size_status(pid, 'failed', error='Capacity calculation failed')
        finally:
            with self.guard:
                self.pending_size_projects.discard(pid)
                self.pending_sizes.difference_update(row['id'] for row,ticket in jobs)
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
            policy_allowed = actor.get('is_admin', False) or project.get('allow_user_delete', True)
            row['deletable'] = bool(policy_allowed and scan_ok and row.get('drone_status') in TERMINAL and not row.get('error') and not row.get('operation_state'))
            if not policy_allowed:
                reason = '此專案未開放一般使用者刪除，請聯絡管理員。'
            elif row.get('operation_state'):
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
                'size_scans':self.db.size_status(allowed),
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
            if not actor.get('is_admin', False) and not allowed[row['project']].get('allow_user_delete', True):
                self.db.rejected(actor,row['project'],{'reason':'project user deletion disabled','record':rid})
                raise RequestError('此專案未開放一般使用者刪除，請聯絡管理員。',403)
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
                if not actor.get('is_admin', False) and not project.get('allow_user_delete', True):
                    self.db.update_operation(oid,'rejected','此專案未開放一般使用者刪除，請聯絡管理員。')
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
                if state in ('deleted','partial'):
                    self.disks.invalidate(pid)
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
        if self.scan_scheduler is not None:
            self.scan_scheduler.stop()
        self.pool.shutdown(wait=True)
        self.size_pool.shutdown(wait=True)
        self.disks.shutdown()

