# -*- coding: utf-8 -*-
"""Phone details found in the function review: no raw keys in the maintenance badge, SSL mode group wraps."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    return open(os.path.join(ROOT, *p), encoding='utf-8').read()


def test_maintenance_badge_is_translated():
    html = _read('templates', 'maintenance.html')
    assert "{{ e.item_type }}</span>" not in html
    assert "t('maint.type_' ~ e.item_type" in html


def test_ssl_mode_group_wraps_on_phone():
    css = _read('static', 'css', 'custom.css')
    assert '#sslModeGroup { display: flex; flex-wrap: wrap;' in css


def test_demo_maintenance_types_exist_in_the_form():
    seed = _read('tools', 'seed_demo.py')
    form = _read('templates', 'maintenance.html')
    import re
    for typ in re.findall(r'\("([a-z0-9_]+)", "[A-Z]', seed.split('WARTUNG = [')[1].split(']')[0]):
        assert 'value="%s"' % typ in form, typ
