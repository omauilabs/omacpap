"""ResMed myAir client (unofficial).

ResMed publishes no API for myAir. This module reproduces the login flow the
myAir website itself uses — Okta primary auth (+ optional email MFA), an OAuth
authorization-code exchange with PKCE, then ResMed's AppSync GraphQL endpoint.

The flow, endpoints, and client IDs are ported from the MIT-licensed Home
Assistant integration by @prestomation and @Snuffy2:
https://github.com/prestomation/resmed_myair_sensors

It is stdlib-only (urllib) so OmaCPAP needs no pip dependencies on Omarchy.
ResMed can change any of this at any time; errors are mapped to messages that
say what to do next.
"""

from __future__ import annotations

import base64
import calendar
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any, Callable, Iterable

from . import __version__

log = logging.getLogger("omacpap.myair")

USER_AGENT = f"Mozilla/5.0 (X11; Linux x86_64) OmaCPAP/{__version__}"


# ---------------------------------------------------------------- errors
class MyAirError(RuntimeError):
    """Base error with a human-readable, actionable message."""


class AuthError(MyAirError):
    pass


class MFARequired(MyAirError):
    pass


class AccountIncomplete(MyAirError):
    pass


class SessionExpired(MyAirError):
    pass


# ---------------------------------------------------------------- regions
@dataclass(frozen=True)
class Region:
    code: str
    product: str
    okta_host: str
    auth_server_id: str
    client_id: str
    api_key: str
    graphql_url: str
    redirect_url: str
    email_factor_id: str
    okta_base: str = ""  # override for tests, e.g. http://127.0.0.1:9999

    @property
    def _okta(self) -> str:
        return self.okta_base or f"https://{self.okta_host}"

    @property
    def authn_url(self) -> str:
        return f"{self._okta}/api/v1/authn"

    @property
    def authorize_url(self) -> str:
        return f"{self._okta}/oauth2/{self.auth_server_id}/v1/authorize"

    @property
    def token_url(self) -> str:
        return f"{self._okta}/oauth2/{self.auth_server_id}/v1/token"

    def mfa_url(self, factor_id: str | None = None) -> str:
        factor_id = factor_id or self.email_factor_id
        if not factor_id:
            raise AuthError("myAir asked for a verification code but didn't say how to send it. "
                            "Sign in once at myair.resmed.com, then try again.")
        return f"{self._okta}/api/v1/authn/factors/{factor_id}/verify?rememberDevice=true"


REGIONS: dict[str, Region] = {
    # Americas (US, Canada, Latin America) — also used by Australia per upstream README
    "NA": Region(
        code="NA", product="myAir", okta_host="resmed-ext-1.okta.com",
        auth_server_id="aus4ccsxvnidQgLmA297", client_id="0oa4ccq1v413ypROi297",
        api_key="da2-cenztfjrezhwphdqtwtbpqvzui",
        graphql_url="https://graphql.myair-prd.dht.live/graphql",
        redirect_url="https://myair.resmed.com", email_factor_id="",  # no known fixed id; use the one Okta returns
    ),
    "EU": Region(
        code="EU", product="myAir EU", okta_host="id.resmed.eu",
        auth_server_id="aus2uznux2sYKTsEg417", client_id="0oa2uz04d2Pks2NgR417",
        api_key="da2-o66oo6xdnfh5hlfuw5yw5g2dtm",
        graphql_url="https://graphql.hyperdrive.resmed.eu/graphql",
        redirect_url="https://myair.resmed.eu", email_factor_id="emfg9cmjqxEPr52cT417",
    ),
}


def get_region(code: str) -> Region:
    code = (code or "NA").upper()
    if code not in REGIONS:
        raise MyAirError(f"Unknown region {code!r}; use one of {', '.join(REGIONS)}")
    region = REGIONS[code]
    # Test hook: point the whole flow at a local mock server.
    override = os.environ.get("OMACPAP_MYAIR_MOCK")
    if override:
        from dataclasses import replace
        region = replace(region, okta_base=override.rstrip("/"), graphql_url=override.rstrip("/") + "/graphql")
    return region


