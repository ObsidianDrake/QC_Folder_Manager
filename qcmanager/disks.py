"""Non-recursive filesystem capacity queries, independent of folder-size workers."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor


class DiskCache:
    def __init__(self, storage, projects):
        self.storage, self.projects = storage, projects
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='qc-disk')
        self.lock = threading.Lock()
        self.items = {}
        self.pending = set()
        self.invalidated = {}

    def refresh(self, projects, manual=False):
        accepted = 0
        now = time.time()
        with self.lock:
            for pid in projects:
                previous = self.items.get(pid, {})
                due = now - previous.get('attempted', 0) >= (5 if manual else 60)
                due = due or previous.get('attempted', 0) < self.invalidated.get(pid, 0)
                if pid in self.pending or not due:
                    continue
                self.pending.add(pid)
                try:
                    self.pool.submit(self._query, pid)
                    accepted += 1
                except Exception:
                    self.pending.discard(pid)
                    raise
        return accepted

    def _query(self, pid):
        started = time.time()
        try:
            result = self.storage.request(self.projects[pid], 'disk')
            if any(type(result.get(k)) is not int or result[k] < 0 for k in ('total','used','available')):
                raise ValueError('Invalid disk counters')
            if not isinstance(result.get('mount'), str) or not isinstance(result.get('mount_id'), str):
                raise ValueError('Invalid mount identity')
            with self.lock:
                self.items[pid] = dict(result, updated=started, attempted=started, error=False)
        except Exception:
            with self.lock:
                self.items[pid] = dict(self.items.get(pid, {}), attempted=started, error=True)
        finally:
            with self.lock:
                self.pending.discard(pid)

    def invalidate(self, pid):
        with self.lock:
            self.invalidated[pid] = time.time()
        self.refresh([pid])

    def snapshot(self, allowed):
        self.refresh(allowed)
        now = time.time()
        groups = {}
        with self.lock:
            # Filter before grouping: never expose another organization's project or root.
            for pid,p in allowed.items():
                item = dict(self.items.get(pid, {}))
                key = (p['host'], item.get('mount_id', 'unknown:'+pid))
                item.update(stale=bool(item.get('error') or now-item.get('updated',0)>90 or
                                       item.get('updated',0)<self.invalidated.get(pid,0)),
                            refreshing=pid in self.pending)
                groups.setdefault(key, []).append((p,item))
        result = []
        for (host,mid), entries in groups.items():
            latest = max((item for _,item in entries), key=lambda i:i.get('updated',0))
            result.append({
                'host':host, 'mount':latest.get('mount'),
                'available':latest.get('available'), 'total':latest.get('total'), 'used':latest.get('used'),
                'updated':latest.get('updated'), 'stale':any(i['stale'] for _,i in entries),
                'error':any(i.get('error',False) for _,i in entries),
                'refreshing':any(i['refreshing'] for _,i in entries),
                'projects':[{'id':p['id'],'organization':p['organization'],'repository':p['repository']} for p,_ in entries],
            })
        return sorted(result, key=lambda d:(d['host'],d['mount'] or ''))

    def shutdown(self):
        self.pool.shutdown(wait=True)
