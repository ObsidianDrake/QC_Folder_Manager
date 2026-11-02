"""Guard compatibility with separately provided Linux libpython libraries."""
import sys
import sysconfig
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux build compatibility')
def test_shared_library_discovery_with_static_interpreter_metadata(monkeypatch):
    bindepend = pytest.importorskip('PyInstaller.depend.bindepend')
    expected = bindepend.get_python_library_path()
    if not expected:
        pytest.skip('Build host has no shared Python library')
    original = sysconfig.get_config_var
    static_metadata = {'Py_ENABLE_SHARED': 0,
                       'INSTSONAME': 'libpython%d.%d.a' % sys.version_info[:2]}
    monkeypatch.setattr(sysconfig, 'get_config_var',
                        lambda name: static_metadata[name] if name in static_metadata else original(name))
    monkeypatch.setattr(bindepend.compat, 'is_conda', False)
    actual = bindepend.get_python_library_path()
    assert actual and Path(actual).samefile(expected)
