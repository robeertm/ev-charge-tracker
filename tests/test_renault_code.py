# -*- coding: utf-8 -*-
"""Renault/Dacia e-mail confirmation code (v3.0.163).

Since October 2026 Renault asks some accounts for an e-mail code at sign-in
(Gigya 403101). renault-api 0.6 can answer it, but keeps the open challenge
in memory only — and in a web app "send me the code" and "here it is" are
two separate requests. These tests pin the parts that make it work for an
owner: the code is mailed only when a person pressed Test, the challenge
survives between the two requests, the device is remembered afterwards so
the background sync signs in without a code, and nothing of it ends up
where it should not (file name, file mode).

Only the network is fake (``tests/_renault_fake.py``); renault-api's own
two-factor bookkeeping runs for real.
"""
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

pytest.importorskip('renault_api')
import _renault_fake as fake  # noqa: E402
from services.vehicle import connector_renault as cr  # noqa: E402

LANGS = ('de', 'en', 'fr', 'es', 'it', 'nl')
USER = 'owner@example.invalid'


@pytest.fixture
def net(monkeypatch, tmp_path):
    monkeypatch.setattr(cr, 'STATE_DIR', str(tmp_path / 'renault'))
    return fake.install(monkeypatch.setattr, tfa=True)


def _conn():
    return cr.RenaultConnector({'username': USER, 'password': 'pw', 'locale': 'fr_FR', 'vin': ''})


def _state():
    return cr._load_state(USER)


def test_test_button_mails_the_code_and_says_where(net):
    with pytest.raises(cr.RenaultCodeNeeded) as e:
        _conn().verify_credentials()
    assert net['mails'] == 1
    assert 'o***@example.invalid' in str(e.value)
    assert net['mail_lang'] == 'fr'                  # mail in the owner's language
    pend = _state()['pending']
    assert pend['reg_token'] == 'reg-token-1' and pend['phv_token'] == 'phv-1'
    assert _state()['device_ids'] == fake.DEVICE


def test_pressing_test_twice_does_not_mail_twice(net):
    for _ in range(2):
        with pytest.raises(cr.RenaultCodeNeeded):
            _conn().verify_credentials()
    assert net['mails'] == 1


def test_the_background_sync_never_mails_a_code(net):
    with pytest.raises(RuntimeError) as e:
        _conn().get_status()
    assert not isinstance(e.value, cr.RenaultCodeNeeded)
    assert net['mails'] == 0
    assert 'Test' in str(e.value) or 'Tester' in str(e.value) or 'test' in str(e.value)


def test_the_code_completes_the_sign_in_across_two_requests(net):
    with pytest.raises(cr.RenaultCodeNeeded):
        _conn().verify_credentials()
    # second web request: a NEW connector, a NEW renault-api session
    _conn().submit_code(' 123456 ')
    f = net['finalize'][-1]
    assert f['reg_token'] == 'reg-token-1' and f['remember'] is True
    assert f['device_ids'] == fake.DEVICE            # the same device the code was mailed for
    st = _state()
    assert 'pending' not in st and st['device_ids'] == fake.DEVICE


def test_afterwards_the_sync_signs_in_without_a_code(net):
    with pytest.raises(cr.RenaultCodeNeeded):
        _conn().verify_credentials()
    _conn().submit_code(fake.CODE)
    mails = net['mails']
    s = _conn().get_status()
    assert s.soc_percent == 63
    assert net['logins'][-1]['device_ids'] == fake.DEVICE
    assert net['mails'] == mails


def test_a_wrong_code_keeps_the_challenge_for_a_second_try(net):
    with pytest.raises(cr.RenaultCodeNeeded):
        _conn().verify_credentials()
    with pytest.raises(RuntimeError) as e:
        _conn().submit_code('999999')
    assert 'renault.code_wrong' not in str(e.value) and str(e.value)
    assert _state().get('pending')
    _conn().submit_code(fake.CODE)
    assert net['mails'] == 1


def test_an_old_challenge_is_not_completed(net, monkeypatch):
    with pytest.raises(cr.RenaultCodeNeeded):
        _conn().verify_credentials()
    st = _state(); st['pending']['ts'] = time.time() - cr.PENDING_TTL - 1
    cr._save_state(USER, st)
    with pytest.raises(RuntimeError) as e:
        _conn().submit_code(fake.CODE)
    assert not net['finalize']
    assert str(e.value)


def test_the_state_file_is_private_and_not_named_after_the_email(net):
    with pytest.raises(cr.RenaultCodeNeeded):
        _conn().verify_credentials()
    files = os.listdir(cr.STATE_DIR)
    assert len(files) == 1 and 'example' not in files[0] and '@' not in files[0]
    mode = stat.S_IMODE(os.stat(os.path.join(cr.STATE_DIR, files[0])).st_mode)
    assert mode == 0o600
    raw = open(os.path.join(cr.STATE_DIR, files[0])).read()
    assert 'pw' not in json.loads(raw).get('pending', {}).values()   # the password is never stored


def test_code_texts_exist_in_every_language():
    keys = ('renault.code_sent', 'renault.code_title', 'renault.code_intro', 'renault.code_label',
            'renault.code_submit', 'renault.code_hint', 'renault.code_resend', 'renault.code_back',
            'renault.code_wrong', 'renault.code_expired', 'renault.code_format', 'renault.code_ok',
            'renault.err_2fa', 'wiz.enter_code', 'wiz.continue_anyway')
    for lang in LANGS:
        d = json.loads((ROOT / 'translations' / f'{lang}.json').read_text(encoding='utf-8'))
        for k in keys:
            assert d.get(k), f'{k} missing in {lang}'
        assert '{email}' in d['renault.code_sent'] and '{name}' in d['renault.code_ok']


