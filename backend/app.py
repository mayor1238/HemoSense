"""HemoSense back end.

- Serves the web app (static/).
- API for logins, patients, tests, results, research data and settings.
- Live links: readers connect to /ws/reader, open apps to /ws/app.

Laptop:  uvicorn app:app --reload           (http://localhost:8000)
Nobus:   docker compose up -d --build       (see README)
"""
import contextlib
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import date, datetime, timedelta, timezone

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from analysis import analyse
from db import Store, from_json, to_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hemosense")

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DEVICE_KEY = os.environ.get("DEVICE_KEY", "dev-device-key")
ADMIN_USER = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "hemosense-admin")
SESSION_HOURS = 12
GROUPS = ("A+", "A−", "B+", "B−", "AB+", "AB−", "O+", "O−")
ID_TYPES = ("hospital", "nhia", "nin", "nbsa", "emergency")
DEFAULTS = {
    "facility": "Demo General Hospital, Abeokuta",
    "method": "relative", "direction": "rise", "threshold": 8, "settle": 3,
    "sweep_s": 120, "interval_s": 2,
    "agg_low": 14, "agg_high": 19, "non_low": 30, "non_high": 38,
}

store: Store | None = None


# ------------------------------------------------------------------ small helpers
def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def fail(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"{salt.hex()}${digest.hex()}"


def password_ok(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$")
        test = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(test.hex(), digest)
    except ValueError:
        return False


def short_code(test_id: str) -> str:
    return "HS-" + test_id.replace("-", "")[:6].upper()


def age_band(dob: str | None) -> str:
    try:
        born = date.fromisoformat(dob or "")
    except ValueError:
        return "unknown"
    today = date.today()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return "<18" if age < 18 else "18–29" if age < 30 else "30–44" if age < 45 else "45–59" if age < 60 else "60+"


def settings() -> dict:
    row = store.row("SELECT value FROM app_settings WHERE name = 'analysis'")
    return {**DEFAULTS, **(from_json(row["value"], {}) if row else {})}


async def read_json(request: Request) -> dict:
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ login and roles
def user_from_token(token: str | None) -> dict | None:
    if not token:
        return None
    u = store.row("SELECT u.id, u.username, u.full_name, u.role, s.created_at FROM sessions s "
                  "JOIN users u ON u.id = s.user_id WHERE s.token = :t", t=token)
    if not u:
        return None
    started = datetime.fromisoformat(u.pop("created_at").replace("Z", "+00:00"))
    if datetime.now(timezone.utc) - started > timedelta(hours=SESSION_HOURS):
        store.run("DELETE FROM sessions WHERE token = :t", t=token)
        return None
    return u


def needs_login(admin: bool = False):
    def wrap(handler):
        async def inner(request: Request):
            auth = request.headers.get("authorization", "")
            user = user_from_token(auth[7:] if auth.lower().startswith("bearer ") else None)
            if not user:
                return fail("Please log in.", 401)
            if admin and user["role"] != "admin":
                return fail("Only an admin can do this.", 403)
            request.state.user = user
            return await handler(request)
        return inner
    return wrap


LOGIN_FAILS: dict[str, list[float]] = {}


async def login(request: Request):
    ip = request.client.host if request.client else "?"
    recent = [t for t in LOGIN_FAILS.get(ip, []) if time.time() - t < 300]
    if len(recent) >= 8:
        return fail("Too many attempts. Wait 5 minutes.", 429)
    body = await read_json(request)
    u = store.row("SELECT * FROM users WHERE username = :u", u=str(body.get("username", "")).strip().lower())
    if not u or not password_ok(str(body.get("password", "")), u["password_hash"]):
        LOGIN_FAILS[ip] = recent + [time.time()]
        return fail("Wrong username or password.", 401)
    LOGIN_FAILS.pop(ip, None)
    token = secrets.token_urlsafe(32)
    store.run("INSERT INTO sessions (token, user_id, created_at) VALUES (:t, :u, :c)", t=token, u=u["id"], c=now())
    return JSONResponse({"token": token, "user": {k: u[k] for k in ("id", "username", "full_name", "role")}})


@needs_login()
async def logout(request: Request):
    store.run("DELETE FROM sessions WHERE token = :t", t=request.headers["authorization"][7:])
    return JSONResponse({"ok": True})


@needs_login()
async def me(request: Request):
    return JSONResponse({"user": request.state.user, "settings": settings()})


@needs_login(admin=True)
async def users(request: Request):
    if request.method == "GET":
        return JSONResponse({"users": store.rows("SELECT id, username, full_name, role, created_at FROM users ORDER BY created_at")})
    b = await read_json(request)
    username, full_name = str(b.get("username", "")).strip().lower(), str(b.get("full_name", "")).strip()
    password, role = str(b.get("password", "")), b.get("role", "operator")
    if not re.fullmatch(r"[a-z0-9._-]{3,30}", username):
        return fail("Username: 3–30 letters, numbers, dots or dashes.")
    if not full_name or len(password) < 8 or role not in ("admin", "operator"):
        return fail("Enter a full name, a password of 8+ characters and a role.")
    if store.row("SELECT id FROM users WHERE username = :u", u=username):
        return fail("That username is taken.", 409)
    store.run("INSERT INTO users (id, username, full_name, password_hash, role, created_at) VALUES (:i, :u, :n, :p, :r, :c)",
              i=str(uuid.uuid4()), u=username, n=full_name, p=hash_password(password), r=role, c=now())
    return JSONResponse({"ok": True})


async def app_settings(request: Request):
    auth = request.headers.get("authorization", "")
    user = user_from_token(auth[7:] if auth.lower().startswith("bearer ") else None)
    if not user:
        return fail("Please log in.", 401)
    if request.method == "GET":
        return JSONResponse(settings())
    if user["role"] != "admin":
        return fail("Only an admin can change settings.", 403)
    b = await read_json(request)
    new = {**settings(), **{k: b[k] for k in DEFAULTS if k in b}}
    if new["method"] not in ("relative", "range") or new["direction"] not in ("rise", "fall"):
        return fail("Unknown method or direction.")
    try:
        for k in DEFAULTS:
            if k not in ("facility", "method", "direction"):
                new[k] = float(new[k])
    except (TypeError, ValueError):
        return fail("Numbers only for thresholds, ranges and timing.")
    if not (new["threshold"] > 0 and 10 <= new["sweep_s"] <= 600 and 1 <= new["interval_s"] <= 30 and new["settle"] >= 0):
        return fail("Threshold > 0, sweep 10–600 s, interval 1–30 s.")
    store.run("DELETE FROM app_settings WHERE name = 'analysis'")
    store.run("INSERT INTO app_settings (name, value) VALUES ('analysis', :v)", v=to_json(new))
    return JSONResponse(new)


# ------------------------------------------------------------------ live links
class Reader:
    def __init__(self, ws: WebSocket, reader_id: str):
        self.ws, self.id = ws, reader_id
        self.info: dict = {}
        self.pending: str | None = None
        self.since = now()

    def public(self) -> dict:
        i = self.info
        return {"id": self.id, "firmware": i.get("firmware"), "simulated": bool(i.get("simulate")),
                "sensor_ok": i.get("sensor_ok"), "calibrated": i.get("calibrated"),
                "busy": i.get("state") == "running", "test_id": i.get("test_id"), "since": self.since}

    async def send(self, msg: dict) -> bool:
        try:
            await self.ws.send_text(json.dumps(msg))
            return True
        except Exception:
            return False


READERS: dict[str, Reader] = {}
APPS: set[WebSocket] = set()


async def tell_apps(msg: dict):
    text = json.dumps(msg)
    for ws in list(APPS):
        try:
            await ws.send_text(text)
        except Exception:
            APPS.discard(ws)


async def tell_readers_list():
    await tell_apps({"type": "readers", "readers": [r.public() for r in READERS.values()]})


def test_readings(test_id: str) -> list[dict]:
    return [{"seq": r["seq"], "t_ms": r["t_ms"], "z": [r["z_a"], r["z_b"], r["z_d"]],
             "re": [r["re_a"], r["re_b"], r["re_d"]], "im": [r["im_a"], r["im_b"], r["im_d"]]}
            for r in store.rows("SELECT * FROM readings WHERE test_id = :t ORDER BY seq", t=test_id)]


async def reader_socket(ws: WebSocket):
    rid, key = ws.query_params.get("device_id", ""), ws.query_params.get("key", "")
    await ws.accept()
    if not hmac.compare_digest(key, DEVICE_KEY) or not re.fullmatch(r"[A-Za-z0-9_-]{3,40}", rid):
        await ws.send_text(json.dumps({"type": "error", "msg": "Wrong device key or device ID"}))
        await ws.close(code=4401)
        log.warning("Rejected reader %r", rid)
        return
    if rid in READERS:
        with contextlib.suppress(Exception):
            await READERS[rid].ws.close(code=4000)
    reader = READERS[rid] = Reader(ws, rid)
    log.info("Reader online: %s", rid)
    await reader.send({"type": "hello"})
    await tell_readers_list()
    try:
        while True:
            try:
                msg = json.loads(await ws.receive_text())
            except ValueError:
                continue
            if isinstance(msg, dict):
                await from_reader(reader, msg)
    except WebSocketDisconnect:
        pass
    finally:
        if READERS.get(rid) is reader:
            del READERS[rid]
            log.info("Reader offline: %s", rid)
            await tell_readers_list()


async def finish_test(test_id: str, status: str, reader_id: str):
    store.run("UPDATE tests SET status = :s, finished_at = :f WHERE id = :i AND status IN ('starting', 'running')",
              s=status, f=now(), i=test_id)
    t = store.row("SELECT analysis_settings FROM tests WHERE id = :i", i=test_id)
    result = analyse(test_readings(test_id), from_json(t["analysis_settings"], settings()) if t else settings())
    await tell_apps({"type": "done", "reader_id": reader_id, "test_id": test_id, "status": status, "analysis": result})


async def from_reader(reader: Reader, msg: dict):
    kind, tid = msg.get("type"), msg.get("test_id")
    msg["reader_id"] = reader.id
    if kind == "info":
        reader.info = msg
        running = store.row("SELECT id FROM tests WHERE reader_id = :r AND status = 'running'", r=reader.id)
        if msg.get("state") == "running" and tid:
            await reader.send({"type": "sync", "test_id": tid})          # resend what we missed
        elif running:
            await finish_test(running["id"], "interrupted", reader.id)   # reader restarted mid-test
        await tell_readers_list()
    elif kind == "ack" and tid:
        store.run("UPDATE tests SET status = 'running' WHERE id = :i AND status = 'starting'", i=tid)
        reader.info.update(state="running", test_id=tid)
        reader.pending = None
        await tell_apps(msg)
        await tell_readers_list()
    elif kind == "reading" and tid:
        z, re_, im = (msg.get(k) or [None, None, None] for k in ("z", "re", "im"))
        store.run("INSERT INTO readings (test_id, seq, t_ms, z_a, z_b, z_d, re_a, re_b, re_d, im_a, im_b, im_d) "
                  "VALUES (:t, :s, :ms, :za, :zb, :zd, :ra, :rb, :rd, :ia, :ib, :id) ON CONFLICT (test_id, seq) DO NOTHING",
                  t=tid, s=int(msg.get("i", 0)), ms=int(msg.get("t_ms", 0)), za=z[0], zb=z[1], zd=z[2],
                  ra=re_[0], rb=re_[1], rd=re_[2], ia=im[0], ib=im[1], id=im[2])
        await tell_apps(msg)
    elif kind == "done" and tid:
        reason = msg.get("reason", "complete")
        reader.info.update(state="idle")
        await finish_test(tid, {"complete": "done", "stopped": "stopped"}.get(reason, "error"), reader.id)
        await tell_readers_list()
    elif kind == "error":
        if reader.pending:  # the reader refused to start: free the cartridge
            store.run("UPDATE tests SET status = 'rejected' WHERE id = :i AND status = 'starting'", i=reader.pending)
            msg.setdefault("test_id", reader.pending)
            reader.pending = None
        await tell_apps(msg)
    elif kind == "calibrated":
        reader.info["calibrated"] = bool(msg.get("ok"))
        await tell_apps(msg)
        await tell_readers_list()
    elif kind in ("snapshot", "pong"):
        await tell_apps(msg)


async def app_socket(ws: WebSocket):
    user = user_from_token(ws.query_params.get("token"))
    await ws.accept()
    if not user:
        await ws.close(code=4401)
        return
    APPS.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "readers", "readers": [r.public() for r in READERS.values()]}))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        APPS.discard(ws)


