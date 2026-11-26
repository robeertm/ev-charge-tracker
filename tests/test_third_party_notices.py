"""THIRD-PARTY-NOTICES.md must name every dependency and bundled library.

Offline: compares the package names in the requirement files and the
directories under static/vendor/ with the generated file. When this fails,
regenerate the file (see the docstring of scripts/drittlizenzen.py) and commit
it.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTICES = ROOT / "THIRD-PARTY-NOTICES.md"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _section(start: str, end: str) -> str:
    text = NOTICES.read_text(encoding="utf-8")
    return text.split(start, 1)[1].split(end, 1)[0]


def _listed_packages() -> set[str]:
    # Only the component list; the licence texts below it contain bullet lists too.
    section = _section("## Python packages", "## Licence texts")
    return {_norm(m) for m in re.findall(r"^- `([^`]+)` ", section, re.M)}


def _requirements() -> set[str]:
    out = set()
    for fname in ("requirements.txt", "requirements-vehicles.txt"):
        for line in (ROOT / fname).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                out.add(_norm(re.split(r"[<>=!~;\[ ]", line, maxsplit=1)[0]))
    return out


def test_every_requirement_is_listed():
    missing = sorted(_requirements() - _listed_packages())
    assert not missing, (
        f"THIRD-PARTY-NOTICES.md lacks {missing}; regenerate it with scripts/drittlizenzen.py"
    )


def test_every_vendored_library_is_listed_with_its_licence():
    section = _section("## Browser libraries", "## Container image")
    for d in sorted((ROOT / "static" / "vendor").iterdir()):
        if d.is_dir():
            assert f"`static/vendor/{d.name}/`" in section, f"{d.name} missing from the notices"
            assert any(
                f.name.upper().startswith(("LICENSE", "LICENCE", "MIT-LICEN"))
                for f in d.iterdir()
            ), f"{d.name} ships without its licence file"


def test_notices_carry_licence_texts():
    text = NOTICES.read_text(encoding="utf-8")
    assert "## Licence texts" in text
    assert "Permission is hereby granted" in text  # MIT texts are kept with their notices


def test_licence_and_notices_ship_in_the_image():
    docker_ignore = (ROOT / ".dockerignore").read_text().splitlines()
    for name in ("LICENSE", "THIRD-PARTY-NOTICES.md", "static", "static/vendor"):
        assert name not in [line.strip() for line in docker_ignore]
    assert "THIRD-PARTY-NOTICES.md" in (ROOT / "Dockerfile").read_text()
