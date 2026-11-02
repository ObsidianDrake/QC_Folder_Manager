"""Explicit browser response fields; internal storage metadata stays server-side."""

def select(value, fields):
    return {key: value[key] for key in fields if key in value}


def project(value):
    return select(value, ('id', 'organization', 'repository'))


def actor(value):
    return select(value, ('id', 'login', 'is_admin'))


def target(value):
    return select(value, ('id', 'project', 'name', 'number', 'organization', 'repository'))


def inventory(value):
    records = [select(row, ('id', 'project', 'name', 'number', 'organization', 'repository',
        'bytes', 'size_at', 'size_state', 'scanned_at', 'drone_status', 'drone_url',
        'operation_state', 'deletable', 'delete_reason')) for row in value['records']]
    scans = {}
    for pid, scan in value['scans'].items():
        scans[pid] = dict(select(scan, ('state', 'updated')),
            error='掃描失敗，請聯絡管理員並提供專案名稱。' if scan.get('error') else None,
            quarantine_count=len(scan.get('quarantines', [])))
    return dict(records=records, projects=[project(p) for p in value['projects']], scans=scans)


def disks(rows):
    return [dict(select(row, ('available', 'total', 'used', 'updated', 'stale', 'error', 'refreshing')),
                 projects=[project(p) for p in row['projects']]) for row in rows]


def operation_message(state):
    return '操作未完成，請聯絡管理員並提供紀錄編號。' if state in ('failed', 'partial', 'unknown', 'rejected') else {'deleted':'已刪除', 'queued':'等待執行', 'running':'清理處理中'}.get(state)


def operations(rows):
    return [dict(select(row, ('id', 'batch', 'record', 'project', 'state', 'created', 'updated', 'drone_status')),
                 actor=actor(row['actor']), target=target(row['target']),
                 message=operation_message(row['state'])) for row in rows]


def audit(rows):
    result = []
    for row in rows:
        detail = {}
        if isinstance(row['detail'].get('target'), dict):
            detail['target'] = target(row['detail']['target'])
        detail['message'] = operation_message(row['event'])
        result.append(dict(select(row, ('seq', 'at', 'operation', 'project', 'event')),
                           actor=actor(row['actor']), detail=detail))
    return result
