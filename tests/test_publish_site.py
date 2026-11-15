import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "engine"))
"""Run from project root: python3 -m unittest discover -s tests -q"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from equity_lens import make_demo, EvidenceError
from publish_site import build, validate_live, stable_json


def to_live(s, index):
    import copy
    p = copy.deepcopy(s)
    p["mode"] = "LIVE_BASE_MAINNET_READ_ONLY"
    p["captured_at_utc"] = "2026-11-15T16:40:45+00:00"
    p["block"]["block_timestamp_utc"] = "2026-11-15T16:40:00+00:00"
    for a in p["assets"]:
        a["notes"] = []
        a["code_bytes"] = 10
    return p


class PublicSiteTests(unittest.TestCase):
    def records(self, directory):
        a, b = make_demo()
        a, b = to_live(a, 1), to_live(b, 2)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "base_equity_20261115T001Z_b100.json").write_text(json.dumps(a), encoding="utf-8")
        (directory / "base_equity_20261115T002Z_b200.json").write_text(json.dumps(b), encoding="utf-8")
        return a, b

    def test_valid_build(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.records(root / "snapshots")
            result = build(root / "snapshots", root / "docs")
            self.assertEqual(result["blocks"], [100, 200])
            self.assertTrue((root / "docs" / "index.html").is_file())
            self.assertFalse(result["internet_upload"])
            page = (root / "docs" / "index.html").read_text()
            self.assertIn("#100", page)
            self.assertIn("#200", page)
            self.assertIn("READ ONLY", page)
            m = json.loads((root / "docs" / "data" / "manifest.json").read_text())
            for fn, digest in m["files_sha256"].items():
                self.assertEqual(hashlib.sha256((root / "docs" / "data" / fn).read_bytes()).hexdigest(), digest)

    def test_no_live_data(self):
        a, _ = make_demo()
        with self.assertRaisesRegex(EvidenceError, "REFUSES_SYNTHETIC"):
            validate_live(a)

    def test_insufficient_data(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(EvidenceError, "TWO_LIVE"):
                build(Path(td), Path(td) / "docs")

    def test_missing_status_fails_closed(self):
        a, _ = make_demo()
        a = to_live(a, 1)
        a["assets"][0]["status"] = "NOT_VERIFIED"
        with self.assertRaisesRegex(EvidenceError, "INCOMPLETE"):
            validate_live(a)

    def test_invalid_supply_fails(self):
        a, _ = make_demo()
        a = to_live(a, 1)
        a["assets"][0]["total_supply_raw"] = "123n"
        with self.assertRaisesRegex(EvidenceError, "supply"):
            validate_live(a)

    def test_inconsistent_units_fails(self):
        a, _ = make_demo()
        a = to_live(a, 1)
        a["assets"][0]["token_units_reported"] = "999"
        with self.assertRaisesRegex(EvidenceError, "mismatch"):
            validate_live(a)

    def test_no_private_custom_fields_exported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a, b = self.records(root / "snapshots")
            a["my_private_secret"] = "DEMO_ONLY_SHOULD_NOT_APPEAR"
            (root / "snapshots" / "base_equity_20261115T001Z_b100.json").write_text(json.dumps(a))
            build(root / "snapshots", root / "docs")
            contents = "".join(x.read_text() for x in (root / "docs").rglob("*") if x.is_file())
            self.assertNotIn("DEMO_ONLY_SHOULD_NOT_APPEAR", contents)

    def test_incorrect_source_fails(self):
        a, _ = make_demo()
        a = to_live(a, 1)
        a["source_url"] = "https://example.com/other"
        with self.assertRaisesRegex(EvidenceError, "reference"):
            validate_live(a)

    def test_same_block_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            a,b=self.records(root/"snapshots")
            b["block"]["number"] = 100
            (root/"snapshots"/"base_equity_20261115T002Z_b200.json").write_text(json.dumps(b))
            with self.assertRaisesRegex(EvidenceError, "DIFFERENT_BLOCKS"):
                build(root/"snapshots", root/"docs")

    def test_block_timestamp_after_capture_fails(self):
        a,_ = make_demo()
        a = to_live(a, 1)
        a["block"]["block_timestamp_utc"] = "2026-11-15T16:41:01+00:00"
        with self.assertRaisesRegex(EvidenceError, "time"):
            validate_live(a)

if __name__ == "__main__":
    unittest.main()
