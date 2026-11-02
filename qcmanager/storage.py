import errno
import re
import base64
import json
import os
import pwd
import shlex
import subprocess
import sys
from pathlib import Path

from . import fs_helper
from .diagnostics import exception_details


class StorageError(RuntimeError):
    def __init__(self, message, uncertain=False, details=None, ui_message=None):
        super().__init__(message)
        self.uncertain = uncertain
        self.details = details or {}
        self.ui_message = ui_message or '儲存空間操作失敗，請查看服務 log 的診斷資訊。'


def filesystem_error(details, uncertain=False):
    number = details.get('error_errno', details.get('errno'))
    reason = errno.errorcode.get(number, 'UNKNOWN') if isinstance(number, int) else 'UNKNOWN'
    message = {
        'ENOENT': '目錄或路徑不存在，請確認 host 與 run_folder。',
        'EACCES': '沒有目錄存取權限，請檢查服務帳號與路徑各層權限。',
        'EPERM': '檔案系統拒絕操作，請檢查服務帳號權限。',
        'ENOTDIR': '路徑包含非目錄或不允許的符號連結。',
        'ELOOP': '路徑包含不允許的符號連結。',
    }.get(reason, '檔案系統檢查失敗，請查看服務 log 的錯誤類型與 errno。')
    return StorageError('Filesystem inspection failed', uncertain, dict(details, errno_name=reason), message)


def ssh_failure(returncode, stderr, uncertain=False):
    # Classify stderr without logging arbitrary remote shell output or credentials.
    text = (stderr or '').lower()
    cases = [
        ('host key verification failed', 'host_key', 'SSH 主機金鑰驗證失敗，請檢查服務帳號的 known_hosts。'),
        ('remote host identification has changed', 'host_key_changed', 'SSH 主機金鑰已變更，請確認遠端主機後更新 known_hosts。'),
        ('permission denied', 'permission_denied', 'SSH 或遠端命令權限不足，請以服務帳號確認免密碼 SSH 與 Python 執行權限。'),
        ('could not resolve hostname', 'dns', 'SSH 無法解析主機名稱，請檢查 hostname 與 DNS。'),
        ('connection refused', 'refused', 'SSH 連線被拒絕，請檢查遠端 SSH 服務與連接埠。'),
        ('no route to host', 'unreachable', 'SSH 無法連到遠端主機，請檢查網路路由。'),
        ('timed out', 'timeout', 'SSH 連線逾時，請檢查遠端主機與網路。'),
        ('command not found', 'python_missing', '遠端找不到 Python，請設定 ssh.python 的正確路徑。'),
        ('not found', 'command_missing', '遠端命令不存在，請檢查 ssh.python 與 shell 啟動設定。'),
        ('syntaxerror', 'python_syntax', '遠端 Python 無法解析 helper，請確認 ssh.python 為 Python 3.9 以上。'),
        ('importerror', 'python_import', '遠端 Python 載入失敗，請檢查 Python 安裝與環境。'),
    ]
    for pattern, code, message in cases:
        if pattern in text:
            return StorageError('SSH helper failed', uncertain, {'exit_code': returncode, 'reason': code}, message)
    return StorageError('SSH helper failed', uncertain, {'exit_code': returncode, 'reason': 'unclassified'},
                        'SSH helper 執行失敗，請查看服務 log 的 exit_code 並確認遠端 Python 環境。')


class Storage:
    def __init__(self, config):
        self.hosts = config['hosts']

    def request(self, project, action, **fields):
        body = dict(action=action, root=project['run_folder'], prefix=project['prefix'], **fields)
        host = self.hosts[project['host']]
        if host['transport'] == 'local':
            try:
                result = fs_helper.execute(body)
            except Exception as e:
                raise filesystem_error(exception_details(e), action == 'delete') from e
        else:
            source = Path(fs_helper.__file__).with_suffix('.py').read_bytes()
            payload = base64.b64encode(source).decode('ascii')
            script = "import base64;exec(compile(base64.b64decode('%s'),'<qc-helper>','exec'))" % payload
            ssh = host['ssh']
            command = [
                'ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
                '-o', 'ClearAllForwardings=yes', '-o', 'PermitLocalCommand=no',
                '-l', pwd.getpwuid(os.geteuid()).pw_name, host['hostname'],
                shlex.quote(ssh['python']) + ' -c ' + shlex.quote(script),
            ]
            try:
                environment = os.environ.copy()
                if getattr(sys,'frozen',False):
                    # Do not inject bundled libssl/libcrypto into system ssh.
                    if 'LD_LIBRARY_PATH_ORIG' in environment:
                        environment['LD_LIBRARY_PATH'] = environment['LD_LIBRARY_PATH_ORIG']
                    else:
                        environment.pop('LD_LIBRARY_PATH',None)
                proc = subprocess.run(command, input=json.dumps(body), text=True, encoding='utf-8', errors='replace',
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      timeout=min(20,ssh['timeout_seconds']) if action=='disk' else ssh['timeout_seconds'], check=False, env=environment)
            except subprocess.TimeoutExpired as e:
                raise StorageError('SSH command timed out', action == 'delete',
                                   {'timeout_seconds': e.timeout}, 'SSH 命令執行逾時，請檢查遠端負載或調整 ssh.timeout_seconds。') from e
            except OSError as e:
                raise StorageError('Cannot start SSH', action == 'delete', exception_details(e),
                                   '無法啟動 SSH，請檢查服務主機的 ssh 指令與執行權限。') from e
            if proc.returncode:
                raise ssh_failure(proc.returncode, proc.stderr, action == 'delete')
            try:
                result = json.loads(proc.stdout)
            except (ValueError, TypeError) as e:
                raise StorageError('Invalid SSH response', action == 'delete',
                                   {'stdout_bytes': len(proc.stdout.encode('utf-8'))},
                                   'SSH 回傳的資料不是有效 JSON，請檢查 shell 啟動訊息是否混入 stdout。') from e
        if not isinstance(result, dict):
            raise StorageError('Invalid filesystem response', action == 'delete')
        if action != 'delete' and (result.get('error') or 'root_identity' not in result):
            if result.get('error_code') == 'python_version':
                raise StorageError('Unsupported remote Python', ui_message='遠端 Python 版本過舊，需要 Python 3.9 以上。')
            kind = result.get('error_type')
            details = {'error_type': kind if isinstance(kind,str) and re.fullmatch(r'[A-Za-z]{1,64}',kind) else 'Unknown',
                       'error_errno': result.get('error_errno') if type(result.get('error_errno')) is int else None}
            raise filesystem_error(details)
        return result

