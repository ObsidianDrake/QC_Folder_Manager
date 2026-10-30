import base64
import json
import os
import pwd
import shlex
import subprocess
import sys
from pathlib import Path

from . import fs_helper


class StorageError(RuntimeError):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


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
                raise StorageError('Local filesystem unavailable', action == 'delete') from e
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
                proc = subprocess.run(command, input=json.dumps(body), text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      timeout=ssh['timeout_seconds'], check=False, env=environment)
            except (subprocess.TimeoutExpired, OSError) as e:
                raise StorageError('SSH unavailable or timed out; reconcile remote state', action == 'delete') from e
            if proc.returncode:
                raise StorageError('SSH helper failed; verify remote Python and permissions', action == 'delete')
            try:
                result = json.loads(proc.stdout)
            except (ValueError, TypeError) as e:
                raise StorageError('Invalid SSH response; verify shell startup output', action == 'delete') from e
        if not isinstance(result, dict):
            raise StorageError('Invalid filesystem response', action == 'delete')
        if action != 'delete' and (result.get('error') or 'root_identity' not in result):
            raise StorageError('Cannot inspect management root')
        return result
