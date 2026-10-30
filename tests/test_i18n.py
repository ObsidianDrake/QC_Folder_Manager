"""Keep translations complete as UI text and controlled server messages evolve."""
import ast
import json
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def catalog():
    source = (ROOT/'qcmanager/static/i18n.js').read_text()
    return json.JSONDecoder().raw_decode(source.split('const translations = ', 1)[1])[0]


def test_static_text_and_accessibility_labels_have_english_translations():
    texts = []
    class Page(HTMLParser):
        def handle_data(self, data):
            texts.append(data.strip())
        def handle_starttag(self, tag, attrs):
            texts.extend(value for key, value in attrs if key in ('aria-label', 'placeholder'))
    Page().feed((ROOT/'qcmanager/static/index.html').read_text())
    translations = catalog()
    for text in texts:
        if re.search('[\u4e00-\u9fff]', text) and text not in ('繁體中文', '語言 / Language'):
            assert translations.get(text), text


def test_controlled_server_messages_have_english_translations():
    translations = catalog()
    for filename in ('service.py', 'web.py'):
        tree = ast.parse((ROOT/'qcmanager'/filename).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search('[\u4e00-\u9fff]', node.value):
                assert translations.get(node.value), node.value
    for original, translated in translations.items():
        assert set(re.findall(r'\{\w+\}', original)) == set(re.findall(r'\{\w+\}', translated))