# ------------------------------------------------------------------ patients
def last_group(patient_id: str) -> dict | None:
    t = store.row("SELECT blood_group, lab_group, saved_at FROM tests WHERE patient_id = :p AND saved_at IS NOT NULL "
                  "AND result_valid = 1 ORDER BY saved_at DESC LIMIT 1", p=patient_id)
    return {"group": t["lab_group"] or t["blood_group"], "lab_confirmed": bool(t["lab_group"]), "date": t["saved_at"]} if t else None


def patient_view(p: dict) -> dict:
    return {"id": p["id"], "id_type": p["id_type"], "id_value": p["id_value"], "full_name": p["full_name"],
            "dob": p["dob"], "sex": p["sex"], "last": last_group(p["id"])}


@needs_login()
async def find_patient(request: Request):
    q = request.query_params
    p = store.row("SELECT * FROM patients WHERE id_type = :t AND LOWER(id_value) = :v",
                  t=q.get("id_type", ""), v=q.get("id_value", "").strip().lower())
    return JSONResponse({"patient": patient_view(p) if p else None})


@needs_login()
async def patients(request: Request):
    if request.method == "GET":
        q = "%" + request.query_params.get("q", "").strip().lower() + "%"
        rows = store.rows("SELECT * FROM patients WHERE LOWER(full_name) LIKE :q OR LOWER(id_value) LIKE :q "
                          "ORDER BY created_at DESC LIMIT 200", q=q)
        return JSONResponse({"patients": [patient_view(p) for p in rows]})
    b = await read_json(request)
    id_type, id_value = b.get("id_type"), str(b.get("id_value", "")).strip()
    full_name, dob, sex = str(b.get("full_name", "")).strip(), b.get("dob") or None, b.get("sex") or None
    if id_type not in ID_TYPES or not id_value or not full_name:
        return fail("ID type, patient ID and full name are needed.")
    if id_type != "emergency" and not dob:
        return fail("Date of birth is needed as the second identifier.")
    if dob:
        try:
            if date.fromisoformat(dob) > date.today():
                return fail("Date of birth is in the future.")
        except ValueError:
            return fail("Date of birth must look like 1994-06-12.")
    p = store.row("SELECT * FROM patients WHERE id_type = :t AND LOWER(id_value) = :v", t=id_type, v=id_value.lower())
    if p:
        if p["full_name"].strip().lower() != full_name.lower() or (p["dob"] or "") != (dob or ""):
            return fail("This ID belongs to a patient with a different name or date of birth. Check identity.", 409)
        return JSONResponse({"patient": patient_view(p), "created": False})
    pid = str(uuid.uuid4())
    store.run("INSERT INTO patients (id, id_type, id_value, full_name, dob, sex, created_at, created_by) "
              "VALUES (:i, :t, :v, :n, :d, :s, :c, :u)", i=pid, t=id_type, v=id_value, n=full_name, d=dob, s=sex,
              c=now(), u=request.state.user["id"])
    return JSONResponse({"patient": patient_view(store.row("SELECT * FROM patients WHERE id = :i", i=pid)), "created": True})


