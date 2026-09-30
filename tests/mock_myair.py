"""A local stand-in for ResMed's Okta + AppSync endpoints, used by the tests
(and handy for UI work without touching a real account).

    python -m tests.mock_myair 9999
    OMACPAP_MYAIR_MOCK=http://127.0.0.1:9999 omacpap login --username demo@example.com   # password: demo

Passwords: "demo" -> direct success, "mfa" -> email MFA (code 123456), anything else -> 401.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import random
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HISTORY_START = dt.date(2023, 3, 10)
KNOWN = {"startDate", "totalUsage", "sleepScore", "usageScore", "ahiScore", "maskScore", "leakScore",
         "ahi", "maskPairCount", "leakPercentile", "sleepRecordPatientId", "__typename"}
HIDDEN_EXTRA = {"ahiEvents"}  # pretend ResMed exposes one undocumented field


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")


def fake_jwt(claims: dict) -> str:
    return f"{_b64({'alg': 'none'})}.{_b64(claims)}.sig"


def night(d: dt.date) -> dict | None:
    rnd = random.Random(d.toordinal())
    if rnd.random() < 0.06:
        return None  # skipped night
    weekend = d.weekday() >= 4
    usage = max(0, int(rnd.gauss(400 if not weekend else 350, 70)))
    if rnd.random() < 0.08:
        usage = rnd.randint(40, 230)
    # slow improvement over time, like a real mask refit story
    months = (d - HISTORY_START).days / 30
    ahi = max(0.2, rnd.gauss(4.2 - min(months, 18) * 0.15, 1.1))
    if rnd.random() < 0.05:
        ahi += rnd.uniform(3, 7)
    leak = max(0.0, rnd.gauss(8 if months > 6 else 16, 5))
    pairs = max(1, int(rnd.gauss(1.8, 1)))
    usage_score = min(70, round(usage / 60 * 10))
    leak_score = 20 if leak < 12 else 15 if leak < 18 else 10 if leak < 24 else 5
    ahi_score = 5 if ahi < 5 else 3 if ahi < 10 else 1
    mask_score = 5 if pairs <= 1 else 4 if pairs <= 2 else 3 if pairs <= 3 else 1
    return {
        "startDate": d.isoformat(), "totalUsage": usage, "ahi": round(ahi, 1), "leakPercentile": round(leak, 1),
        "maskPairCount": pairs, "usageScore": usage_score, "leakScore": leak_score, "ahiScore": ahi_score,
        "maskScore": mask_score, "sleepScore": usage_score + leak_score + ahi_score + mask_score,
        "sleepRecordPatientId": "p-123", "ahiEvents": int(ahi * usage / 60), "__typename": "SleepRecord",
    }


class Mock(BaseHTTPRequestHandler):
    stats: dict = {"graphql": 0, "authn": 0}

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body=None, headers=None):
        raw = json.dumps(body or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        ctype = self.headers.get("Content-Type", "")
        if "json" in ctype:
            return json.loads(raw or b"{}")
        return {k: v[-1] for k, v in urllib.parse.parse_qs(raw.decode()).items()}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path.endswith("/v1/authorize"):
            if "sessionToken" not in q:
                return self._send(200, {}, {"Set-Cookie": "DT=device-token-1; Path=/; HttpOnly"})
            if q["sessionToken"] != "sess-ok":
                return self._send(302, {}, {"Location": f"{q['redirect_uri']}#error=login_required"})
            loc = f"{q['redirect_uri']}#code=authcode-1&state={q.get('state', '')}"
            return self._send(302, {}, {"Location": loc, "Set-Cookie": "sid=sid-1; Path=/"})
        self._send(404, {"error": "nope"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        b = self._body()
        host = f"http://{self.headers['Host']}"
        if u.path == "/api/v1/authn":
            Mock.stats["authn"] += 1
            if b.get("password") == "demo":
                return self._send(200, {"status": "SUCCESS", "sessionToken": "sess-ok"})
            if b.get("password") == "mfa":
                return self._send(200, {"status": "MFA_REQUIRED", "stateToken": "state-1", "_embedded": {"factors": [
                    {"id": "fac1", "_links": {"verify": {"href": f"{host}/api/v1/authn/factors/fac1/verify"}}}]}})
            return self._send(401, {"errorCode": "E0000004", "errorSummary": "Authentication failed"})
        if u.path.startswith("/api/v1/authn/factors/"):
            if b.get("passCode") == "":
                return self._send(200, {"status": "MFA_CHALLENGE", "stateToken": "state-1"})
            if b.get("passCode") == "123456":
                return self._send(200, {"status": "SUCCESS", "sessionToken": "sess-ok"})
            return self._send(403, {"errorCode": "E0000068", "errorSummary": "Invalid Passcode/Answer"})
        if u.path.endswith("/v1/token"):
            if b.get("code") != "authcode-1" or not b.get("code_verifier"):
                return self._send(400, {"error": "invalid_grant"})
            exp = int(time.time()) + 3600
            return self._send(200, {"access_token": fake_jwt({"exp": exp, "sub": "x"}), "expires_in": 3600,
                                    "id_token": fake_jwt({"myAirCountryId": "US", "exp": exp})})
        if u.path == "/graphql":
            Mock.stats["graphql"] += 1
            if self.headers.get("x-api-key", "").startswith("da2-") is False or \
                    not self.headers.get("Authorization", "").startswith("Bearer "):
                return self._send(401, {"errors": [{"errorInfo": {"errorType": "unauthorized", "errorCode": "x"}}]})
            return self._graphql(b.get("query", ""))
        self._send(404, {})

    def _graphql(self, query: str):
        if "sleepRecords" in query:
            fields = re.search(r"items\s*\{([^}]*)\}", query).group(1).split()
            bad = [f for f in fields if f not in KNOWN | HIDDEN_EXTRA]
            if bad:
                return self._send(200, {"data": None, "errors": [{"message": f"Validation error of type FieldUndefined: Field '{bad[0]}' in type 'SleepRecord' is undefined"}]})
            s, e = re.findall(r'"(\d{4}-\d{2}-\d{2})"', query)[:2]
            d, end = dt.date.fromisoformat(s), min(dt.date.fromisoformat(e), dt.date.today() - dt.timedelta(days=1))
            items = []
            while d <= end:
                if d >= HISTORY_START:
                    n = night(d)
                    if n:
                        items.append({k: v for k, v in n.items() if k in fields})
                d += dt.timedelta(days=1)
            return self._send(200, {"data": {"getPatientWrapper": {"patient": {"firstName": "James"},
                                                                    "sleepRecords": {"items": items}}}})
        return self._send(200, {"data": {"getPatientWrapper": {
            "patient": {"firstName": "James"},
            "masks": [{"maskCode": "AirFit F20"}],
            "fgDevices": [{"serialNumber": "23161234567", "localizedName": "AirSense 10 AutoSet",
                           "deviceSeries": "AirSense 10", "deviceFamily": "CPAP",
                           "lastSleepDataReportTime": dt.datetime.now().isoformat(),
                           "fgDeviceManufacturerName": "ResMed", "fgDevicePatientId": "p-123"}]}}})


def start(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    srv, url = start(int(sys.argv[1]) if len(sys.argv) > 1 else 9999)
    print(f"mock myAir at {url}")
    threading.Event().wait()
