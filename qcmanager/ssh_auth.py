"""OpenSSH password delivery without an extra executable dependency."""
from contextlib import contextmanager
from pathlib import Path
import tempfile


@contextmanager
def password_environment(environment, password, directory):
    if password is None:
        yield environment
        return
    Path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='ssh-askpass-', dir=directory) as temporary:
        helper = Path(temporary)/'askpass'
        helper.write_text('''#!/bin/sh
case "$1" in
  *assword:*) printf '%s\\n' "$QC_FOLDER_SSH_PASSWORD" ;;
  *) exit 1 ;;
esac
''', encoding='ascii')
        helper.chmod(0o700)
        child = dict(environment, SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE='force',
                     DISPLAY='qcfm:0', LC_ALL='C', QC_FOLDER_SSH_PASSWORD=password)
        yield child