@needs_login()
async def emergency_id(request: Request):
    day = datetime.now(timezone.utc).strftime("%y%m%d")
    n = len(store.rows("SELECT id FROM patients WHERE id_type = 'emergency' AND id_value LIKE :p", p=f"EMG-{day}-%"))
    while store.row("SELECT id FROM patients WHERE id_type = 'emergency' AND id_value = :v", v=f"EMG-{day}-{n + 1:02d}"):
        n += 1
    return JSONResponse({"id_value": f"EMG-{day}-{n + 1:02d}", "full_name": f"Unknown patient {n + 1:02d}"})


@needs_login()
async def patient_detail(request: Request):
    p = store.row("SELECT * FROM patients WHERE id = :i", i=request.path_params["pid"])
    if not p:
        return fail("No such patient.", 404)
    tests = store.rows(TEST_LIST_SQL + " AND t.patient_id = :p ORDER BY t.started_at DESC", p=p["id"])
    return JSONResponse({"patient": patient_view(p), "tests": [test_view(t) for t in tests]})


# ------------------------------------------------------------------ tests
TEST_LIST_SQL = ("SELECT t.*, p.id_type, p.id_value, p.full_name, p.dob, p.sex FROM tests t "
                 "JOIN patients p ON p.id = t.patient_id WHERE t.saved_at IS NOT NULL")


