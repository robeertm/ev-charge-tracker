# -*- coding: utf-8 -*-
"""An opaque provider reject must be classified by exception CLASS, not only
by the words in it — and a rejected sign-in must never be retried.

The bug: Kia answers a dead credential with its own ``retMsg`` and the SDK
raises ``AuthenticationError("Received unexpected statusCode")``
(``ApiImplType1._check_response_for_errors``). That string matches none of
``_AUTH_REJECT_MARKERS``, so the message-only classifier returned '' — and the
caller reads '' as "just a transient blip" and retries. Live consequence on a
real install: every ten minutes for close to six hours the log showed
``Token refresh failed, retrying once: Received unexpected statusCode`` and the
user was shown that raw English text, with nothing saying what to do.

The second half is the *reason* the credential died: the password field held a
48-character legacy refresh token. Once it stopped being exchangeable the SDK
fell back to a full login and sent that token as the password, which can never
work. That case needs its own wording — a password checklist sends the owner
chasing a password they never stored.

Deliberately no SDK here: the package is absent on native Python 3.11 installs
and in this environment, so the classifier is built to read the class off the
exception instead of importing the types. The stubs below carry the SDK's
module name, which is exactly what the real exceptions look like.

Run with:  python3 -m pytest tests/test_kia_anmeldung_nach_typ_einordnen.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import services.vehicle.connector_hyundai_kia as K                      # noqa: E402

# A refresh token as the install actually stored it: 48 characters, no '@',
# no spaces. The value itself is meaningless, only its shape matters.
TOKEN = 'k' * 48
PASSWORT = 'ein-echtes-passwort'


def _sdk_exc(name, text):
    """An exception that looks to the classifier exactly like the SDK's."""
    cls = type(name, (Exception,), {})
    cls.__module__ = 'hyundai_kia_connect_api.exceptions'
    return cls(text)


PROVIDER_REJECT = 'Received unexpected statusCode'


# ── the classifier ────────────────────────────────────────────────────

def test_der_reject_wird_am_typ_erkannt():
    err = _sdk_exc('AuthenticationError', PROVIDER_REJECT)
    assert K._sdk_error_name(err) == 'AuthenticationError'
    assert K._is_auth_rejection(err) is True


def test_eine_fremde_ausnahme_wird_nicht_zum_sdk_typ():
    assert K._sdk_error_name(ValueError('whatever')) == ''


def test_der_woertliche_text_reicht_auch_ohne_den_typ():
    # An older SDK may wrap the same provider answer in another class.
    assert K._is_auth_rejection(Exception(PROVIDER_REJECT)) is True


def test_eine_abgelehnte_anmeldung_ist_nie_ohne_meldung():
    """The regression itself: '' meant "retry", and that was the defect."""
    for cred in (TOKEN, PASSWORT, ''):
        err = _sdk_exc('AuthenticationError', PROVIDER_REJECT)
        assert K._friendly_auth_message(err, cred) != '', cred


# ── which wording ─────────────────────────────────────────────────────

def test_abgelaufener_token_bekommt_seinen_eigenen_hinweis():
    msg = K._friendly_auth_message(
        _sdk_exc('AuthenticationError', PROVIDER_REJECT), TOKEN)
    assert 'Token' in msg and 'abgelaufen' in msg
    assert 'Konto-Passwort' in msg          # says what to do instead
    assert 'Mit Apple anmelden' not in msg  # not the password checklist


def test_ein_passwort_bekommt_die_checkliste_und_nicht_den_token_text():
    msg = K._friendly_auth_message(
        _sdk_exc('AuthenticationError', PROVIDER_REJECT), PASSWORT)
    assert 'Mit Apple anmelden' in msg
    assert 'abgelaufen' not in msg


def test_tageskontingent_sagt_es_und_raet_vom_nachschieben_ab():
    msg = K._friendly_auth_message(
        _sdk_exc('RateLimitingError', 'Exceeds number of requests'), TOKEN)
    assert msg and 'Tageskontingent' in msg
    # Not a credential problem — so it must not be dressed up as one.
    assert 'Passwort' not in msg.replace('Konto- oder Passwortproblem', '')


def test_zustimmung_wird_auch_am_typ_erkannt():
    msg = K._friendly_auth_message(
        _sdk_exc('ConsentRequiredError', 'nothing quotable here'), PASSWORT)
    assert 'Zustimmung' in msg


# ── the retry decision ────────────────────────────────────────────────

def test_eine_voruebergehende_stoerung_bleibt_wiederholbar():
    """A network blip must keep returning '' or the retry path is gone."""
    for err in (ConnectionError('temporary failure in name resolution'),
                Exception('Connection reset by peer'),
                TimeoutError('read timed out')):
        assert K._friendly_auth_message(err, TOKEN) == '', err


class _Zaehler:
    """Stands in for the SDK's VehicleManager and counts sign-in attempts."""

    def __init__(self, fehler):
        self.fehler = fehler
        self.versuche = 0
        self.password = TOKEN  # _get_manager compares this against the cred

    def check_and_refresh_token(self):
        self.versuche += 1
        raise self.fehler


def _verbinder(fehler):
    v = K.KiaConnector({'username': 'wer@example.invalid', 'password': TOKEN,
                        'pin': '', 'region': 'EU'})
    zaehler = _Zaehler(fehler)
    v._get_manager = lambda: zaehler          # no SDK, no network
    v._check_sdk_supports_credential = lambda: None
    return v, zaehler


def test_eine_ablehnung_wird_genau_einmal_versucht():
    import pytest
    v, z = _verbinder(_sdk_exc('AuthenticationError', PROVIDER_REJECT))
    with pytest.raises(RuntimeError) as ex:
        v._ensure_auth()
    assert z.versuche == 1, 'eine Ablehnung darf nicht wiederholt werden'
    assert 'abgelaufen' in str(ex.value)


def test_eine_voruebergehende_stoerung_wird_ein_zweites_mal_versucht():
    import pytest
    v, z = _verbinder(ConnectionError('temporary failure in name resolution'))
    with pytest.raises(Exception):
        v._ensure_auth()
    assert z.versuche == 2, 'ein Aussetzer darf genau einmal wiederholt werden'
