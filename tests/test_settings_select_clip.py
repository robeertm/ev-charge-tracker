# -*- coding: utf-8 -*-
"""Settings selects must clip inside their box (WebKit sized the import-mode select to its longest option)."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_import_mode_select_clips():
    html = open(os.path.join(ROOT, 'templates', 'settings.html'), encoding='utf-8').read()
    tag = re.search(r'<select[^>]*id="importMode"[^>]*>', html).group(0)
    assert 'overflow:hidden' in tag


def test_language_select_wide_enough():
    html = open(os.path.join(ROOT, 'templates', 'settings.html'), encoding='utf-8').read()
    tag = re.search(r'<select[^>]*name="app_language"[^>]*>', html).group(0)
    assert 'max-width:320px' in tag
