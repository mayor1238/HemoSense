"""End-to-end test of the back end without a network.
Run:  cd backend && python -m pytest -q
"""
import asyncio
import json
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DEVICE_KEY"] = "k1"

import app as server  # noqa: E402


async def call(method, path, body=None, token=None):
    headers = [(b"content-type", b"application/json")]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    p, _, qs = path.partition("?")
    scope = {"type": "http", "method": method, "path": p, "raw_path": p.encode(), "query_string": qs.encode(),
             "headers": headers, "scheme": "http", "server": ("t", 80), "client": ("1.2.3.4", 1), "root_path": "",
             "http_version": "1.1"}
    data, sent, out = json.dumps(body).encode() if body is not None else b"", False, {"status": 0, "body": b""}

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": data, "more_body": False}
        await asyncio.sleep(60)

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
            out["headers"] = dict(m["headers"])
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")

    await server.app(scope, receive, send)
    try:
        return out["status"], json.loads(out["body"] or b"null")
    except ValueError:
        return out["status"], out["body"].decode()


class Socket:
    def __init__(self, path):
        p, _, qs = path.partition("?")
        self.inbox, self.outbox = asyncio.Queue(), asyncio.Queue()
        self.inbox.put_nowait({"type": "websocket.connect"})
        scope = {"type": "websocket", "path": p, "raw_path": p.encode(), "query_string": qs.encode(), "headers": [],
                 "scheme": "ws", "server": ("t", 80), "client": ("1.2.3.4", 2), "root_path": "", "subprotocols": []}
        self.task = asyncio.create_task(server.app(scope, self.inbox.get, self.outbox.put))

    def send(self, obj):
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(obj)})

    async def recv(self, kind=None):
        while True:
            m = await asyncio.wait_for(self.outbox.get(), 2)
            if m["type"] == "websocket.send":
                d = json.loads(m["text"])
                if kind is None or d.get("type") == kind:
                    return d
            elif m["type"] == "websocket.close":
                return {"type": "closed", "code": m.get("code")}

    async def close(self):
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, 2)