def test_view(t: dict) -> dict:
    return {"id": t["id"], "code": short_code(t["id"]), "started_at": t["started_at"], "saved_at": t["saved_at"],
            "status": t["status"], "patient": {"id": t["patient_id"], "id_type": t["id_type"], "id_value": t["id_value"],
                                               "full_name": t["full_name"], "dob": t["dob"], "sex": t["sex"]},
            "cartridge_id": t["cartridge_id"], "reagent_lot": t["reagent_lot"], "operator": t["operator"],
            "reader_id": t["reader_id"], "simulated": bool(t["simulated"]), "group": t["blood_group"],
            "valid": bool(t["result_valid"]), "confidence": t["confidence"], "method": t["method"],
            "notes": from_json(t["notes"], []), "lab_group": t["lab_group"], "research": bool(t["research_consent"])}


def cartridge_used(cartridge: str) -> bool:
    return store.row("SELECT id FROM tests WHERE LOWER(cartridge_id) = :c AND status NOT IN ('starting', 'rejected')",
                     c=cartridge.strip().lower()) is not None


@needs_login()
async def cartridge_check(request: Request):
    return JSONResponse({"used": cartridge_used(request.path_params["cid"])})


@needs_login()
async def tests(request: Request):
    if request.method == "GET":
        q = request.query_params
        sql, args = TEST_LIST_SQL, {}
        if q.get("q"):
            sql += " AND (LOWER(p.full_name) LIKE :q OR LOWER(p.id_value) LIKE :q OR LOWER(t.cartridge_id) LIKE :q)"
            args["q"] = "%" + q["q"].strip().lower() + "%"
        if q.get("group") in GROUPS:
            sql += " AND t.blood_group = :g"
            args["g"] = q["group"]
        if q.get("lab") == "confirmed":
            sql += " AND t.lab_group IS NOT NULL"
        elif q.get("lab") == "pending":
            sql += " AND t.lab_group IS NULL"
        rows = store.rows(sql + " ORDER BY t.saved_at DESC LIMIT 500", **args)
        return JSONResponse({"tests": [test_view(t) for t in rows]})

    b = await read_json(request)
    try:
        tid = str(uuid.UUID(str(b.get("test_id", ""))))
        sweep, interval = float(b.get("sweep_s", 120)), float(b.get("interval_s", 2))
    except (ValueError, TypeError):
        return fail("Bad test ID or timing.")
    cartridge, lot = str(b.get("cartridge_id", "")).strip(), str(b.get("reagent_lot", "")).strip()
    operator, rid = str(b.get("operator", "")).strip(), str(b.get("reader_id", ""))
    patient = store.row("SELECT id FROM patients WHERE id = :i", i=str(b.get("patient_id", "")))
    if not patient:
        return fail("Register the patient first.")
    if not b.get("care_consent"):
        return fail("Care consent is needed before testing.")
    if not cartridge or not lot or not operator:
        return fail("Cartridge ID, reagent lot and operator are needed.")
    if cartridge_used(cartridge):
        return fail("This cartridge was already used. Cartridges are single use.", 409)
    if not (10 <= sweep <= 600 and 1 <= interval <= 30):
        return fail("Sweep 10–600 s, interval 1–30 s.")
    reader = READERS.get(rid)
    if not reader:
        return fail("That reader is offline. Check its power and WiFi.", 409)
    if reader.info.get("state") == "running":
        return fail("That reader is busy with another test.", 409)
    if reader.info.get("calibrated") is False:
        return fail("Calibrate the reader first (5 kΩ resistor on C0).", 409)
    if store.row("SELECT id FROM tests WHERE id = :i", i=tid):
        return fail("This test already exists.", 409)
    s = settings()
    store.run("INSERT INTO tests (id, patient_id, operator, user_id, cartridge_id, reagent_lot, reader_id, firmware, simulated, "
              "facility, research_consent, sweep_s, interval_s, status, started_at, analysis_settings) VALUES (:i, :p, :o, :u, "
              ":c, :l, :r, :f, :sim, :fac, :rc, :sw, :iv, 'starting', :st, :an)",
              i=tid, p=patient["id"], o=operator, u=request.state.user["id"], c=cartridge, l=lot, r=rid,
              f=reader.info.get("firmware"), sim=1 if reader.info.get("simulate") else 0, fac=s["facility"],
              rc=1 if b.get("research_consent") else 0, sw=sweep, iv=interval, st=now(), an=to_json(s))
    reader.pending = tid
    if not await reader.send({"type": "start", "test_id": tid, "short_id": short_code(tid),
                              "duration_s": sweep, "interval_s": interval}):
        store.run("UPDATE tests SET status = 'rejected' WHERE id = :i", i=tid)
        return fail("Could not reach the reader. Try again.", 502)
    return JSONResponse({"ok": True, "code": short_code(tid), "simulated": bool(reader.info.get("simulate"))})


