"""UTF-8 UI assets must survive non-UTF-8 service account locales."""
from pathlib import Path

import pytest


@pytest.mark.parametrize('default_encoding', ['latin-1', 'ascii', 'utf-8'])
def test_homepage_and_assets_use_utf8(web, monkeypatch, default_encoding):
    app, _, _ = web
    original_open = Path.open

    def legacy_open(self, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        if 'b' not in mode and encoding is None:
            encoding = default_encoding
        return original_open(self, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, 'open', legacy_open)
    client = app.test_client()
    response = client.get('/')
    assert response.status_code == 200
    assert response.content_type == 'text/html; charset=utf-8'
    html = response.data.decode('utf-8')
    assert '使用 Gitea 登入' in html
    assert '繁體中文' in html
    assert '__BASE__' not in html
    for name in ('app.js', 'i18n.js', 'style.css'):
        asset = client.get('/assets/' + name)
        assert asset.status_code == 200
        assert 'charset=utf-8' in asset.content_type
        source = Path(__file__).resolve().parents[1]/'qcmanager'/'static'/name
        assert asset.data == source.read_bytes()
        asset.data.decode('utf-8')
