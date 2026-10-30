"""Self-contained Linux helper, also sent over SSH. Requires Python >= 3.9.

Input is JSON on stdin, never interpolated into a shell command. No symlink
directory traversal; fd-relative operations anchor all work to the opened root.
"""
import json
import os
import re
import stat
import sys

FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


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


def disk_size(fd, seen=None):
    if seen is None:
        seen = set()
    root = os.fstat(fd)
    total = root.st_blocks * 512
    for name in os.listdir(fd):
        entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
        key = (entry.st_dev, entry.st_ino)
        if key in seen:
            continue
        seen.add(key)
        if stat.S_ISDIR(entry.st_mode):
            child = child_dir(fd, name)
            try:
                if identity(os.fstat(child)) != identity(entry):
                    raise ValueError('Directory changed while scanning')
                total += disk_size(child, seen)
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
        if action == 'probe':
            return {'root_identity': root_identity, 'python': list(sys.version_info[:3]),
                    'uid': os.geteuid(), 'mount_id': mount_id(fd)}
        prefix = request['prefix']
        if not isinstance(prefix, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', prefix):
            raise ValueError('Invalid prefix')
        pattern = re.compile(re.escape(prefix) + r'_([1-9][0-9]*)', re.ASCII)
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
                        row['bytes'] = disk_size(child)
                    finally:
                        os.close(child)
                except (OSError, ValueError, RecursionError):
                    row['error'] = 'Directory cannot be safely inspected'
                rows.append(row)
                if len(rows) > 10000:
                    raise ValueError('Too many records; split the management root')
            return {'records': rows, 'quarantines': quarantines, 'root_identity': root_identity}
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
                'error': 'Filesystem operation failed (%s)' % type(error).__name__}
    finally:
        os.close(fd)


def main():
    try:
        if sys.version_info < (3, 9):
            raise ValueError('Python 3.9 or newer required')
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError('Request too large')
        result = execute(json.loads(raw))
        print(json.dumps(result))
    except Exception:
        print(json.dumps({'status': 'failed', 'error': 'Invalid request or inaccessible root'}))


if __name__ == '__main__':
    main()
