"""HTTPS / TLS support for the Flask web server.

Adapted from shelly-energy-analyzer's ssl_utils.py. Three modes:

- ``off``     — plain HTTP, no certificate handling
- ``auto``    — self-signed certificate, generated once and stored on disk
- ``custom``  — user-provided cert + key paths (e.g. Let's Encrypt output)

Self-signed mode generates the cert via the ``cryptography`` Python library
if installed, otherwise falls back to the ``openssl`` CLI. Cert is valid for
10 years and lives in ``data/ssl/server.{crt,key}``.

The Flask development server's ``ssl_context`` parameter is used directly —
no nginx, no gunicorn. For a self-hosted single-user app this is enough.
"""
from __future__ import annotations

import logging
import socket
import ssl
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def _ensure_self_signed_cert(cert_dir: Path, common_name: str = 'EV Charge Tracker') -> Tuple[Path, Path]:
    """Generate a self-signed TLS certificate if none exists in `cert_dir`.

    Returns ``(cert_path, key_path)``.  Tries the ``cryptography`` library
    first; falls back to the ``openssl`` CLI.
    """
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / 'server.crt'
    key_path = cert_dir / 'server.key'
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path

    logger.info('Generating self-signed TLS certificate for HTTPS …')

    # Try 1: pure-Python via cryptography library (works on all platforms)
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime as _dt

        now = _dt.datetime.now(_dt.timezone.utc)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        # SAN entries — include localhost + best-effort LAN IP so the cert
        # is valid for both desktop and smartphone clients on the same network.
        san_entries = [
            x509.DNSName('localhost'),
            x509.IPAddress(_parse_ip('127.0.0.1')),
        ]
        try:
            lan_ip = _local_ip_guess()
            if lan_ip and lan_ip != '127.0.0.1':
                san_entries.append(x509.IPAddress(_parse_ip(lan_ip)))
        except Exception:
            pass

        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
            .sign(key, hashes.SHA256())
        )
        key_path.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        logger.info('TLS certificate created (cryptography): %s', cert_path)
        return cert_path, key_path
    except ImportError:
        logger.debug('cryptography library not available, trying openssl CLI')
    except Exception as e:
        logger.debug('cryptography cert generation failed: %s', e)

    # Try 2: openssl CLI
    import shutil
    import subprocess

    if not shutil.which('openssl'):
        raise RuntimeError(
            "Cannot generate TLS certificate: neither 'cryptography' library "
            "nor 'openssl' CLI found. Install one or provide custom cert files."
        )

    # Build SAN config for openssl
    san_lines = ['DNS:localhost', 'IP:127.0.0.1']
    try:
        lan_ip = _local_ip_guess()
        if lan_ip and lan_ip != '127.0.0.1':
            san_lines.append(f'IP:{lan_ip}')
    except Exception:
        pass

    config_path = cert_dir / 'openssl.cnf'
    config_path.write_text(
        '[req]\n'
        'distinguished_name = dn\n'
        'x509_extensions = v3_req\n'
        'prompt = no\n'
        '[dn]\n'
        f'CN = {common_name}\n'
        '[v3_req]\n'
        f'subjectAltName = {",".join(san_lines)}\n'
    )
    try:
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
            '-keyout', str(key_path),
            '-out', str(cert_path),
            '-days', '3650',
            '-nodes',
            '-config', str(config_path),
            '-extensions', 'v3_req',
        ], check=True, capture_output=True, timeout=30)
        logger.info('TLS certificate created (openssl CLI): %s', cert_path)
    finally:
        try:
            config_path.unlink()
        except Exception:
            pass
    return cert_path, key_path


def _parse_ip(ip_str: str):
    """Convert a string like '192.168.1.5' to an ipaddress.IPv4Address."""
    import ipaddress
    return ipaddress.ip_address(ip_str)


def _local_ip_guess() -> str:
    """Best-effort LAN IP discovery (no external calls)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def build_ssl_context(mode: str, cert_dir: Path,
                      custom_cert: str = '', custom_key: str = '') -> Optional[ssl.SSLContext]:
    """Return a configured ``ssl.SSLContext`` for ``mode`` or ``None`` for HTTP.

    ``mode`` is one of: ``'off'``, ``'auto'``, ``'custom'``.
    """
    mode = (mode or 'off').lower()
    if mode == 'off':
        return None

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

    if mode == 'custom':
        if not custom_cert or not custom_key:
            raise ValueError('Custom HTTPS mode requires cert and key paths')
        if not Path(custom_cert).exists() or not Path(custom_key).exists():
            raise FileNotFoundError(f'Custom cert/key not found: {custom_cert}, {custom_key}')
        ctx.load_cert_chain(custom_cert, custom_key)
        logger.info('HTTPS enabled (custom certificate)')
        return ctx

    # auto mode → self-signed
    cert_path, key_path = _ensure_self_signed_cert(cert_dir)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    logger.info('HTTPS enabled (self-signed certificate at %s)', cert_path)
    return ctx


def get_cert_info(cert_path: Path) -> Optional[dict]:
    """Return cert metadata for the Settings UI: subject, dates, fingerprint.
    Tries the ``cryptography`` library first, falls back to ``openssl`` CLI."""
    if not cert_path.exists():
        return None

    # Try 1: cryptography library
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        fp = cert.fingerprint(hashes.SHA256()).hex()
        fp_pretty = ':'.join(fp[i:i + 2] for i in range(0, len(fp), 2)).upper()
        # cryptography >=42 deprecates not_valid_before/after in favor of
        # the _utc variants; support both so we work across versions.
        nb = getattr(cert, 'not_valid_before_utc', None) or cert.not_valid_before
        na = getattr(cert, 'not_valid_after_utc', None) or cert.not_valid_after
        return {
            'subject': cert.subject.rfc4514_string(),
            'not_before': nb.isoformat(),
            'not_after': na.isoformat(),
            'fingerprint_sha256': fp_pretty,
        }
    except ImportError:
        pass
    except Exception as e:
        logger.warning('cryptography parse failed: %s', e)

    # Try 2: openssl CLI
    import shutil as _shutil
    import subprocess
    if not _shutil.which('openssl'):
        return None
    try:
        out = subprocess.run(
            ['openssl', 'x509', '-in', str(cert_path), '-noout',
             '-subject', '-startdate', '-enddate', '-fingerprint', '-sha256'],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout
        info = {'subject': '', 'not_before': '', 'not_after': '', 'fingerprint_sha256': ''}
        for line in out.splitlines():
            if line.startswith('subject='):
                info['subject'] = line.split('=', 1)[1].strip()
            elif line.startswith('notBefore='):
                info['not_before'] = line.split('=', 1)[1].strip()
            elif line.startswith('notAfter='):
                info['not_after'] = line.split('=', 1)[1].strip()
            elif 'Fingerprint=' in line:
                info['fingerprint_sha256'] = line.split('=', 1)[1].strip()
        return info
    except Exception as e:
        logger.warning('openssl parse failed: %s', e)
        return None
