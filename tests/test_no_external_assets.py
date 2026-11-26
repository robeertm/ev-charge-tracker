"""The web interface must not load scripts or styles from another host.

Every browser library is bundled under static/vendor/, so a page works with no
connection to a CDN and no third party learns who opens it. The only thing the
browser still fetches from outside is map tiles (a JavaScript call, documented
in the README under "Network connections").

This test fails when a template references a script, stylesheet, font or
other asset by an absolute http(s) or protocol-relative URL. Plain links the
user clicks (`<a href>`) are not assets and are not checked.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# <script src=...>, <link ... href=...>, <img|source|iframe|audio|video ... src=...>
TAG = re.compile(r"<(script|link|img|source|iframe|audio|video|embed|object)\b[^>]*>", re.I | re.S)
URL_ATTR = re.compile(r"""\b(?:src|href|data)\s*=\s*["']\s*((?:https?:)?//[^"']+)""", re.I)
CSS_URL = re.compile(r"""url\(\s*["']?\s*((?:https?:)?//[^)"']+)""", re.I)


def test_templates_load_no_external_assets():
    offenders = []
    for path in sorted((ROOT / "templates").glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for tag in TAG.finditer(text):
            for url in URL_ATTR.findall(tag.group(0)):
                offenders.append(f"{path.name}: <{tag.group(1)}> {url}")
        for url in CSS_URL.findall(text):
            offenders.append(f"{path.name}: css url({url})")
    assert not offenders, "external asset host in templates:\n" + "\n".join(offenders)


def test_static_css_loads_no_external_assets():
    offenders = []
    for path in sorted((ROOT / "static").rglob("*.css")):
        for url in CSS_URL.findall(path.read_text(encoding="utf-8", errors="replace")):
            offenders.append(f"{path.relative_to(ROOT)}: {url}")
    assert not offenders, "external host in stylesheets:\n" + "\n".join(offenders)


def test_vendored_libraries_are_in_place():
    base = ROOT / "static" / "vendor"
    for rel in (
        "bootstrap-5.3.3/bootstrap.min.css",
        "bootstrap-5.3.3/bootstrap.bundle.min.js",
        "bootstrap-icons-1.11.3/bootstrap-icons.min.css",
        "bootstrap-icons-1.11.3/fonts/bootstrap-icons.woff2",
        "leaflet-1.9.4/leaflet.css",
        "leaflet-1.9.4/leaflet.js",
        "leaflet-1.9.4/images/marker-icon.png",
        "leaflet.markercluster-1.5.3/leaflet.markercluster.js",
        "chartjs-4.4.6/chart.umd.js",
        "chartjs-adapter-date-fns-3.0.0/chartjs-adapter-date-fns.bundle.min.js",
    ):
        assert (base / rel).is_file() and (base / rel).stat().st_size > 500, rel


def test_every_vendored_reference_in_templates_exists():
    missing = []
    for path in sorted((ROOT / "templates").glob("*.html")):
        for rel in re.findall(r"filename='(vendor/[^']+)'", path.read_text(encoding="utf-8")):
            if not (ROOT / "static" / rel).is_file():
                missing.append(f"{path.name}: {rel}")
    assert not missing, missing