# ---------------------------------------------------------------- queries
SLEEP_RECORD_FIELDS = [
    "startDate", "totalUsage", "sleepScore", "usageScore", "ahiScore", "maskScore",
    "leakScore", "ahi", "maskPairCount", "leakPercentile", "sleepRecordPatientId",
]

# Field names worth testing against the live schema (introspection is disabled
# on ResMed's AppSync, so the only way to learn more is to ask). Anything that
# does not come back as FieldUndefined gets added to every future sync.
PROBE_CANDIDATES = [
    "endDate", "sleepRecordId", "id", "totalUsageMinutes", "usageMinutes", "usageHours",
    "maskOnCount", "maskOffCount", "maskOnOffCount", "maskSealScore", "maskSeal",
    "leak", "leak50", "leak95", "leakMedian", "leak95Percentile", "leakLpm", "largeLeak",
    "ahiEvents", "eventsPerHour", "oai", "cai", "hi", "uai", "ai", "centralApneaIndex",
    "obstructiveApneaIndex", "hypopneaIndex", "apneaIndex", "csr",
    "pressure", "pressure50", "pressure95", "medianPressure", "maxPressure", "minPressure",
    "pressure95Percentile", "therapyMode", "mode", "eprLevel", "rampTime",
    "humidity", "humidityLevel", "temperature", "tubeTemperature", "climateControl",
    "respiratoryRate", "tidalVolume", "minuteVentilation", "spo2", "pulse",
    "sessionCount", "sessions", "usageScoreMax", "totalScore", "myAirScore",
    "deviceSerialNumber", "fgDeviceSerialNumber", "serialNumber", "dataSource", "lastUpdated",
]


def _jwt_claims(token: str | None) -> dict[str, Any]:
    if not token or token.count(".") < 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return {}


def month_windows(end: dt.date, stop: dt.date) -> Iterable[tuple[dt.date, dt.date]]:
    """Calendar months from end's month backwards to stop's month (inclusive)."""
    y, m = end.year, end.month
    while (y, m) >= (stop.year, stop.month):
        first = dt.date(y, m, 1)
        last = dt.date(y, m, calendar.monthrange(y, m)[1])
        yield first, min(last, end)
        m -= 1
        if m == 0:
            y, m = y - 1, 12


# ---------------------------------------------------------------- http
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


@dataclass
class Response:
    status: int
    headers: Any
    body: bytes

    def json(self) -> dict[str, Any]:
        try:
            return json.loads(self.body or b"{}")
        except json.JSONDecodeError as e:
            snippet = self.body[:200].decode("utf-8", "replace")
            raise MyAirError(f"myAir returned non-JSON (HTTP {self.status}): {snippet}") from e

    def set_cookies(self) -> list[str]:
        return self.headers.get_all("Set-Cookie") or []


# ---------------------------------------------------------------- client
@dataclass
class SessionState:
    """Everything worth caching between runs (stored in the keyring)."""
    device_token: str | None = None   # Okta DT cookie: "remember this device" → fewer MFA prompts
    access_token: str | None = None
    id_token: str | None = None
    expires_at: float = 0.0

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, s: str | None) -> "SessionState":
        if not s:
            return cls()
        try:
            d = json.loads(s)
            return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})
        except (json.JSONDecodeError, TypeError):
            return cls()


