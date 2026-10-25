#!/usr/bin/env python3
import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

APP_DIR = Path("/app")
DATA_DIR = Path("/data")
OPTIONS_FILE = DATA_DIR / "options.json"
LAYOUT_FILE = DATA_DIR / "dashboard.json"
VERSION = "0.1.5"


def options():
    value = {"deye_bridge_url": "http://deye_ai_bridge:8099"}
    try:
        value.update(json.loads(OPTIONS_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return value


def request_json(url, method="GET", data=None, headers=None, timeout=10):
    payload = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=payload, method=method,
                                 headers=headers or {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode())


def ha(path, method="GET", data=None):
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    return request_json("http://supervisor/core/api" + path, method, data,
                        {"Authorization": "Bearer " + token, "Content-Type": "application/json"})


def entity_list():
    areas = {}
    try:
        template = ("{% set ns=namespace(items=[]) %}{% for s in states %}"
                    "{% set ns.items=ns.items + [dict(entity_id=s.entity_id, area=area_name(s.entity_id))] %}"
                    "{% endfor %}{{ ns.items | tojson }}")
        for row in ha("/template", "POST", {"template": template}):
            if row.get("area"):
                areas[row["entity_id"]] = row["area"]
    except Exception:
        pass
    allowed = {"light", "switch", "input_boolean", "sensor", "binary_sensor",
               "weather", "climate", "cover", "lock", "fan"}
    result = []
    for item in ha("/states"):
        entity_id = item.get("entity_id", "")
        domain = entity_id.split(".", 1)[0]
        if domain not in allowed:
            continue
        attrs = item.get("attributes", {})
        result.append({
            "entity_id": entity_id, "domain": domain, "state": item.get("state"),
            "name": attrs.get("friendly_name") or entity_id, "unit": attrs.get("unit_of_measurement"),
            "device_class": attrs.get("device_class"), "icon": attrs.get("icon"),
            "temperature": attrs.get("temperature"), "humidity": attrs.get("humidity"),
            "brightness": attrs.get("brightness"), "rgb_color": attrs.get("rgb_color"),
            "supported_color_modes": attrs.get("supported_color_modes", []),
            "area": areas.get(entity_id),
        })
    return sorted(result, key=lambda x: (str(x.get("area") or ""), x["domain"], str(x["name"]).lower()))


def layout():
    try:
        value = json.loads(LAYOUT_FILE.read_text())
        if isinstance(value.get("widgets"), list):
            return value
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        pass
    return {"widgets": [
        {"id": "energy", "type": "energy", "size": "wide", "title": "Energiflow"},
        {"id": "price", "type": "price", "size": "normal", "title": "Elpris"},
        {"id": "solar", "type": "solar", "size": "normal", "title": "Solprognose"},
        {"id": "clock", "type": "clock", "size": "normal", "title": "Tid"},
    ]}


def save_layout(data):
    rows = data.get("widgets", [])
    if not isinstance(rows, list) or len(rows) > 60:
        raise ValueError("Der kan højst gemmes 60 widgets")
    allowed = {"entity", "energy", "price", "solar", "clock"}
    clean = []
    for number, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("type") not in allowed:
            continue
        entity_id = str(row.get("entity_id", ""))[:160]
        if row["type"] == "entity" and not re.match(r"^[a-z_]+\.[a-z0-9_]+$", entity_id):
            continue
        clean.append({"id": str(row.get("id") or "w%d" % number)[:80], "type": row["type"],
                      "entity_id": entity_id, "title": str(row.get("title", ""))[:100],
                      "size": "wide" if row.get("size") == "wide" else "normal"})
    saved = {"widgets": clean, "updated": datetime.now(timezone.utc).isoformat()}
    LAYOUT_FILE.write_text(json.dumps(saved, ensure_ascii=False, indent=2))
    return saved


def control(data):
    entity_id = str(data.get("entity_id", ""))
    domain = entity_id.split(".", 1)[0]
    action = str(data.get("action", ""))
    allowed = {
        "light": {"turn_on", "turn_off", "toggle"}, "switch": {"turn_on", "turn_off", "toggle"},
        "input_boolean": {"turn_on", "turn_off", "toggle"}, "fan": {"turn_on", "turn_off", "toggle"},
        "cover": {"open_cover", "close_cover", "stop_cover"}, "lock": {"lock", "unlock"},
    }
    if domain not in allowed or action not in allowed[domain] or not re.match(r"^[a-z_]+\.[a-z0-9_]+$", entity_id):
        raise ValueError("Handlingen er ikke tilladt")
    payload = {"entity_id": entity_id}
    if domain == "light" and action == "turn_on":
        if data.get("brightness_pct") is not None:
            payload["brightness_pct"] = max(1, min(100, int(data["brightness_pct"])))
        rgb = data.get("rgb_color")
        if isinstance(rgb, list) and len(rgb) == 3:
            payload["rgb_color"] = [max(0, min(255, int(x))) for x in rgb]
    ha("/services/%s/%s" % (domain, action), "POST", payload)
    return {"ok": True, "executed": True}


def deye_data():
    configured = options()["deye_bridge_url"].rstrip("/")
    candidates = [configured, "http://30046a15-deye-ai-bridge:8099",
                  "http://30046a15_deye_ai_bridge:8099", "http://deye_ai_bridge:8099"]
    errors = []
    for base in dict.fromkeys(candidates):
        try:
            return {"connected": True, "source": base,
                    "data": request_json(base + "/api/shadow", timeout=6)}
        except Exception as exc:
            errors.append("%s: %s" % (base, exc))
    return {"connected": False, "error": " | ".join(errors), "data": None}


class Handler(BaseHTTPRequestHandler):
    def json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        try:
            path = urlparse(self.path).path.rstrip("/")
            if path.endswith("/api/entities"): return self.json(entity_list())
            if path.endswith("/api/layout"): return self.json(layout())
            if path.endswith("/api/deye"): return self.json(deye_data())
            if path.endswith("/api/status"): return self.json({"version": VERSION, "ha": True})
            if path.endswith("/tesla_icon.jpeg"):
                body = (APP_DIR / "tesla_icon.jpeg").read_bytes()
                self.send_response(200); self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                return
            body = (APP_DIR / "index.html").read_bytes()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        except Exception as exc: self.json({"error": str(exc)}, 500)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            path = urlparse(self.path).path.rstrip("/")
            if path.endswith("/api/layout"): return self.json(save_layout(body))
            if path.endswith("/api/control"): return self.json(control(body))
            self.json({"error": "Ukendt handling"}, 404)
        except ValueError as exc: self.json({"error": str(exc)}, 400)
        except Exception as exc: self.json({"error": str(exc)}, 502)

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", 8100), Handler).serve_forever()
