import errno
from pathlib import Path

import pytest

from qcmanager.config import ConfigError, load_config


def test_missing_config_reports_absolute_path(tmp_path):
    path = tmp_path / "missing.yml"
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert str(path) in str(error.value)
    assert "errno=2" in str(error.value)


def test_permission_error_reports_reason(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")
    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(ConfigError, match="Permission denied"):
        load_config(tmp_path / "config.yml")


def test_yaml_error_reports_location_without_source(tmp_path):
    path = tmp_path / "config.yml"
    path.write_text("secret: [PRIVATE_VALUE\n", encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(path)
    message = str(error.value)
    assert str(path) in message
    assert "line 2, column 1" in message
    assert "PRIVATE_VALUE" not in message


def test_invalid_utf8_reports_encoding(tmp_path):
    path = tmp_path / "config.yml"
    path.write_bytes(b"bad: \xff")
    with pytest.raises(ConfigError, match="must be UTF-8"):
        load_config(path)


def test_unicode_yaml_uses_explicit_utf8_and_reports_ascii_errors(tmp_path):
    path = tmp_path / "\u8a2d\u5b9a.yml"
    path.write_text("# \u4e2d\u6587\nsecret: [PRIVATE_VALUE\n", encoding="utf-8-sig")
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert "line 3, column 1" in str(error.value)
    str(error.value).encode("ascii")
