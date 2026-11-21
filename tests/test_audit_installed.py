"""scripts/audit_installed.py: findings count unless covered by a valid exception."""
import datetime
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import audit_installed as A  # noqa: E402

REPORT = {"dependencies": [{"name": "x", "version": "1", "vulns": [{"id": "PYSEC-1", "aliases": ["CVE-1"]}]},
                           {"name": "y", "version": "2", "vulns": []}]}


def _exc(items):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"exceptions": items}, f)
    f.close()
    return f.name


def test_all():
    today = datetime.date(2026, 11, 21)
    assert A.findings(REPORT, set()) == ["x 1: PYSEC-1"]
    good = {"id": "CVE-1", "reason": "not reachable in this app", "until": "2027-03-01"}
    assert A.findings(REPORT, A.load_exceptions(_exc([good]), today)) == []
    for bad in ({**good, "until": "2026-01-01"}, {**good, "reason": "x"}, {**good, "until": "2030-01-01"}):
        assert A.findings(REPORT, A.load_exceptions(_exc([bad]), today)) == ["x 1: PYSEC-1"]
    assert A.load_exceptions("/nonexistent", today) == set()
    print("ok")


if __name__ == "__main__":
    test_all()
