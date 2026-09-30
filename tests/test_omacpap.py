"""End-to-end tests against the local mock of ResMed's endpoints.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="omacpap-test-"))
os.environ.update(OMACPAP_DATA_DIR=str(TMP / "data"), OMACPAP_STATE_DIR=str(TMP / "state"),
                  OMACPAP_CONFIG_DIR=str(TMP / "config"))
os.environ.pop("OMACPAP_MYAIR_PASSWORD", None)

from tests import mock_myair  # noqa: E402
from tests.edfwriter import make_card  # noqa: E402

MOCK, MOCK_URL = mock_myair.start()
os.environ["OMACPAP_MYAIR_MOCK"] = MOCK_URL

from omacpap import analysis, db, edf, myair, report, sdcard, secrets_store, server, sync, theme  # noqa: E402

EXPECTED_NIGHTS = sum(
    1 for i in range((dt.date.today() - dt.timedelta(days=1) - mock_myair.HISTORY_START).days + 1)
    if mock_myair.night(mock_myair.HISTORY_START + dt.timedelta(days=i)))


def client(password="demo") -> myair.MyAirClient:
    return myair.MyAirClient("demo@example.com", password, myair.get_region("NA"))


class T01_Auth(unittest.TestCase):
    def test_login_success_gets_tokens_and_device_cookie(self):
        c = client()
        self.assertEqual(c.login(), "SUCCESS")
        self.assertTrue(c.token_valid)
        self.assertEqual(c.state.device_token, "device-token-1")
        self.assertEqual(c._country(), "US")

    def test_bad_password_is_actionable(self):
        with self.assertRaises(myair.AuthError) as cm:
            client("wrong").login()
        self.assertIn("rejected", str(cm.exception))

    def test_mfa_flow(self):
        c = client("mfa")
        with self.assertRaises(myair.MFARequired):
            c.login()
        with self.assertRaises(myair.AuthError):
            c.verify_mfa("000000")
        self.assertEqual(c.verify_mfa("123456"), "SUCCESS")
        self.assertTrue(c.token_valid)

    def test_session_roundtrip(self):
        c = client()
        c.login()
        s = myair.SessionState.from_json(c.state.to_json())
        self.assertEqual(s.access_token, c.state.access_token)


class T02_Sync(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        secrets_store.save_config({**secrets_store.load_config(), "username": "demo@example.com", "region": "NA"})

    def test_a_full_backfill_then_incremental(self):
        c = client()
        progress: list[str] = []
        res = sync.run_sync(client=c, progress=progress.append)
        self.assertTrue(res["full"])
        self.assertEqual(res["new"], EXPECTED_NIGHTS)
        # stops after 3 empty months before HISTORY_START
        self.assertTrue(any("Feb 2023" in p for p in progress))
        self.assertFalse(any("Oct 2022" in p for p in progress))
        with db.session() as con:
            n = con.execute("SELECT COUNT(*) c, MIN(date) a FROM nights").fetchone()
            dev = con.execute("SELECT * FROM devices").fetchone()
        self.assertEqual(n["c"], EXPECTED_NIGHTS)
        self.assertEqual(n["a"], "2023-03-10")
        self.assertEqual(dev["mask_code"], "AirFit F20")

        res2 = sync.run_sync(client=c)
        self.assertFalse(res2["full"])
        self.assertEqual((res2["new"], res2["updated"]), (0, 0))
        self.assertLessEqual(res2["fetched"], 45)

    def test_b_probe_discovers_hidden_field(self):
        c = client()
        found = c.probe_fields(["ahiEvents", "definitelyNotAField", "pressure95"])
        self.assertEqual(found, ["ahiEvents"])
        recs = c.sleep_records(dt.date.today() - dt.timedelta(days=5), dt.date.today(), found)
        self.assertIn("ahiEvents", recs[0])
        with db.session() as con:
            db.upsert_myair_records(con, recs)
            row = db.nights(con, recs[0]["startDate"], recs[0]["startDate"])[0]
        self.assertIn("ahiEvents", row["extra"])


class T03_Analysis(unittest.TestCase):
    def test_summary_numbers_are_sane(self):
        with db.session() as con:
            rows = db.nights(con)
        s = analysis.build_summary(rows, 4.0, 24.0)
        w = s["windows"]["30d"]
        self.assertEqual(w["days"], 30)
        self.assertTrue(0 <= w["compliance_pct"] <= 100)
        self.assertTrue(0 < w["ahi_weighted"] < 15)
        self.assertGreater(s["streak_longest"], 0)
        self.assertEqual(len(s["weekday"]), 7)
        self.assertTrue(s["insights"])
        filled = analysis.calendar_fill(rows)
        self.assertEqual(len(filled), (dt.date.fromisoformat(rows[-1]["date"]) - dt.date(2023, 3, 10)).days + 1)

    def test_weighted_ahi(self):
        nights = [{"date": "2025-01-01", "usage_min": 60, "ahi": 10},
                  {"date": "2025-01-02", "usage_min": 540, "ahi": 1}]
        self.assertAlmostEqual(analysis._weighted_ahi(nights), 1.9)

    def test_report_renders(self):
        html = report.render(days=30)
        self.assertIn("CPAP therapy report", html)
        self.assertIn("<svg", html)
        self.assertIn("AirSense 10", html)


class T04_EDF_and_SD(unittest.TestCase):
    def test_str_and_events_import(self):
        card = make_card(TMP / "card", nights=12)
        days = sdcard.parse_str(card / "STR.edf")
        self.assertEqual(len(days), 12)
        d0 = days[0]
        self.assertAlmostEqual(d0["usage_min"], 420, delta=0.1)
        self.assertAlmostEqual(d0["leak95_lpm"], 12.0, delta=0.05)  # 0.2 L/s → 12 L/min
        self.assertAlmostEqual(d0["press95"], 11.8, delta=0.01)
        sess = json.loads(d0["sessions"])
        self.assertTrue(sess[0][0].endswith("22:30"))
        self.assertEqual(json.loads(d0["settings"])["S.AS.MaxPress"], 15.0)
        res = sdcard.import_card(card)
        self.assertEqual(res["events"], 9)
        with db.session() as con:
            types = {r["type"] for r in con.execute("SELECT type FROM sd_events")}
            merged = db.nights(con)[-1]
        self.assertEqual(types, {"Obstructive Apnea", "Hypopnea", "Central Apnea"})
        self.assertIsNotNone(merged["oai"])

    def test_growing_file_is_tolerated(self):
        card = make_card(TMP / "card2", nights=5)
        raw = (card / "STR.edf").read_bytes()
        p = TMP / "truncated.edf"
        p.write_bytes(raw[:-10])  # last record cut short
        f = edf.read_edf(p)
        self.assertEqual(f.n_records, 4)


class T05_Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.serve(0)
        server.Handler.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), r.headers

    def post(self, path, body, headers=None):
        h = {"Content-Type": "application/json", "X-OmaCPAP": "1", **(headers or {})}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_pages_and_api(self):
        code, body, h = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"OmaCPAP", body)
        self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
        st = json.loads(self.get("/api/state")[1])
        self.assertTrue(st["configured"])
        self.assertEqual(st["username"], "de••@example.com")
        self.assertGreater(len(json.loads(self.get("/api/nights")[1])["nights"]), 900)
        self.assertIn(b"--accent", self.get("/theme.css")[1])
        self.assertTrue(self.get("/api/export.csv")[1].startswith(b"date,usage_min"))
        self.assertEqual(self.get("/static/vendor/uPlot.iife.min.js")[0], 200)

    def test_guards(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/api/state", {"Host": "evil.example:80"})
        self.assertEqual(cm.exception.code, 403)
        code, _ = self.post("/api/note", {"date": "2025-01-01", "text": "x"}, {"X-OmaCPAP": "0"})
        self.assertEqual(code, 403)
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/static/../server.py")

    def test_note_and_settings(self):
        with db.session() as con:
            d = con.execute("SELECT MAX(date) d FROM nights").fetchone()["d"]
        self.assertEqual(self.post("/api/note", {"date": d, "text": "new cushion"})[0], 200)
        with db.session() as con:
            self.assertEqual(db.nights(con, d, d)[0]["note"], "new cushion")
        self.post("/api/settings", {"usage_goal_hours": 5})
        self.assertEqual(secrets_store.load_config()["usage_goal_hours"], 5.0)
        self.post("/api/settings", {"usage_goal_hours": 4})

    def test_login_mfa_via_api(self):
        code, r = self.post("/api/login", {"username": "demo@example.com", "password": "mfa", "region": "NA"})
        self.assertEqual((code, r["status"]), (200, "MFA_REQUIRED"))
        code, r = self.post("/api/mfa", {"code": "123456"})
        self.assertEqual((code, r["status"]), (200, "SUCCESS"))
        for _ in range(100):
            if not server.JOB.running:
                break
            threading.Event().wait(0.1)
        self.assertIsNone(server.JOB.error)


class T06_Theme(unittest.TestCase):
    def test_reads_colors_toml(self):
        d = TMP / "theme"
        d.mkdir(exist_ok=True)
        (d / "colors.toml").write_text('background = "#101010"\nforeground = "#eeeeee"\naccent = "#ff8800"\ncolor2 = "#00ff00"\n')
        out = theme.css(d)
        self.assertIn("--accent: #ff8800", out)
        self.assertIn("--good: #00ff00", out)
        self.assertIn("color-scheme: dark", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