@dataclass
class MyAirClient:
    username: str
    password: str | None
    region: Region
    state: SessionState = field(default_factory=SessionState)
    timeout: float = 30.0
    _sid: str | None = None
    _state_token: str | None = None
    _mfa_url: str | None = None

    def __post_init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect())

    # -------------------------------------------------------------- transport
    def _cookie_header(self) -> dict[str, str]:
        parts = []
        if self.state.device_token:
            parts.append(f"DT={self.state.device_token}")
        if self._sid:
            parts.append(f"sid={self._sid}")
        return {"Cookie": "; ".join(parts)} if parts else {}

    def _absorb_cookies(self, resp: Response) -> None:
        for header in resp.set_cookies():
            c = SimpleCookie()
            try:
                c.load(header)
            except Exception:  # noqa: BLE001 - malformed third-party cookies are ignorable
                continue
            for k, morsel in c.items():
                if k.lower() == "dt" and morsel.value:
                    self.state.device_token = morsel.value
                elif k.lower() == "sid" and morsel.value:
                    self._sid = morsel.value

    def _request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                 json_body: Any = None, form: dict[str, Any] | None = None,
                 params: dict[str, Any] | None = None) -> Response:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        h = {"User-Agent": USER_AGENT, "Accept": "application/json", **self._cookie_header(), **(headers or {})}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            h["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                resp = Response(r.status, r.headers, r.read())
        except urllib.error.HTTPError as e:  # 3xx (no-redirect) and 4xx/5xx land here
            with e:  # the error is also the open response; close it once read
                resp = Response(e.code, e.headers, e.read() if e.fp else b"")
        except urllib.error.URLError as e:
            raise MyAirError(f"Could not reach {urllib.parse.urlparse(url).netloc}: {e.reason}") from e
        self._absorb_cookies(resp)
        return resp

    # -------------------------------------------------------------- auth
    @property
    def token_valid(self) -> bool:
        return bool(self.state.access_token) and time.time() < self.state.expires_at - 60

    def login(self) -> str:
        """Returns 'SUCCESS' or raises MFARequired after sending the email code."""
        if not self.password:
            raise AuthError("No myAir password available — run `omacpap login`.")
        if not self.state.device_token:
            self._request("GET", self.region.authorize_url)  # primes the DT cookie
        resp = self._request("POST", self.region.authn_url,
                             json_body={"username": self.username, "password": self.password})
        body = resp.json()
        if resp.status == 401 or body.get("errorCode") == "E0000004":
            raise AuthError("myAir rejected the email/password. Check them at myair.resmed.com.")
        self._raise_okta_error("sign-in", resp, body)
        status = body.get("status")
        if status == "SUCCESS":
            self._exchange_tokens(body["sessionToken"])
            return "SUCCESS"
        if status == "MFA_REQUIRED":
            self._state_token = body.get("stateToken")
            self._mfa_url = self._factor_url(body)
            trig = self._request("POST", self._mfa_url, json_body={"passCode": "", "stateToken": self._state_token})
            self._raise_okta_error("sending MFA code", trig, trig.json())
            raise MFARequired("ResMed emailed you a verification code. Enter it to finish signing in.")
        if status in {"PASSWORD_EXPIRED", "PASSWORD_WARN", "LOCKED_OUT"}:
            raise AuthError(f"myAir account status is {status}. Sign in at myair.resmed.com to fix it.")
        raise AuthError(f"Unexpected myAir sign-in status: {status!r}")

    def verify_mfa(self, code: str) -> str:
        if not (self._state_token and self._mfa_url):
            raise AuthError("No MFA challenge is pending — start the login again.")
        resp = self._request("POST", self._mfa_url,
                             json_body={"passCode": code.strip(), "stateToken": self._state_token})
        body = resp.json()
        if resp.status in (401, 403) or body.get("errorCode") in {"E0000068", "E0000069"}:
            raise AuthError("That verification code was not accepted. Request a new one and try again.")
        self._raise_okta_error("verifying MFA code", resp, body)
        if body.get("status") != "SUCCESS" or "sessionToken" not in body:
            raise AuthError(f"MFA verification did not succeed (status {body.get('status')!r}).")
        self._state_token = None
        self._exchange_tokens(body["sessionToken"])
        return "SUCCESS"

    def _factor_url(self, authn: dict[str, Any]) -> str:
        factors = (authn.get("_embedded") or {}).get("factors") or []
        if factors and isinstance(factors[0], dict):
            href = (((factors[0].get("_links") or {}).get("verify") or {}).get("href"))
            if href:
                return f"{href}?rememberDevice=true"
            if factors[0].get("id"):
                return self.region.mfa_url(factors[0]["id"])
        return self.region.mfa_url()

    def _exchange_tokens(self, session_token: str) -> None:
        verifier = re.sub(r"[^a-zA-Z0-9]+", "", base64.urlsafe_b64encode(os.urandom(40)).decode())
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        resp = self._request("GET", self.region.authorize_url, params={
            "client_id": self.region.client_id,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "none",
            "redirect_uri": self.region.redirect_url,
            "response_mode": "fragment",
            "response_type": "code",
            "sessionToken": session_token,
            "scope": "openid profile email",
            "state": secrets.token_urlsafe(8),
        })
        location = resp.headers.get("Location") if resp.headers else None
        if not location:
            raise AuthError(f"myAir did not return an authorization redirect (HTTP {resp.status}).")
        frag = urllib.parse.parse_qs(urllib.parse.urldefrag(location).fragment)
        if "code" not in frag:
            err = frag.get("error_description", frag.get("error", ["unknown error"]))[0]
            raise AuthError(f"myAir authorization failed: {err}")
        tok = self._request("POST", self.region.token_url, form={
            "client_id": self.region.client_id,
            "redirect_uri": self.region.redirect_url,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
            "code": frag["code"][0],
        })
        body = tok.json()
        if "access_token" not in body:
            raise AuthError(f"Token exchange failed: {body.get('error_description') or body}")
        self.state.access_token = body["access_token"]
        self.state.id_token = body.get("id_token")
        exp = _jwt_claims(self.state.access_token).get("exp")
        self.state.expires_at = float(exp) if exp else time.time() + float(body.get("expires_in", 3600))

    @staticmethod
    def _raise_okta_error(step: str, resp: Response, body: dict[str, Any]) -> None:
        if resp.status >= 400 or "errorCode" in body:
            summary = body.get("errorSummary") or body.get("error_description") or body
            raise AuthError(f"myAir {step} failed (HTTP {resp.status}): {summary}")

    def ensure_session(self) -> None:
        if not self.token_valid:
            self.login()

    # -------------------------------------------------------------- graphql
    def _country(self) -> str:
        return str(_jwt_claims(self.state.id_token).get("myAirCountryId") or "US")

    def graphql(self, operation: str, query: str, *, _retry: bool = True) -> dict[str, Any]:
        self.ensure_session()
        headers = {
            "x-api-key": self.region.api_key,
            "Authorization": f"Bearer {self.state.access_token}",
            "rmdhandsetid": "02c1c662-c289-41fd-a9ae-196ff15b5166",
            "rmdlanguage": "en",
            "rmdhandsetmodel": "Chrome",
            "rmdhandsetosversion": "127.0.6533.119",
            "rmdproduct": self.region.product,
            "rmdappversion": "1.0.0",
            "rmdhandsetplatform": "Web",
            "rmdcountry": self._country(),
            "accept-language": "en-US,en;q=0.9",
        }
        # GraphQL rides over a separate host; don't leak Okta cookies to it.
        saved_dt, saved_sid = self.state.device_token, self._sid
        self.state.device_token = self._sid = None
        try:
            resp = self._request("POST", self.region.graphql_url, headers=headers,
                                 json_body={"operationName": operation, "variables": {}, "query": query})
        finally:
            self.state.device_token, self._sid = saved_dt, saved_sid
        body = resp.json()
        errors = body.get("errors") or []
        if errors:
            info = errors[0].get("errorInfo") or {}
            etype, ecode = info.get("errorType"), info.get("errorCode")
            if etype == "unauthorized" or resp.status == 401:
                if _retry:
                    self.state.access_token = None
                    return self.graphql(operation, query, _retry=False)
                raise SessionExpired("myAir session expired and re-login did not help.")
            if etype == "badRequest" and ecode in {"onboardingFlowInProgress", "equipmentNotAssigned"}:
                raise AccountIncomplete(
                    "myAir says your account setup is incomplete. Sign in at myair.resmed.com, "
                    "accept any new terms, and make sure your machine is registered.")
            if body.get("data") is None:
                msg = errors[0].get("message") or f"{etype}: {ecode}"
                raise MyAirError(f"myAir query {operation} failed: {msg}")
        return body

    # -------------------------------------------------------------- data
    def patient_and_device(self) -> tuple[dict[str, Any], str | None, str | None]:
        q = """query getPatientWrapper { getPatientWrapper {
                 patient { firstName }
                 masks { maskCode }
                 fgDevices { serialNumber localizedName deviceSeries deviceFamily
                             lastSleepDataReportTime fgDeviceManufacturerName fgDevicePatientId } } }"""
        pw = (self.graphql("getPatientWrapper", q).get("data") or {}).get("getPatientWrapper") or {}
        devices = pw.get("fgDevices") or []
        masks = [m.get("maskCode") for m in pw.get("masks") or [] if isinstance(m, dict) and m.get("maskCode")]
        first = (pw.get("patient") or {}).get("firstName")
        return (devices[0] if devices else {}), (masks[0] if masks else None), first

    def sleep_records(self, start: dt.date, end: dt.date, fields: list[str] | None = None) -> list[dict[str, Any]]:
        f = " ".join(dict.fromkeys((fields or []) + SLEEP_RECORD_FIELDS))
        q = f"""query GetPatientSleepRecords {{ getPatientWrapper {{
                  sleepRecords(startMonth: "{start.isoformat()}", endMonth: "{end.isoformat()}") {{
                    items {{ {f} }} }} }} }}"""
        body = self.graphql("GetPatientSleepRecords", q)
        pw = (body.get("data") or {}).get("getPatientWrapper") or {}
        items = (pw.get("sleepRecords") or {}).get("items") or []
        return [i for i in items if isinstance(i, dict)]

    def history(self, *, since: dt.date | None = None, extra_fields: list[str] | None = None,
                progress: Callable[[str], None] | None = None,
                floor: dt.date = dt.date(2014, 1, 1)) -> list[dict[str, Any]]:
        """Walk month by month back from today.

        * Incremental (``since`` given): from ``since`` to today.
        * Full backfill: until three straight empty months after data was found
          (or twelve before any data), never earlier than ``floor`` —
          the AirSense 10 launched in 2014.
        """
        today = dt.date.today()
        stop = max(since, floor) if since else floor
        out: dict[str, dict[str, Any]] = {}
        empty_run, found_any = 0, False
        for first, last in month_windows(today, stop):
            recs = self.sleep_records(max(first, stop), last, extra_fields)
            with_data = [r for r in recs if r.get("startDate")]
            for r in with_data:
                out[str(r["startDate"])[:10]] = r
            if progress:
                progress(f"{first:%b %Y}: {len(with_data)} nights")
            if since is None:
                if with_data:
                    found_any, empty_run = True, 0
                else:
                    empty_run += 1
                    if (found_any and empty_run >= 3) or (not found_any and empty_run >= 12):
                        break
            time.sleep(0.25)  # be gentle with ResMed
        return [out[k] for k in sorted(out)]

    def probe_fields(self, candidates: Iterable[str] = PROBE_CANDIDATES,
                     progress: Callable[[str], None] | None = None) -> list[str]:
        """Return candidate SleepRecord fields that ResMed's schema accepts."""
        end = dt.date.today()
        start = end - dt.timedelta(days=14)
        accepted: list[str] = []
        for name in candidates:
            q = f"""query ProbeField {{ getPatientWrapper {{
                      sleepRecords(startMonth: "{start}", endMonth: "{end}") {{ items {{ startDate {name} }} }} }} }}"""
            self.ensure_session()
            try:
                body = self.graphql("ProbeField", q)
                ok = not body.get("errors")
            except MyAirError as e:
                ok = False
                if "FieldUndefined" not in str(e) and "Validation" not in str(e):
                    log.debug("probe %s: %s", name, e)
            if ok:
                accepted.append(name)
            if progress:
                progress(f"{name}: {'yes' if ok else 'no'}")
            time.sleep(0.15)
        return accepted
