"""Bounded diagnostic metadata; never stringify exceptions or subprocess commands."""
import errno
import traceback
from pathlib import Path


def exception_details(error):
    details = {'type': type(error).__name__}
    if isinstance(error, OSError) and isinstance(error.errno, int):
        details['errno'] = error.errno
        details['errno_name'] = errno.errorcode.get(error.errno, 'UNKNOWN')
    details['frames'] = ['%s:%d:%s' % (Path(frame.filename).name, frame.lineno, frame.name)
                         for frame in traceback.extract_tb(error.__traceback__)[-6:]]
    return details