@needs_login()
async def test_detail(request: Request):
    t = store.row("SELECT t.*, p.id_type, p.id_value, p.full_name, p.dob, p.sex FROM tests t "
                  "JOIN patients p ON p.id = t.patient_id WHERE t.id = :i", i=request.path_params["tid"])
    if not t:
        return fail("No such test.", 404)
    readings = test_readings(t["id"])
    view = test_view(t)
    view["readings"] = readings
    view["analysis"] = analyse(readings, from_json(t["analysis_settings"], settings()))
    return JSONResponse(view)


@needs_login()
async def stop_test(request: Request):
    t = store.row("SELECT reader_id FROM tests WHERE id = :i", i=request.path_params["tid"])
    if not t:
        return fail("No such test.", 404)
    reader = READERS.get(t["reader_id"])
    if reader:
        await reader.send({"type": "stop", "test_id": request.path_params["tid"]})
    return JSONResponse({"ok": True, "reader_online": reader is not None})


@needs_login()
async def save_test(request: Request):
    tid = request.path_params["tid"]
    b = await read_json(request)
    if not b.get("identity_checked"):
        return fail("Confirm the identity check before saving.")
    t = store.row("SELECT t.*, p.dob, p.sex FROM tests t JOIN patients p ON p.id = t.patient_id WHERE t.id = :i", i=tid)
    if not t:
        return fail("No such test.", 404)
    if t["saved_at"]:
        return fail("This result is already saved.", 409)
    if t["status"] in ("starting", "rejected"):
        return fail("This test never started on the reader.", 409)
    readings = test_readings(tid)
    result = analyse(readings, from_json(t["analysis_settings"], settings()))
    store.run("UPDATE tests SET blood_group = :g, result_valid = :v, confidence = :c, method = :m, notes = :n, saved_at = :s, "
              "status = CASE WHEN status IN ('starting', 'running') THEN 'stopped' ELSE status END WHERE id = :i",
              g=result["group"], v=1 if result["valid"] else 0, c=result["confidence"], m=result["method"],
              n=to_json(result["flags"]), s=now(), i=tid)
    if t["research_consent"]:
        store.run("INSERT INTO research (test_id, created_at, reader_id, firmware, simulated, reagent_lot, age_band, sex, "
                  "device_group, lab_group, features) VALUES (:i, :c, :r, :f, :sim, :l, :a, :s, :g, NULL, :ft)",
                  i=tid, c=t["started_at"], r=t["reader_id"], f=t["firmware"], sim=t["simulated"], l=t["reagent_lot"],
                  a=age_band(t["dob"]), s=t["sex"], g=result["group"] or "invalid", ft=to_json(result))
    saved = store.row(TEST_LIST_SQL + " AND t.id = :i", i=tid)
    return JSONResponse({"test": test_view(saved), "analysis": result})


