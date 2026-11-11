"""Self-contained Linux helper, also sent over SSH. Requires Python >= 3.9.

Input is JSON on stdin, never interpolated into a shell command. No symlink
directory traversal; fd-relative operations anchor all work to the opened root.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile

FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def failure_details(error):
    # Structured fields only: exception strings may contain private paths or input.
    if isinstance(error, DustError):
        return dict(error.details, error_type='DustError')
    return {'error_type': type(error).__name__,
            'error_errno': error.errno if isinstance(error, OSError) else None}


class DustError(ValueError):
    def __init__(self, reason, **details):
        super().__init__(reason)
        self.details = dict(reason=reason, **details)


def dust_tree(fd, options, depth=0, lines=1):
    """Measure an opened run directory; never invoke a shell or user dust config."""
    executable = options.get('executable', 'dust')
    # Resolve PATH before changing cwd to the vendor-controlled run folder.
    if not os.path.isabs(executable):
        executable = shutil.which(executable)
        if executable is None:
            raise DustError('dust_not_found')
        executable = os.path.abspath(executable)
    timeout = options.get('timeout_seconds', 1800)
    environment = os.environ.copy()
    environment['LC_ALL'] = 'C'
    # A bundled Python's libraries must not be injected into an external binary.
    if getattr(sys, 'frozen', False):
        if 'LD_LIBRARY_PATH_ORIG' in environment:
            environment['LD_LIBRARY_PATH'] = environment['LD_LIBRARY_PATH_ORIG']
        else:
            environment.pop('LD_LIBRARY_PATH', None)
    with tempfile.TemporaryDirectory(prefix='qc-dust-') as temporary:
        # Dust 1.1.1 has no --config flag. Isolate only this child's configuration
        # lookup; never change the service account's environment or home files.
        environment['HOME'] = temporary
        environment['XDG_CONFIG_HOME'] = temporary
        command = [executable, '-j', '-o', 'b', '-d', str(depth), '-D',
                   '-n', str(lines), '-P', '-c', '-x', '-T', str(options.get('threads', 4)), '--', '.']
        try:
            proc = subprocess.run(command, cwd='/proc/self/fd/%d' % fd, pass_fds=(fd,),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding='utf-8', errors='replace', env=environment,
                                  timeout=timeout, check=False)
        except FileNotFoundError as error:
            raise DustError('dust_not_found') from error
        except subprocess.TimeoutExpired as error:
            raise DustError('dust_timeout', timeout_seconds=timeout) from error
        except OSError as error:
            raise DustError('dust_start_failed', error_errno=error.errno) from error
    # Dust can return zero even after a permission/I/O error. Only disappearing
    # entries are acceptable for an estimate; other warnings retain the old value.
    warnings = [line for line in proc.stderr.splitlines() if line.strip()]
    if any(not line.startswith('No such file or directory: ') for line in warnings):
        reason = 'dust_permission_denied' if 'permission' in proc.stderr.lower() else 'dust_scan_error'
        raise DustError(reason, exit_code=proc.returncode)
    if proc.returncode:
        raise DustError('dust_exit_failed', exit_code=proc.returncode)
    try:
        result = json.loads(proc.stdout)
        dust_bytes(result)
        return result
    except (ValueError, TypeError, KeyError) as error:
        raise DustError('dust_invalid_output') from error


def dust_bytes(node):
    value = node['size']
    if isinstance(value, str) and re.fullmatch(r'[0-9]+B?', value):
        value = int(value.rstrip('B'))
    if type(value) is not int or value < 0:
        raise DustError('dust_invalid_output')
    return value


def dust_size(fd, options):
    return dust_bytes(dust_tree(fd, options))


def project_sizes(fd, pattern, options):
    names = os.listdir(fd)
    before = {}
    for name in names:
        if not pattern.fullmatch(name):
            continue
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                continue
            child = child_dir(fd, name)
            try:
                if identity(os.fstat(child)) == identity(info):
                    before[name] = identity(info)
            finally:
                os.close(child)
        except FileNotFoundError:
            continue
    # One recursive walk for the entire project, including all immediate folders.
    tree = dust_tree(fd, options, depth=1, lines=len(names)+1)
    records = {}
    try:
        children = tree['children']
        if not isinstance(children, list):
            raise ValueError('Invalid children')
        for node in children:
            name = node['name']
            if not isinstance(name, str):
                raise ValueError('Invalid name')
            name = name[2:] if name.startswith('./') else name
            if name not in before:
                continue
            if name in records:
                raise ValueError('Duplicate folder')
            value = dust_bytes(node)
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if identity(info) == before[name] and stat.S_ISDIR(info.st_mode):
                records[name] = {'bytes': value, 'identity': before[name]}
    except (ValueError, KeyError, TypeError) as error:
        raise DustError('dust_invalid_output') from error
    return records


def identity(info):
    return [info.st_dev, info.st_ino]


def mount_id(fd):
    with open('/proc/self/fdinfo/%d' % fd) as stream:
        for line in stream:
            if line.startswith('mnt_id:'):
                return line.split(':', 1)[1].strip()
    raise ValueError('Cannot verify mount boundary')


def open_root(path):
    if not isinstance(path, str) or not path.startswith('/') or path == '/':
        raise ValueError('Invalid root')
    parts = path[1:].split('/')
    if any(x in ('', '.', '..') for x in parts):
        raise ValueError('Root must be normalized')
    fd = os.open('/', FLAGS)
    try:
        for part in parts:
            child = os.open(part, FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def child_dir(fd, name):
    child = os.open(name, FLAGS, dir_fd=fd)
    try:
        if mount_id(child) != mount_id(fd):
            raise ValueError('Nested mount point is not managed')
        return child
    except BaseException:
        os.close(child)
        raise


def disk_size(fd, seen=None, tolerate_missing=False):
    if seen is None:
        seen = set()
    root = os.fstat(fd)
    total = root.st_blocks * 512
    for name in os.listdir(fd):
        try:
            entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            if tolerate_missing:
                continue
            raise
        key = (entry.st_dev, entry.st_ino)
        if key in seen:
            continue
        seen.add(key)
        if stat.S_ISDIR(entry.st_mode):
            try:
                child = child_dir(fd, name)
            except FileNotFoundError:
                if tolerate_missing:
                    continue
                raise
            try:
                if identity(os.fstat(child)) != identity(entry):
                    raise ValueError('Directory changed while scanning')
                total += disk_size(child, seen, tolerate_missing=tolerate_missing)
            finally:
                os.close(child)
        else:
            total += entry.st_blocks * 512
    return total


def empty_dir(fd):
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = child_dir(fd, name)
            try:
                if identity(os.fstat(child)) != identity(info):
                    raise ValueError('Directory changed during deletion')
                empty_dir(child)
                if identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != identity(info):
                    raise ValueError('Directory replaced during deletion')
                os.rmdir(name, dir_fd=fd)
            finally:
                os.close(child)
        else:
            # This unlinks symlinks themselves, never their targets.
            os.unlink(name, dir_fd=fd)


def execute(request):
    action = request['action']
    fd = open_root(request['root'])
    quarantine = None
    moved = False
    try:
        root_identity = identity(os.fstat(fd))
        if action == 'disk':
            mid = mount_id(fd)
            mount = None
            with open('/proc/self/mountinfo') as stream:
                for line in stream:
                    fields = line.split()
                    if fields[0] == mid:
                        mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), fields[4])
                        break
            if mount is None:
                raise ValueError('Mount information unavailable')
            info = os.fstatvfs(fd)
            unit = info.f_frsize
            return {'root_identity': root_identity, 'mount_id': mid, 'mount': mount,
                    'total': info.f_blocks * unit, 'available': max(0, info.f_bavail) * unit,
                    'used': max(0, info.f_blocks - info.f_bfree) * unit}
        if action == 'probe':
            return {'root_identity': root_identity, 'python': list(sys.version_info[:3]),
                    'uid': os.geteuid(), 'mount_id': mount_id(fd)}
        prefix = request['prefix']
        if not isinstance(prefix, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', prefix):
            raise ValueError('Invalid prefix')
        pattern = re.compile(re.escape(prefix) + r'_([1-9][0-9]*)', re.ASCII)
        if action == 'project_sizes':
            return {'root_identity': root_identity,
                    'records': project_sizes(fd, pattern, request.get('dust', {}))}
        if action == 'scan':
            rows, quarantines = [], []
            names = os.listdir(fd)
            for name in sorted(names):
                if name.startswith('.qcfm-delete-'):
                    quarantines.append(name)
                match = pattern.fullmatch(name)
                if not match or int(match.group(1)) > 2**63 - 1:
                    continue
                entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISDIR(entry.st_mode):
                    continue
                row = {'name': name, 'number': int(match.group(1)), 'identity': identity(entry),
                       'root_identity': root_identity, 'mtime': entry.st_mtime,
                       'bytes': None, 'error': None}
                try:
                    child = child_dir(fd, name)
                    try:
                        if identity(os.fstat(child)) != row['identity']:
                            raise ValueError('Directory changed during scan')
                        if not request.get('metadata_only', False):
                            row['bytes'] = disk_size(child)
                    finally:
                        os.close(child)
                except (OSError, ValueError, RecursionError):
                    row['error'] = 'Directory cannot be safely inspected'
                rows.append(row)
                if len(rows) > 10000:
                    raise ValueError('Too many records; split the management root')
            return {'records': rows, 'quarantines': quarantines, 'root_identity': root_identity}
        if action == 'size':
            name = request['name']
            if not isinstance(name, str) or not pattern.fullmatch(name):
                raise ValueError('Invalid record name')
            if request['root_identity'] != root_identity:
                raise ValueError('Management root changed')
            child = child_dir(fd, name)
            try:
                if identity(os.fstat(child)) != request['identity']:
                    raise ValueError('Directory changed')
                size = dust_size(child, request.get('dust', {}))
                if identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != request['identity']:
                    raise ValueError('Directory replaced during size calculation')
                return {'root_identity': root_identity, 'bytes': size}
            finally:
                os.close(child)
        if action != 'delete':
            raise ValueError('Unknown action')
        name = request['name']
        if not isinstance(name, str) or not pattern.fullmatch(name):
            raise ValueError('Invalid record name')
        if request['root_identity'] != root_identity:
            raise ValueError('Management root was replaced; rescan required')
        operation = request['operation_id']
        if not re.fullmatch(r'[a-f0-9]{32}', operation):
            raise ValueError('Invalid operation ID')
        quarantine = '.qcfm-delete-' + operation
        child = child_dir(fd, name)
        try:
            expected = request['identity']
            if identity(os.fstat(child)) != expected:
                raise ValueError('Record changed; rescan required')
            # Validate the complete tree before any destructive operation.
            disk_size(child)
            try:
                os.stat(quarantine, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError('Operation already exists; manual reconciliation required')
            os.rename(name, quarantine, src_dir_fd=fd, dst_dir_fd=fd)
            moved = True
            if identity(os.stat(quarantine, dir_fd=fd, follow_symlinks=False)) != expected:
                raise ValueError('Record replaced before quarantine; no contents removed')
            empty_dir(child)
            if identity(os.stat(quarantine, dir_fd=fd, follow_symlinks=False)) != expected:
                raise ValueError('Quarantine replaced during deletion')
            os.rmdir(quarantine, dir_fd=fd)
            return {'status': 'deleted', 'quarantine': quarantine}
        finally:
            os.close(child)
    except (OSError, ValueError, RecursionError, KeyError, TypeError) as error:
        # Never return unfiltered OS paths, command text or credentials.
        return {'status': 'partial' if moved else 'failed', 'quarantine': quarantine if moved else None,
                'error': 'Filesystem operation failed (%s)' % type(error).__name__,
                **failure_details(error)}
    finally:
        os.close(fd)


def main():
    try:
        if sys.version_info < (3, 9):
            print(json.dumps({'error': 'Python 3.9 or newer required', 'error_code': 'python_version'}))
            return
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError('Request too large')
        result = execute(json.loads(raw))
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error': 'Invalid request or inaccessible root',
                          **failure_details(error)}))


if __name__ == '__main__':
    main()