# ── the web flow, in a subprocess with its own data folder ─────────────

_SCRIPT = r'''
import json, sys
sys.path.insert(0, %(root)r); sys.path.insert(0, %(root)r + "/tests")
import _renault_fake as fake
fake.install(setattr, tfa=True)
from app import create_app
from models.database import db, Vehicle, AppConfig
app = create_app()
app.config["TESTING"] = True
with app.app_context():
    v = Vehicle.query.order_by(Vehicle.id).first()
    v.name = "Megane"; v.api_brand = "renault"; v.api_username = "owner@example.invalid"
    v.api_password = "pw"; v.api_region = "fr_FR"; db.session.commit()
    AppConfig.set("app_language", "fr")
    vid = v.id
out = {}
c = app.test_client()
with c.session_transaction() as s:
    s["logged_in"] = True; s["user"] = "t"; s["authenticated"] = True
r = c.post("/vehicles/%%d/test" %% vid, data={"ajax": "1"})
out["ajax"] = r.get_json()
r = c.post("/vehicles/%%d/test" %% vid)
out["plain_status"], out["plain_location"] = r.status_code, r.headers.get("Location", "")
r = c.get("/vehicles/%%d/code" %% vid)
out["page_status"] = r.status_code
out["page_has_input"] = 'name="code"' in r.get_data(as_text=True)
out["page_fr"] = "Saisir le code de confirmation" in r.get_data(as_text=True)
r = c.post("/vehicles/%%d/code" %% vid, data={"code": "12"})
out["short_code_stays"] = r.status_code == 200 and "4" in r.get_data(as_text=True)
r = c.post("/vehicles/%%d/code" %% vid, data={"code": "999999"})
out["wrong_code_stays"] = r.status_code == 200
r = c.post("/vehicles/%%d/code" %% vid, data={"code": "123456"})
out["ok_status"], out["ok_location"] = r.status_code, r.headers.get("Location", "")
r = c.post("/vehicles/%%d/test" %% vid, data={"ajax": "1"})
out["after"] = r.get_json()
# Einrichtungsassistent: Code per ajax, Antwort als JSON (zweites Konto)
fake.install(setattr, tfa=True)
with app.app_context():
    v = Vehicle.query.get(vid); v.api_username = "second@example.invalid"; db.session.commit()
out["wiz_need"] = c.post("/vehicles/%%d/test" %% vid, data={"ajax": "1"}).get_json()
out["wiz_wrong"] = c.post("/vehicles/%%d/code" %% vid, data={"ajax": "1", "code": "999999"}).get_json()
out["wiz_ok"] = c.post("/vehicles/%%d/code" %% vid, data={"ajax": "1", "code": "123456"}).get_json()
out["wizard_html"] = c.get("/settings").get_data(as_text=True)
print(json.dumps(out))
'''


def test_the_web_flow_test_code_page_and_back():
    data_dir = tempfile.mkdtemp(prefix='evct-renault-')
    env = dict(os.environ, EV_DATA_DIR=data_dir, SECRET_KEY='renault-code-test')
    proc = subprocess.run([sys.executable, '-c', _SCRIPT % {'root': str(ROOT)}], cwd=str(ROOT),
                          env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-2500:]
    out = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith('{')][-1])
    a = out['ajax']
    assert a['ok'] is False and a['code_needed'] is True and a['code_url'].endswith('/code'), a
    assert out['plain_status'] == 302 and out['plain_location'].endswith('/code'), out
    assert out['page_status'] == 200 and out['page_has_input'] and out['page_fr'], out
    assert out['short_code_stays'] and out['wrong_code_stays'], out
    assert out['ok_status'] == 302 and out['ok_location'].endswith('/settings#sec-fleet'), out
    assert out['after']['ok'] is True, out['after']
    # the wizard takes the code in step 3, as JSON
    assert out['wiz_need'].get('code_needed') is True, out['wiz_need']
    assert out['wiz_wrong']['ok'] is False and out['wiz_wrong']['error'], out['wiz_wrong']
    assert out['wiz_ok']['ok'] is True and 'Megane' in out['wiz_ok']['message'], out['wiz_ok']
    html = out['wizard_html']
    assert 'id="cwCodeBox"' in html and 'id="cw_code"' in html and "CODE_BRANDS = ['renault', 'dacia']" in html
    # the code input must not be part of the vehicle form (no name attribute)
    import re as _re
    tag = _re.search(r'<input[^>]*id="cw_code"[^>]*>', html).group(0)
    assert 'name=' not in tag, tag
    # a placeholder region ("EU") no longer leaves Renault's list empty
    assert 'function waehleOption' in html and 'neu.value = waehleOption(f.options, eingabe.value)' in html


def test_the_image_installs_a_renault_api_that_knows_the_code():
    """The code page needs renault-api 0.6+; image and in-app installer agree."""
    req = (ROOT / 'requirements-vehicles.txt').read_text(encoding='utf-8')
    assert 'renault-api>=0.6.1' in req
    from services.vehicle.catalog import PACKAGES
    assert 'renault-api>=0.6.1' in PACKAGES['renault']