@needs_login()
async def lab_result(request: Request):
    tid = request.path_params["tid"]
    group = (await read_json(request)).get("group") or None
    if group is not None and group not in GROUPS:
        return fail("Unknown blood group.")
    if not store.row("SELECT id FROM tests WHERE id = :i AND saved_at IS NOT NULL", i=tid):
        return fail("No saved test with that ID.", 404)
    store.run("UPDATE tests SET lab_group = :g WHERE id = :i", g=group, i=tid)
    store.run("UPDATE research SET lab_group = :g WHERE test_id = :i", g=group, i=tid)
    return JSONResponse({"ok": True})


@needs_login()
async def calibrate(request: Request):
    reader = READERS.get(request.path_params["rid"])
    if not reader:
        return fail("That reader is offline.", 409)
    if reader.info.get("state") == "running":
        return fail("Can't calibrate during a test.", 409)
    await reader.send({"type": "calibrate"})
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------ dashboard, research, export
@needs_login()
async def overview(request: Request):
    saved = store.rows("SELECT blood_group, lab_group, result_valid, saved_at, simulated FROM tests WHERE saved_at IS NOT NULL")
    today = datetime.now(timezone.utc).date().isoformat()
    by_group = {g: 0 for g in GROUPS}
    per_day: dict[str, int] = {}
    agree = checked = 0
    for t in saved:
        if t["result_valid"] and t["blood_group"] in by_group:
            by_group[t["blood_group"]] += 1
        per_day[t["saved_at"][:10]] = per_day.get(t["saved_at"][:10], 0) + 1
        if t["lab_group"] and t["result_valid"]:
            checked += 1
            agree += t["lab_group"] == t["blood_group"]
    days = [(datetime.now(timezone.utc).date() - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    return JSONResponse({
        "tests_total": len(saved), "tests_today": per_day.get(today, 0),
        "valid_rate": round(sum(1 for t in saved if t["result_valid"]) / len(saved) * 100) if saved else None,
        "lab_checked": checked, "lab_agreement": round(agree / checked * 100) if checked else None,
        "by_group": by_group, "per_day": [{"day": d, "tests": per_day.get(d, 0)} for d in days],
        "patients": len(store.rows("SELECT id FROM patients")),
        "readers": [r.public() for r in READERS.values()],
    })


@needs_login()
async def research(request: Request):
    rows = store.rows("SELECT * FROM research ORDER BY created_at DESC LIMIT 500")
    out = [{**{k: r[k] for k in ("test_id", "created_at", "reader_id", "firmware", "reagent_lot", "age_band", "sex",
                                 "device_group", "lab_group")}, "simulated": bool(r["simulated"]),
            "features": from_json(r["features"], {}), "readings": len(test_readings(r["test_id"]))} for r in rows]
    return JSONResponse({"research": out})


@needs_login()
async def research_csv(request: Request):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["test_id", "created_at", "reader_id", "firmware", "simulated", "reagent_lot", "age_band", "sex",
                "t_s", "z_antiA_ohm", "z_antiB_ohm", "z_antiD_ohm", "re_antiA", "im_antiA", "re_antiB", "im_antiB",
                "re_antiD", "im_antiD", "device_group", "lab_group"])
    for r in store.rows("SELECT * FROM research ORDER BY created_at"):
        for x in test_readings(r["test_id"]):
            w.writerow([r["test_id"], r["created_at"], r["reader_id"], r["firmware"], r["simulated"], r["reagent_lot"],
                        r["age_band"], r["sex"], x["t_ms"] / 1000, *x["z"], x["re"][0], x["im"][0], x["re"][1],
                        x["im"][1], x["re"][2], x["im"][2], r["device_group"], r["lab_group"]])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=hemosense-research.csv"})


@needs_login(admin=True)
async def export_all(request: Request):
    rows = store.rows(TEST_LIST_SQL + " ORDER BY t.saved_at")
    return JSONResponse({"exported_at": now(), "settings": settings(),
                         "tests": [{**test_view(t), "readings": test_readings(t["id"])} for t in rows]},
                        headers={"Content-Disposition": "attachment; filename=hemosense-export.json"})


async def health(request: Request):
    return JSONResponse({"ok": True, "time": now(), "readers_online": len(READERS), "database": "postgres" if store.is_pg else "sqlite"})


async def index(request: Request):
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


# ------------------------------------------------------------------ security headers and start-up
class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


@contextlib.asynccontextmanager
async def lifespan(app):
    global store
    store = Store()
    if not store.row("SELECT id FROM users LIMIT 1"):
        store.run("INSERT INTO users (id, username, full_name, password_hash, role, created_at) VALUES (:i, :u, 'Administrator', :p, 'admin', :c)",
                  i=str(uuid.uuid4()), u=ADMIN_USER.lower(), p=hash_password(ADMIN_PASS), c=now())
        log.info("Created admin user %r", ADMIN_USER)
    log.info("HemoSense ready (%s)", "PostgreSQL" if store.is_pg else "SQLite")
    yield


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/health", health),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/me", me),
        Route("/api/users", users, methods=["GET", "POST"]),
        Route("/api/settings", app_settings, methods=["GET", "PUT"]),
        Route("/api/overview", overview),
        Route("/api/patients", patients, methods=["GET", "POST"]),
        Route("/api/patients/find", find_patient),
        Route("/api/patients/emergency-id", emergency_id, methods=["POST"]),
        Route("/api/patients/{pid}", patient_detail),
        Route("/api/cartridges/{cid}", cartridge_check),
        Route("/api/tests", tests, methods=["GET", "POST"]),
        Route("/api/tests/{tid}", test_detail),
        Route("/api/tests/{tid}/stop", stop_test, methods=["POST"]),
        Route("/api/tests/{tid}/save", save_test, methods=["POST"]),
        Route("/api/tests/{tid}/lab", lab_result, methods=["PATCH"]),
        Route("/api/readers/{rid}/calibrate", calibrate, methods=["POST"]),
        Route("/api/research", research),
        Route("/api/research.csv", research_csv),
        Route("/api/export.json", export_all),
        WebSocketRoute("/ws/reader", reader_socket),
        WebSocketRoute("/ws/app", app_socket),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ],
    middleware=[Middleware(SecurityHeaders)],
    lifespan=lifespan,
)
