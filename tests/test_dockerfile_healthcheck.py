# -*- coding: utf-8 -*-
"""The image must carry its own HEALTHCHECK.

Anyone running the published image without the bundled docker-compose.yml
(plain ``docker run``, Portainer, a NAS GUI) used to get a container with no
health status at all. The probe now lives in the Dockerfile and has to stay
in step with docker-compose.yml.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _dockerfile():
    return (ROOT / 'Dockerfile').read_text(encoding='utf-8')


def test_dockerfile_declares_healthcheck_on_api_health():
    text = _dockerfile()
    assert re.search(r'^HEALTHCHECK\b', text, re.M)
    assert '/api/health' in text.split('HEALTHCHECK', 1)[1]


def test_healthcheck_uses_python_not_curl():
    # python:3.12-slim ships neither curl nor wget.
    hc = _dockerfile().split('HEALTHCHECK', 1)[1].split('\nCMD ["python", "app.py"]')[0]
    assert '"python"' in hc and 'urllib.request' in hc
    assert 'curl' not in hc and 'wget' not in hc


def test_healthcheck_timings_match_compose():
    hc = _dockerfile().split('HEALTHCHECK', 1)[1].split('CMD [', 1)[0]
    compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
    for flag, key in (('interval', 'interval'), ('timeout', 'timeout'),
                      ('start-period', 'start_period'), ('retries', 'retries')):
        d = re.search(r'--%s=(\w+)' % flag, hc).group(1)
        c = re.search(r'^\s+%s: (\w+)' % key, compose, re.M).group(1)
        assert d == c, flag


def test_healthcheck_follows_app_port():
    assert "os.environ.get('APP_PORT', '7654')" in _dockerfile()