async def flow():
    async with server.lifespan(server.app):
        st, _ = await call("GET", "/api/health")
        assert st == 200
        st, r = await call("POST", "/api/login", {"username": "admin", "password": "nope"})
        assert st == 401
        st, r = await call("POST", "/api/login", {"username": "admin", "password": "hemosense-admin"})
        tok = r["token"]
        st, r = await call("GET", "/api/overview")
        assert st == 401

        bad = Socket("/ws/reader?device_id=HS-DEV-001&key=wrong")
        assert (await bad.recv())["type"] == "error"
        assert (await bad.recv())["type"] == "closed"

        app_ws = Socket("/ws/app?token=" + tok)
        assert (await app_ws.recv("readers"))["readers"] == []
        rd = Socket("/ws/reader?device_id=HS-DEV-001&key=k1")
        assert (await rd.recv())["type"] == "hello"
        rd.send({"type": "info", "firmware": "0.6.1-wifi-10k", "simulate": False, "sensor_ok": True, "calibrated": False, "state": "idle"})
        while True:
            m = await app_ws.recv("readers")
            if m["readers"] and m["readers"][0]["firmware"]:
                break
        assert m["readers"][0]["calibrated"] is False

        st, r = await call("POST", "/api/patients", {"id_type": "hospital", "id_value": "OOUTH-7", "full_name": "Bola Ade", "dob": "1998-02-03", "sex": "Female"}, tok)
        assert st == 200 and r["created"]
        pid = r["patient"]["id"]
        st, r = await call("POST", "/api/patients", {"id_type": "hospital", "id_value": "oouth-7", "full_name": "Bola Adebayo", "dob": "1998-02-03"}, tok)
        assert st == 409

        tid = str(uuid.uuid4())
        start = {"test_id": tid, "patient_id": pid, "care_consent": True, "research_consent": True, "cartridge_id": "CT-1",
                 "reagent_lot": "RL-9", "operator": "NUR-1", "reader_id": "HS-DEV-001", "sweep_s": 30, "interval_s": 2}
        st, r = await call("POST", "/api/tests", start, tok)
        assert st == 409 and "Calibrate" in r["error"]

        st, r = await call("POST", "/api/readers/HS-DEV-001/calibrate", {}, tok)
        assert (await rd.recv("calibrate"))
        rd.send({"type": "calibrated", "ok": True, "gain": 1e-9})
        await app_ws.recv("calibrated")

        st, r = await call("POST", "/api/tests", start, tok)
        assert st == 200, r
        cmd = await rd.recv("start")
        assert cmd["test_id"] == tid and cmd["duration_s"] == 30
        rd.send({"type": "ack", "cmd": "start", "test_id": tid})
        await app_ws.recv("ack")
        base = [500, 600, 700]
        for i in range(16):  # A and D rise 12%, B flat  -> A+
            f = min(max(i - 6, 0), 6) / 6  # rises after the settling readings, like a real reaction
            z = [base[0] * (1 + 0.12 * f), base[1] * (1 + 0.004 * (i % 2)), base[2] * (1 + 0.12 * f)]
            rd.send({"type": "reading", "test_id": tid, "i": i, "t_ms": i * 2000, "z": z, "re": [1, 1, 1], "im": [0, 0, 0]})
        for _ in range(16):
            await app_ws.recv("reading")
        rd.send({"type": "done", "test_id": tid, "n": 16, "reason": "complete"})
        done = await app_ws.recv("done")
        assert done["analysis"]["valid"] and done["analysis"]["group"] == "A+", done["analysis"]

        st, r = await call("POST", "/api/tests", {**start, "test_id": str(uuid.uuid4())}, tok)
        assert st == 409 and "single use" in r["error"]
        st, r = await call("POST", f"/api/tests/{tid}/save", {}, tok)
        assert st == 400
        st, r = await call("POST", f"/api/tests/{tid}/save", {"identity_checked": True}, tok)
        assert st == 200 and r["test"]["group"] == "A+", r
        st, r = await call("PATCH", f"/api/tests/{tid}/lab", {"group": "A+"}, tok)
        assert st == 200
        st, r = await call("GET", "/api/patients/find?id_type=hospital&id_value=OOUTH-7", None, tok)
        assert r["patient"]["last"]["group"] == "A+" and r["patient"]["last"]["lab_confirmed"]
        st, r = await call("GET", f"/api/patients/{pid}", None, tok)
        assert len(r["tests"]) == 1
        st, r = await call("GET", "/api/tests?group=A%2B", None, tok)
        assert len(r["tests"]) == 1
        st, r = await call("GET", "/api/overview", None, tok)
        assert r["tests_total"] == 1 and r["lab_agreement"] == 100 and r["by_group"]["A+"] == 1
        st, r = await call("GET", "/api/research", None, tok)
        assert r["research"][0]["age_band"] == "18–29" and "full_name" not in json.dumps(r)
        st, r = await call("GET", "/api/research.csv", None, tok)
        assert r.count("\n") == 17
        st, r = await call("GET", "/api/export.json", None, tok)
        assert st == 200 and len(r["tests"][0]["readings"]) == 16
        exported = r
        st, r = await call("POST", "/api/import", exported, tok)
        assert st == 200 and r["added"] == 0 and r["already_here"] == 1, r
        moved = json.loads(json.dumps(exported))
        moved["tests"][0]["id"] = str(uuid.uuid4())
        moved["tests"][0]["cartridge_id"] = "CT-LAPTOP"
        moved["tests"][0]["patient"]["id_value"] = "LAPTOP-9"
        st, r = await call("POST", "/api/import", moved, tok)
        assert st == 200 and r["added"] == 1 and not r["problems"], r
        st, r = await call("GET", "/api/patients/find?id_type=hospital&id_value=LAPTOP-9", None, tok)
        assert r["patient"]["last"]["group"] == "A+"
        st, r = await call("GET", f"/api/tests/{moved['tests'][0]['id']}", None, tok)
        assert len(r["readings"]) == 16 and r["analysis"]["group"] == "A+"
        st, r = await call("GET", "/api/research", None, tok)
        assert len(r["research"]) == 2

        # reader restarts mid-test -> interrupted
        t2 = str(uuid.uuid4())
        st, r = await call("POST", "/api/tests", {**start, "test_id": t2, "cartridge_id": "CT-2"}, tok)
        await rd.recv("start")
        rd.send({"type": "ack", "cmd": "start", "test_id": t2})
        await app_ws.recv("ack")
        await rd.close()
        rd2 = Socket("/ws/reader?device_id=HS-DEV-001&key=k1")
        await rd2.recv("hello")
        rd2.send({"type": "info", "state": "idle", "calibrated": True})
        d = await app_ws.recv("done")
        assert d["test_id"] == t2 and d["status"] == "interrupted"

        # settings and users
        st, r = await call("PUT", "/api/settings", {"threshold": 10, "method": "range"}, tok)
        assert st == 200 and r["threshold"] == 10.0 and r["method"] == "range"
        st, r = await call("POST", "/api/users", {"username": "nurse.ada", "full_name": "Ada", "password": "longpassword"}, tok)
        assert st == 200
        st, r = await call("POST", "/api/login", {"username": "nurse.ada", "password": "longpassword"})
        st, r = await call("PUT", "/api/settings", {"threshold": 3}, r["token"])
        assert st == 403

        await rd2.close()
        await app_ws.close()


def test_flow():
    asyncio.run(flow())
