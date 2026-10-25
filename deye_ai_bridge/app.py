#!/usr/bin/env python3
import json
import html
import io
import os
import re
import socket
import sqlite3
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

APP_DIR = Path("/app")
DATA_DIR = Path("/data")
OPTIONS_FILE = DATA_DIR / "options.json"
SNAPSHOT_FILE = DATA_DIR / "last_snapshot.json"
PLAN_FILE = DATA_DIR / "energy_plan.json"
TESLA_REMINDER_FILE = DATA_DIR / "tesla_reminder.json"
DB_FILE = DATA_DIR / "shadow.db"
FULL_SNAPSHOT_FILE = DATA_DIR / "full_snapshot.json"
CACHE = {
    "prices": None, "weather": None, "price_updated": 0, "weather_updated": 0,
    "price_source": None, "price_error": None, "weather_error": None,
}
APP_VERSION = "0.6.8"

TESLA_ENTITIES = {
    "tesla_connected": "binary_sensor.tessi_opladerkabel",
    "tesla_online": "binary_sensor.tessi_status",
    "tesla_soc": "sensor.tessi_battery_level",
    "tesla_charging_state": "sensor.tessi_charging",
    "tesla_charger_power_kw": "sensor.tessi_charger_power",
    "tesla_charger_current_a": "sensor.tessi_charger_current",
    "tesla_charger_voltage_v": "sensor.tessi_charger_voltage",
    "tesla_charge_energy_added_kwh": "sensor.tessi_charge_energy_added",
    "tesla_charge_limit": "number.tessi_charge_limit",
    "tesla_charge_current_limit_a": "number.tessi_charge_current",
    "tesla_charge_switch": "switch.tessi_charge",
    "tesla_time_to_full": "sensor.tessi_time_to_full_charge",
}

REGISTER_MAP = {
    117: ("Minimum batteri-SOC", "%", 1, False),
    141: ("Energy management bitfelt", "", 1, False),
    142: ("Grid/eksport-mode", "", 1, False),
    143: ("Maksimal nettilsluttet effekt", "W", 1, False),
    145: ("Solar Sell", "", 1, False),
    146: ("Time of Use + ugedage", "", 1, False),
    340: ("Maksimal PV-salgseffekt", "W", 1, False),
    514: ("Batteriopladning i dag", "kWh", 0.1, False),
    515: ("Batteriafladning i dag", "kWh", 0.1, False),
    520: ("Netimport i dag", "kWh", 0.1, False),
    521: ("Neteksport i dag", "kWh", 0.1, False),
    526: ("Forbrug i dag", "kWh", 0.1, False),
    529: ("Solproduktion i dag", "kWh", 0.1, False),
    587: ("Batterispænding", "V", 0.01, False),
    588: ("Batteri-SOC", "%", 1, False),
    590: ("Batterieffekt", "W", 1, True),
    591: ("Batteristrøm", "A", 0.01, True),
    609: ("Netfrekvens", "Hz", 0.01, False),
    604: ("Intern neteffekt L1", "W", 1, True),
    605: ("Intern neteffekt L2", "W", 1, True),
    606: ("Intern neteffekt L3", "W", 1, True),
    607: ("Samlet intern neteffekt", "W", 1, True),
    616: ("Neteffekt L1", "W", 1, True),
    617: ("Neteffekt L2", "W", 1, True),
    618: ("Neteffekt L3", "W", 1, True),
    619: ("Samlet neteffekt", "W", 1, True),
    622: ("Valgt neteffekt L1", "W", 1, True),
    623: ("Valgt neteffekt L2", "W", 1, True),
    624: ("Valgt neteffekt L3", "W", 1, True),
    625: ("Samlet valgt neteffekt", "W", 1, True),
    633: ("Invertereffekt L1", "W", 1, True),
    634: ("Invertereffekt L2", "W", 1, True),
    635: ("Invertereffekt L3", "W", 1, True),
    636: ("Samlet invertereffekt", "W", 1, True),
    650: ("Belastningseffekt L1", "W", 1, True),
    651: ("Belastningseffekt L2", "W", 1, True),
    652: ("Belastningseffekt L3", "W", 1, True),
    653: ("Samlet belastningseffekt", "W", 1, True),
    672: ("PV1-effekt", "W", 1, False),
    673: ("PV2-effekt", "W", 1, False),
    676: ("PV1-spænding", "V", 0.1, False),
    678: ("PV2-spænding", "V", 0.1, False),
}


def options():
    defaults = {
        "deye_host": "DIN_DEYE_IP",
        "deye_port": 502,
        "slave_id": 1,
        "timeout_seconds": 3,
        "read_only": True,
        "price_area": "DK1",
        "sample_interval_seconds": 60,
        "solar_capacity_kwp": 10,
        "solar_tilt_degrees": 30,
        "solar_azimuth_degrees": 90,
        "solar_performance_percent": 85,
    }
    try:
        defaults.update(json.loads(OPTIONS_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return defaults


def ha_request(path, method="GET", data=None):
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    payload = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        "http://supervisor/core/api" + path,
        data=payload,
        method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=8) as response:
        return json.loads(response.read().decode())


def deye_entities():
    states = ha_request("/states")
    result = []
    for item in states:
        entity_id = item.get("entity_id", "")
        name = str(item.get("attributes", {}).get("friendly_name", ""))
        if "deye" in entity_id.lower() or "deye" in name.lower():
            result.append({
                "entity_id": entity_id,
                "state": item.get("state"),
                "unit": item.get("attributes", {}).get("unit_of_measurement"),
                "name": name,
            })
    return result


def notification_services():
    """Return Home Assistant notify services so the phone can be selected in the UI."""
    try:
        domains = ha_request("/services")
        notify = next((item for item in domains if item.get("domain") == "notify"), {})
        result = []
        for service, details in sorted((notify.get("services") or {}).items()):
            if service in ("persistent_notification",):
                continue
            fields = details or {}
            result.append({"service": service,
                           "name": fields.get("name") or service.replace("_", " ").title()})
        return result
    except Exception:
        return []


def send_ha_notification(service, title, message):
    """Send to the selected Companion App, falling back to HA's notification centre."""
    if service and re.fullmatch(r"[a-z0-9_]+", str(service)):
        return ha_request("/services/notify/" + str(service), "POST",
                          {"title": title, "message": message})
    return ha_request("/services/persistent_notification/create", "POST",
                      {"title": title, "message": message,
                       "notification_id": "deye_ai_tesla_charge_reminder"})


def tesla_state():
    """Read the verified Tesla Fleet entities from Home Assistant without writing."""
    try:
        states = {item.get("entity_id"): item for item in ha_request("/states")}
    except Exception as exc:
        return {"available": False, "error": str(exc)}

    result = {"available": False, "error": None}
    numeric = {
        "tesla_soc", "tesla_charger_power_kw", "tesla_charger_current_a",
        "tesla_charger_voltage_v", "tesla_charge_energy_added_kwh",
        "tesla_charge_limit", "tesla_charge_current_limit_a",
    }
    for key, entity_id in TESLA_ENTITIES.items():
        item = states.get(entity_id)
        value = item.get("state") if item else None
        if key in numeric:
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
        elif key in ("tesla_connected", "tesla_online"):
            value = value == "on"
        result[key] = value
        result[key + "_entity_id"] = entity_id
    result["available"] = result.get("tesla_soc") is not None
    return result


def read_holding(start, count):
    cfg = options()
    if count < 1 or count > 125 or start < 0 or start > 65535 or start + count > 65536:
        raise ValueError("Ugyldigt registerområde")
    transaction = int(time.time() * 1000) & 0xFFFF
    pdu = struct.pack(">BHH", 3, start, count)
    packet = struct.pack(">HHHB", transaction, 0, len(pdu) + 1, int(cfg["slave_id"])) + pdu
    with socket.create_connection(
        (str(cfg["deye_host"]), int(cfg["deye_port"])),
        timeout=int(cfg["timeout_seconds"]),
    ) as sock:
        sock.sendall(packet)
        header = recv_exact(sock, 7)
        rx_transaction, protocol, length, unit = struct.unpack(">HHHB", header)
        body = recv_exact(sock, length - 1)
    if rx_transaction != transaction or protocol != 0 or unit != int(cfg["slave_id"]):
        raise RuntimeError("Uventet Modbus-svar")
    function = body[0]
    if function & 0x80:
        raise RuntimeError("Modbus-fejl kode %d" % body[1])
    if function != 3 or body[1] != count * 2:
        raise RuntimeError("Ugyldig Modbus-svarlængde")
    values = list(struct.unpack(">" + "H" * count, body[2:]))
    result = []
    for i, v in enumerate(values):
        address = start + i
        signed_value = v - 65536 if v > 32767 else v
        item = {"address": address, "uint16": v, "int16": signed_value, "hex": "0x%04X" % v}
        if address in REGISTER_MAP:
            name, unit, scale, signed = REGISTER_MAP[address]
            raw = signed_value if signed else v
            item.update({"name": name, "unit": unit, "scale": scale,
                         "value": round(raw * scale, 3), "known": True})
        else:
            item.update({"name": "Ukendt", "unit": "", "scale": 1, "value": v, "known": False})
        result.append(item)
    return result


def energy_plan():
    defaults = {
        "target_soc": 99, "minimum_soc_morning": 20, "morning_time": "07:00",
        "evening_start": "16:00", "max_charge_w": 8000,
        "max_export_w": 0, "allow_battery_sale": False,
        "main_fuse_ampere": 35, "automatic_control": False,
        "battery_capacity_kwh": 32.0, "max_discharge_w": 8000,
        "roundtrip_efficiency_percent": 90,
        "tesla_target_soc": 80, "tesla_departure_time": "07:00",
        "tesla_arrival_time": "16:00", "tesla_battery_capacity_kwh": 75.0,
        "tesla_charge_power_kw": 11.0, "tesla_notify_service": "",
        "tesla_shadow_mode": True,
    }
    try:
        defaults.update(json.loads(PLAN_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return defaults


def save_energy_plan(data):
    plan = energy_plan()
    allowed = set(plan)
    plan.update({k: v for k, v in data.items() if k in allowed})
    plan["target_soc"] = max(10, min(100, int(plan["target_soc"])))
    plan["minimum_soc_morning"] = max(10, min(100, int(plan["minimum_soc_morning"])))
    plan["max_charge_w"] = max(0, min(12000, int(plan["max_charge_w"])))
    plan["max_export_w"] = max(0, min(12000, int(plan["max_export_w"])))
    plan["battery_capacity_kwh"] = max(5, min(100, float(plan["battery_capacity_kwh"])))
    plan["max_discharge_w"] = max(0, min(12000, int(plan["max_discharge_w"])))
    plan["roundtrip_efficiency_percent"] = max(70, min(100, int(plan["roundtrip_efficiency_percent"])))
    plan["tesla_target_soc"] = max(50, min(80, int(plan["tesla_target_soc"])))
    for key, fallback in (("tesla_departure_time", "07:00"), ("tesla_arrival_time", "16:00")):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(plan[key])):
            plan[key] = fallback
    plan["tesla_battery_capacity_kwh"] = max(40, min(100, float(plan["tesla_battery_capacity_kwh"])))
    plan["tesla_charge_power_kw"] = max(2.0, min(22.0, float(plan["tesla_charge_power_kw"])))
    service = str(plan.get("tesla_notify_service") or "").strip()
    plan["tesla_notify_service"] = service if re.fullmatch(r"[a-z0-9_]*", service) else ""
    plan["tesla_shadow_mode"] = True
    plan["main_fuse_ampere"] = 35
    plan["automatic_control"] = False
    PLAN_FILE.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    return plan


def db():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS samples (
          id INTEGER PRIMARY KEY, created TEXT NOT NULL, soc REAL, battery_w REAL,
          grid_w REAL, grid_l1_w REAL, grid_l2_w REAL, grid_l3_w REAL,
          load_w REAL, pv_w REAL, pv1_w REAL, pv2_w REAL,
          battery_charge_today REAL, battery_discharge_today REAL,
          grid_import_today REAL, grid_export_today REAL,
          load_today REAL, pv_today REAL, reg117 INTEGER, reg141 INTEGER,
          reg142 INTEGER, reg145 INTEGER, reg146 INTEGER, tou_json TEXT,
          price_dkk REAL, solar_tomorrow REAL);
        CREATE TABLE IF NOT EXISTS decisions (
          id INTEGER PRIMARY KEY, created TEXT NOT NULL, action TEXT NOT NULL,
          title TEXT NOT NULL, explanation TEXT NOT NULL, details_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', decided TEXT);
        """)
        sample_columns = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
        for column in ("grid_l1_w", "grid_l2_w", "grid_l3_w", "pv1_w", "pv2_w",
                       "battery_charge_today", "battery_discharge_today",
                       "grid_import_today", "grid_export_today", "load_today", "pv_today",
                       "tesla_soc", "tesla_charger_power_kw", "tesla_charger_current_a",
                       "tesla_charger_voltage_v", "tesla_charge_energy_added_kwh",
                       "tesla_charge_limit", "tesla_charge_current_limit_a"):
            if column not in sample_columns:
                conn.execute(f"ALTER TABLE samples ADD COLUMN {column} REAL")
        for column in ("tesla_connected", "tesla_online"):
            if column not in sample_columns:
                conn.execute(f"ALTER TABLE samples ADD COLUMN {column} INTEGER")
        for column in ("tesla_charging_state", "tesla_charge_switch", "tesla_time_to_full"):
            if column not in sample_columns:
                conn.execute(f"ALTER TABLE samples ADD COLUMN {column} TEXT")


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Deye-AI-Bridge/0.3"})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode())


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Deye-AI-Bridge/0.5.3"})
    with urllib.request.urlopen(req, timeout=25) as response:
        return response.read().decode("utf-8", "ignore")


def n1_total_price(energy_price, stamp):
    """Add 2026 N1 C-customer and national variable tariffs, all incl. VAT.

    Fixed monthly subscriptions are intentionally excluded because they do not
    change which hour is cheapest.
    """
    winter = stamp.month in (10, 11, 12, 1, 2, 3)
    hour = stamp.hour
    if hour < 6:
        n1_tariff = 0.1098
        band = "lavlast"
    elif 17 <= hour < 21:
        n1_tariff = 0.988 if winter else 0.4283
        band = "spidslast"
    else:
        n1_tariff = 0.330 if winter else 0.165
        band = "højlast"
    energinet_tariffs = 0.14375  # 4.3 + 7.2 øre/kWh ex VAT
    electricity_tax = 0.0100     # 0.8 øre/kWh ex VAT
    total = float(energy_price) + n1_tariff + energinet_tariffs + electricity_tax
    return round(total, 4), round(n1_tariff, 4), band


def andel_flex_prices(area):
    page = fetch_text("https://andelenergi.dk/el/timepris/")
    match = re.search(r'\bdata-chart="([^"]+)"', page)
    if not match:
        raise RuntimeError("Andels prisdata blev ikke fundet på siden")
    chart = json.loads(html.unescape(match.group(1)))
    region = chart["west" if area == "DK1" else "east"]
    start = datetime.fromisoformat(region["firstDateTime"].replace("Z", "+00:00"))
    local_tz = ZoneInfo("Europe/Copenhagen")
    prices = []
    for index, value in enumerate(region.get("values", [])):
        stamp = (start + timedelta(hours=index)).astimezone(local_tz)
        energy_price = float(value)
        total_price, grid_tariff, tariff_band = n1_total_price(energy_price, stamp)
        prices.append({
            "time": stamp.isoformat(),
            "price_dkk_kwh": total_price,
            "energy_price_dkk_kwh": round(energy_price, 4),
            "grid_tariff_dkk_kwh": grid_tariff,
            "tariff_band": tariff_band,
            "source": "Andel FlexEnergi + N1 og afgifter",
        })
    if not prices:
        raise RuntimeError("Andel returnerede ingen FlexEnergi-priser")
    return prices


def fallback_spot_prices(area, local_today):
    prices = []
    # This small public JSON endpoint is deliberately kept as a second source.
    for day_offset in (-1, 0, 1, 2):
        day = local_today + timedelta(days=day_offset)
        url = "https://www.elprisenligenu.dk/api/v1/prices/%04d/%02d-%02d_%s.json" % (
            day.year, day.month, day.day, area)
        try:
            rows = fetch_json(url)
        except Exception:
            continue
        for row in rows:
            prices.append({
                "time": row.get("time_start"),
                "price_dkk_kwh": round(float(row.get("DKK_per_kWh", 0)), 4),
                "source": "Offentlig spotpris (reserve)",
            })
    if not prices:
        raise RuntimeError("Reservekilden returnerede ingen spotpriser")
    return prices


def load_prices(force=False):
    if not force and CACHE["prices"] and time.time() - CACHE["price_updated"] < 1800:
        return CACHE["prices"]
    cfg = options()
    area = cfg.get("price_area", "DK1")
    copenhagen = ZoneInfo("Europe/Copenhagen")
    local_today = datetime.now(copenhagen).date()
    try:
        prices = andel_flex_prices(area)
        source, error = "Andel FlexEnergi + N1 og afgifter", None
    except Exception as andel_error:
        prices = fallback_spot_prices(area, local_today)
        source, error = "Offentlig spotpris (reserve)", str(andel_error)
    CACHE.update({"prices": prices, "price_updated": time.time(),
                  "price_source": source, "price_error": error})
    return prices


def load_weather(force=False):
    if not force and CACHE["weather"] and time.time() - CACHE["weather_updated"] < 1800:
        return CACHE["weather"]
    cfg = options()
    ha_cfg = ha_request("/config")
    lat, lon = ha_cfg.get("latitude"), ha_cfg.get("longitude")
    wparams = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon, "timezone": "Europe/Copenhagen",
        "forecast_days": 3,
        "hourly": "global_tilted_irradiance,cloud_cover,precipitation,temperature_2m",
        "daily": "shortwave_radiation_sum,precipitation_sum",
        "tilt": cfg.get("solar_tilt_degrees", 30),
        "azimuth": cfg.get("solar_azimuth_degrees", 90),
    })
    weather = fetch_json("https://api.open-meteo.com/v1/forecast?" + wparams)
    CACHE.update({"weather": weather, "weather_updated": time.time(), "weather_error": None})
    return weather


def market_data(force=False):
    # Price and weather are intentionally independent: one failing must not hide the other.
    price_error = weather_error = None
    try:
        prices = load_prices(force)
    except Exception as exc:
        prices, price_error = CACHE.get("prices") or [], str(exc)
    try:
        weather = load_weather(force)
    except Exception as exc:
        weather, weather_error = CACHE.get("weather") or {}, str(exc)
    CACHE["price_error"] = price_error or CACHE.get("price_error")
    CACHE["weather_error"] = weather_error
    return prices, weather


def current_price(prices):
    now = datetime.now(ZoneInfo("Europe/Copenhagen")).replace(minute=0, second=0, microsecond=0)
    for row in prices:
        try:
            stamp = datetime.fromisoformat(row["time"])
            if stamp.replace(tzinfo=None) == now.replace(tzinfo=None):
                return row["price_dkk_kwh"]
        except Exception:
            continue
    return None


def solar_forecast(weather, actual_today=None):
    cfg = options()
    hourly = weather.get("hourly", {}) if weather else {}
    times = hourly.get("time", [])
    radiation = hourly.get("global_tilted_irradiance", [])
    factor = float(cfg.get("solar_capacity_kwp", 10)) * float(cfg.get("solar_performance_percent", 85)) / 100000.0
    days, remaining = {}, {}
    local_now = datetime.now(ZoneInfo("Europe/Copenhagen")).replace(tzinfo=None)
    for stamp, value in zip(times, radiation):
        day = str(stamp)[:10]
        energy = max(0.0, float(value or 0)) * factor
        days[day] = days.get(day, 0.0) + energy
        try:
            if datetime.fromisoformat(str(stamp)) > local_now:
                remaining[day] = remaining.get(day, 0.0) + energy
        except ValueError:
            pass
    today = local_now.date().isoformat()
    result = []
    for day, model_total in sorted(days.items()):
        if day == today and actual_today is not None:
            measured = max(0.0, float(actual_today))
            future = remaining.get(day, 0.0)
            expected = measured + future
            result.append({"date": day, "energy_kwh": round(expected, 1),
                           "measured_kwh": round(measured, 1),
                           "remaining_kwh": round(future, 1),
                           "model_total_kwh": round(model_total, 1)})
        else:
            result.append({"date": day, "energy_kwh": round(model_total, 1)})
    return result


def dashboard_market(sample=None):
    prices, weather = market_data()
    today = datetime.now(ZoneInfo("Europe/Copenhagen")).date().isoformat()
    price_today = [p for p in prices if str(p.get("time", ""))[:10] == today]
    actual_today = sample.get("pv_today") if sample else None
    return {"prices_today": price_today, "price_source": CACHE.get("price_source"),
            "price_error": CACHE.get("price_error"), "weather_error": CACHE.get("weather_error"),
            "solar_forecast": solar_forecast(weather, actual_today),
            "solar_setup": {"capacity_kwp": options().get("solar_capacity_kwp", 10),
                            "tilt": options().get("solar_tilt_degrees", 30),
                            "azimuth": options().get("solar_azimuth_degrees", 90),
                            "performance_percent": options().get("solar_performance_percent", 85)}}


def hourly_load_profile():
    """Learn a conservative hourly house-load profile from local samples."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=21)).isoformat()
    buckets = {hour: [] for hour in range(24)}
    with db() as conn:
        rows = conn.execute("SELECT created,load_w FROM samples WHERE created>=? AND load_w IS NOT NULL", (cutoff,)).fetchall()
    local_tz = ZoneInfo("Europe/Copenhagen")
    all_values = []
    for row in rows:
        try:
            value = max(0.0, float(row["load_w"]))
            hour = datetime.fromisoformat(row["created"]).astimezone(local_tz).hour
            buckets[hour].append(value)
            all_values.append(value)
        except (TypeError, ValueError):
            continue
    fallback = sum(all_values) / len(all_values) if all_values else 1200.0
    profile = {hour: round(sum(values) / len(values) if values else fallback)
               for hour, values in buckets.items()}
    return profile, len(rows)


def optimization_plan(sample, prices, weather):
    """Create an explainable, read-only 48-hour battery schedule."""
    if not sample:
        return {"status": "waiting", "message": "Venter på første Modbus-måling.", "hours": []}
    cfg, plan = options(), energy_plan()
    local_tz = ZoneInfo("Europe/Copenhagen")
    now = datetime.now(local_tz).replace(minute=0, second=0, microsecond=0)
    end = now + timedelta(hours=48)
    price_by_hour = {}
    for row in prices:
        try:
            stamp = datetime.fromisoformat(row["time"])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=local_tz)
            stamp = stamp.astimezone(local_tz).replace(minute=0, second=0, microsecond=0)
            if now <= stamp < end:
                price_by_hour[stamp] = float(row["price_dkk_kwh"])
        except (KeyError, TypeError, ValueError):
            continue
    hourly = weather.get("hourly", {}) if weather else {}
    radiation_by_hour = {}
    for stamp, value in zip(hourly.get("time", []), hourly.get("global_tilted_irradiance", [])):
        try:
            dt = datetime.fromisoformat(str(stamp)).replace(tzinfo=local_tz)
            radiation_by_hour[dt.replace(minute=0, second=0, microsecond=0)] = max(0.0, float(value or 0))
        except (TypeError, ValueError):
            continue
    forecast_hours = sorted(price_by_hour)
    if not forecast_hours:
        return {"status": "waiting", "message": "Venter på fremtidige elpriser.", "hours": []}
    prices_only = sorted(price_by_hour.values())
    cheap = prices_only[max(0, len(prices_only) // 4 - 1)]
    expensive = prices_only[min(len(prices_only) - 1, (len(prices_only) * 3) // 4)]
    load_profile, learning_samples = hourly_load_profile()
    capacity = float(plan["battery_capacity_kwh"])
    efficiency = (float(plan["roundtrip_efficiency_percent"]) / 100.0) ** 0.5
    reserve, target = float(plan["minimum_soc_morning"]), float(plan["target_soc"])
    soc = max(0.0, min(100.0, float(sample.get("soc") or reserve)))
    solar_factor = float(cfg.get("solar_capacity_kwp", 10)) * float(cfg.get("solar_performance_percent", 85)) / 100000.0
    rows = []
    for stamp in forecast_hours:
        price = price_by_hour[stamp]
        pv_kwh = radiation_by_hour.get(stamp, 0.0) * solar_factor
        load_kwh = load_profile.get(stamp.hour, 1200) / 1000.0
        surplus = pv_kwh - load_kwh
        action, power_w, reason = "hold", 0, "Normal drift; behold fleksibiliteten."
        if surplus > 0.10 and soc < target:
            charge_kwh = min(surplus, float(plan["max_charge_w"]) / 1000.0, (target - soc) * capacity / 100.0 / efficiency)
            power_w = -round(charge_kwh * 1000)
            soc += charge_kwh * efficiency / capacity * 100.0
            action, reason = "solar_charge", "Oplad batteriet med forventet soloverskud."
        elif price <= cheap and soc < target:
            charge_kwh = min(float(plan["max_charge_w"]) / 1000.0, (target - soc) * capacity / 100.0 / efficiency)
            power_w = -round(charge_kwh * 1000)
            soc += charge_kwh * efficiency / capacity * 100.0
            action, reason = "grid_charge", "Billig time; forbered batteriet til dyrere timer."
        elif price >= expensive and soc > reserve:
            available = (soc - reserve) * capacity / 100.0 * efficiency
            discharge_kwh = min(max(0.0, load_kwh - pv_kwh), float(plan["max_discharge_w"]) / 1000.0, available)
            if discharge_kwh > 0.05:
                power_w = round(discharge_kwh * 1000)
                soc -= discharge_kwh / efficiency / capacity * 100.0
                action, reason = "use_battery", "Dyr time; dæk huset fra batteriet ned til reserven."
        rows.append({"time": stamp.isoformat(), "price_dkk_kwh": round(price, 4),
                     "pv_forecast_kwh": round(pv_kwh, 2), "load_forecast_kwh": round(load_kwh, 2),
                     "action": action, "battery_power_w": power_w,
                     "projected_soc": round(max(reserve, min(target, soc)), 1), "reason": reason})
    return {"status": "learning" if learning_samples < 168 else "learned",
            "message": ("Foreløbig plan – belastningsprofilen lærer stadig." if learning_samples < 168
                        else "Plan baseret på lokal belastningshistorik."),
            "created": datetime.now(timezone.utc).isoformat(), "hours_available": len(rows),
            "learning_samples": learning_samples, "cheap_limit": round(cheap, 4),
            "expensive_limit": round(expensive, 4), "battery_capacity_kwh": capacity,
            "reserve_soc": reserve, "target_soc": target, "battery_sale_enabled": False,
            "read_only": True, "hours": rows}


def planner(sample, prices, weather):
    plan = energy_plan()
    future = [p["price_dkk_kwh"] for p in prices if p.get("price_dkk_kwh") is not None]
    price = sample.get("price_dkk")
    low = sorted(future)[max(0, len(future) // 4 - 1)] if future else None
    high = sorted(future)[min(len(future) - 1, (len(future) * 3) // 4)] if future else None
    solar = sample.get("solar_tomorrow") or 0
    soc = sample.get("soc") or 0
    details = {"soc": soc, "price_dkk_kwh": price, "cheap_limit": low,
               "expensive_limit": high, "solar_forecast_kwh_m2": solar,
               "target_soc": plan["target_soc"], "minimum_soc_morning": plan["minimum_soc_morning"]}
    if price is not None and low is not None and price <= low and soc < plan["target_soc"] and solar < 2.0:
        return "charge", "Billig opladning foreslås", "Prisen er blandt de billigste timer, solprognosen er lav, og batteriet er under mål-SOC.", details
    if (plan["allow_battery_sale"] and price is not None and high is not None and
            price >= high and soc > max(plan["minimum_soc_morning"] + 20, 50)):
        return "sell", "Batterisalg kunne være fordelagtigt", "Prisen er blandt de dyreste timer, og batteriet har reserve over morgenmålet.", details
    if soc <= plan["minimum_soc_morning"]:
        return "protect", "Batterireserve beskyttes", "SOC er på eller under den valgte morgenreserve; afladning bør begrænses.", details
    return "hold", "Ingen ændring foreslås", "Pris, solprognose og batteriniveau giver ikke grund til at ændre driften nu.", details


def tesla_charge_schedule(sample, prices):
    """Calculate the cheapest available charging hours before the next departure."""
    plan = energy_plan()
    local_tz = ZoneInfo("Europe/Copenhagen")
    now = datetime.now(local_tz)
    departure_h, departure_m = map(int, str(plan.get("tesla_departure_time", "07:00")).split(":"))
    arrival_h, arrival_m = map(int, str(plan.get("tesla_arrival_time", "16:00")).split(":"))
    departure = now.replace(hour=departure_h, minute=departure_m, second=0, microsecond=0)
    if departure <= now:
        departure += timedelta(days=1)
    arrival = departure.replace(hour=arrival_h, minute=arrival_m)
    if arrival >= departure:
        arrival -= timedelta(days=1)
    window_start = max(now, arrival)
    soc = float(sample.get("tesla_soc") or 0) if sample else 0.0
    target = min(80, int(plan.get("tesla_target_soc", 80)))
    capacity = float(plan.get("tesla_battery_capacity_kwh", 75.0))
    power = float(plan.get("tesla_charge_power_kw", 11.0))
    energy = max(0.0, (target - soc) / 100.0 * capacity)
    # Allow for AC charging loss and a little taper near the target.
    duration = energy / max(0.1, power * 0.90)
    required_slots = int(duration) + (1 if duration % 1 else 0)
    candidates = []
    for row in prices:
        try:
            stamp = datetime.fromisoformat(str(row["time"]))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=local_tz)
            stamp = stamp.astimezone(local_tz).replace(minute=0, second=0, microsecond=0)
            if window_start.replace(minute=0, second=0, microsecond=0) <= stamp < departure:
                candidates.append({"time": stamp, "price": float(row["price_dkk_kwh"])})
        except (KeyError, TypeError, ValueError):
            continue
    cheapest = sorted(candidates, key=lambda item: (item["price"], item["time"]))[:required_slots]
    selected = sorted(cheapest, key=lambda item: item["time"])
    fallback_start = departure - timedelta(hours=duration)
    plug_by = min((item["time"] for item in selected), default=fallback_start)
    return {
        "soc": round(soc, 1), "target_soc": target,
        "energy_needed_kwh": round(energy, 1), "estimated_hours": round(duration, 1),
        "charge_power_kw": power, "departure": departure.isoformat(),
        "arrival": arrival.isoformat(), "plug_in_by": plug_by.isoformat(),
        "price_hours_available": len(candidates),
        "selected_hours": [{"time": item["time"].isoformat(),
                            "price_dkk_kwh": round(item["price"], 4)} for item in selected],
    }


def maybe_send_tesla_reminder(sample, prices):
    """Send at most one plug-in reminder for each departure window."""
    if not sample or sample.get("tesla_soc") is None or sample.get("tesla_connected"):
        return
    schedule = tesla_charge_schedule(sample, prices)
    if schedule["energy_needed_kwh"] <= 0:
        return
    now = datetime.now(ZoneInfo("Europe/Copenhagen"))
    arrival = datetime.fromisoformat(schedule["arrival"])
    plug_by = datetime.fromisoformat(schedule["plug_in_by"])
    # Notify when the normal home window begins; waiting until the cheap slot
    # could make the reminder arrive after the household has gone to bed.
    if now < arrival:
        return
    key = schedule["departure"][:10] + ":" + str(schedule["target_soc"])
    previous = {}
    try:
        previous = json.loads(TESLA_REMINDER_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if previous.get("key") == key:
        return
    hours = schedule["selected_hours"]
    if hours:
        first = datetime.fromisoformat(hours[0]["time"]).strftime("%H:%M")
        last_end = (datetime.fromisoformat(hours[-1]["time"]) + timedelta(hours=1)).strftime("%H:%M")
        cheap_text = f"De valgte billige ladetimer ligger mellem kl. {first} og {last_end}."
    else:
        cheap_text = "Morgendagens priser er endnu ikke komplette, så tidspunktet er et sikkert estimat."
    message = (f"Teslaen er på {schedule['soc']:.0f} %. Tilslut bilen senest kl. "
               f"{plug_by.strftime('%H:%M')} for at kunne nå {schedule['target_soc']} % inden kl. "
               f"{datetime.fromisoformat(schedule['departure']).strftime('%H:%M')}. "
               f"Der mangler cirka {schedule['energy_needed_kwh']:.1f} kWh. {cheap_text}")
    try:
        send_ha_notification(energy_plan().get("tesla_notify_service", ""),
                             "Tesla skal sættes til opladning", message)
        TESLA_REMINDER_FILE.write_text(json.dumps({"key": key, "sent": now.isoformat(),
                                                   "message": message}, ensure_ascii=False, indent=2))
    except Exception as exc:
        print("Tesla reminder failed: %s" % exc, flush=True)


def tesla_shadow_plan(sample, prices):
    """Explain what smart charging would do; never sends a command."""
    plan = energy_plan()
    if not sample or sample.get("tesla_soc") is None:
        return {"action": "waiting", "title": "Venter på Tesla-data",
                "explanation": "Tesla Fleet-data er endnu ikke tilgængelige.", "read_only": True}
    connected = bool(sample.get("tesla_connected"))
    charging = str(sample.get("tesla_charging_state") or "").lower() == "charging"
    car_soc = float(sample.get("tesla_soc") or 0)
    target = min(80, int(plan.get("tesla_target_soc", 80)))
    if not connected:
        action, title, explanation = "disconnected", "Tesla er ikke tilsluttet", "Der planlægges ingen opladning, før ladekablet forbindes."
    elif car_soc >= target:
        action, title = "stop" if charging else "complete", "Tesla har nået målet"
        explanation = f"Bilen er på {car_soc:.0f} %, og grænsen er {target} %. Shadow Mode ville stoppe opladningen."
    else:
        future = sorted(float(p["price_dkk_kwh"]) for p in prices if p.get("price_dkk_kwh") is not None)
        cheap = future[max(0, len(future) // 4 - 1)] if future else None
        price = sample.get("price_dkk")
        home_soc = float(sample.get("soc") or 0)
        reserve = float(plan.get("minimum_soc_morning", 20))
        grid_w = float(sample.get("grid_w") or 0)
        if grid_w < -1000 and home_soc >= reserve + 10:
            action, title = "solar_charge", "Soloverskud kan bruges til Teslaen"
            explanation = "Huset eksporterer strøm, og hjemmebatteriet har reserve. Shadow Mode ville foreslå opladning nu."
        elif 16 <= datetime.now(ZoneInfo("Europe/Copenhagen")).hour < 22 and home_soc <= reserve + 15:
            action, title = "protect_house", "Bevar strøm til huset"
            explanation = "Det er aftentid, og hjemmebatteriets reserve prioriteres til madlavning og dyrere timer. Teslaen bør vente."
        elif price is not None and cheap is not None and float(price) <= cheap:
            action, title = "grid_charge", "Billig time til Tesla-opladning"
            explanation = f"Prisen {float(price):.3f} kr/kWh er blandt de billigste tilgængelige timer."
        else:
            action, title = "wait", "Vent på en billigere time"
            explanation = f"Teslaen er tilsluttet ved {car_soc:.0f} %. Planen holder målet på højst {target} % og prioriterer huset først."
    schedule = tesla_charge_schedule(sample, prices)
    return {"action": action, "title": title, "explanation": explanation,
            "connected": connected, "charging": charging, "soc": car_soc,
            "target_soc": target, "departure_time": plan.get("tesla_departure_time", "07:00"),
            "charge_schedule": schedule,
            "read_only": True}


def tesla_energy_today():
    """Estimate charged energy today from Tesla's per-session energy counter."""
    local_tz = ZoneInfo("Europe/Copenhagen")
    start_local = datetime.now(local_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc).isoformat()
    with db() as conn:
        previous = conn.execute(
            "SELECT tesla_charge_energy_added_kwh FROM samples "
            "WHERE created<? AND tesla_charge_energy_added_kwh IS NOT NULL ORDER BY id DESC LIMIT 1",
            (start_utc,),
        ).fetchone()
        rows = conn.execute(
            "SELECT tesla_charge_energy_added_kwh FROM samples "
            "WHERE created>=? AND tesla_charge_energy_added_kwh IS NOT NULL ORDER BY id",
            (start_utc,),
        ).fetchall()
    last = float(previous[0]) if previous else 0.0
    total = 0.0
    for row in rows:
        current = max(0.0, float(row[0]))
        if current >= last:
            total += current - last
        else:
            # A lower counter means that a new charging session started.
            total += current
        last = current
    return round(total, 2)


def collect_sample():
    regs = {}
    for start, count in [(117, 1), (141, 37), (514, 16), (587, 5), (616, 4), (650, 4), (672, 2)]:
        for item in read_holding(start, count):
            regs[item["address"]] = item
    market_error = None
    try:
        prices, weather = market_data()
    except Exception as exc:
        # Live Modbus data must never become stale because an internet service is unavailable.
        prices, weather = CACHE.get("prices") or [], CACHE.get("weather") or {}
        market_error = str(exc)
    daily = weather.get("daily", {}) if weather else {}
    solar_values = daily.get("shortwave_radiation_sum", [])
    solar_tomorrow = float(solar_values[1]) if len(solar_values) > 1 else None
    tesla = tesla_state()
    sample = {
        "created": datetime.now(timezone.utc).isoformat(), "soc": regs.get(588, {}).get("value"),
        "battery_w": regs.get(590, {}).get("value"), "grid_w": regs.get(619, {}).get("value"),
        "grid_l1_w": regs.get(616, {}).get("value"),
        "grid_l2_w": regs.get(617, {}).get("value"),
        "grid_l3_w": regs.get(618, {}).get("value"),
        "load_w": regs.get(653, {}).get("value"),
        "pv1_w": regs.get(672, {}).get("value"), "pv2_w": regs.get(673, {}).get("value"),
        "pv_w": sum(float(regs.get(x, {}).get("value", 0)) for x in (672, 673)),
        "battery_charge_today": regs.get(514, {}).get("value"),
        "battery_discharge_today": regs.get(515, {}).get("value"),
        "grid_import_today": regs.get(520, {}).get("value"),
        "grid_export_today": regs.get(521, {}).get("value"),
        "load_today": regs.get(526, {}).get("value"), "pv_today": regs.get(529, {}).get("value"),
        "reg117": regs.get(117, {}).get("uint16"), "reg141": regs.get(141, {}).get("uint16"),
        "reg142": regs.get(142, {}).get("uint16"), "reg145": regs.get(145, {}).get("uint16"),
        "reg146": regs.get(146, {}).get("uint16"),
        "tou": {str(x): regs.get(x, {}).get("uint16") for x in range(148, 178)},
        "price_dkk": current_price(prices), "solar_tomorrow": solar_tomorrow,
        "market_error": market_error,
    }
    for key in TESLA_ENTITIES:
        sample[key] = tesla.get(key)
    action, title, explanation, details = planner(sample, prices, weather)
    with db() as conn:
        columns = [
            "created", "soc", "battery_w", "grid_w", "grid_l1_w", "grid_l2_w", "grid_l3_w",
            "load_w", "pv_w", "pv1_w", "pv2_w", "battery_charge_today",
            "battery_discharge_today", "grid_import_today", "grid_export_today", "load_today",
            "pv_today", "reg117", "reg141", "reg142", "reg145", "reg146", "tou_json",
            "price_dkk", "solar_tomorrow", *TESLA_ENTITIES.keys(),
        ]
        values = [json.dumps(sample["tou"]) if key == "tou_json" else sample.get(key) for key in columns]
        conn.execute(
            "INSERT INTO samples(%s) VALUES(%s)" % (",".join(columns), ",".join("?" for _ in columns)),
            values,
        )
        last = conn.execute("SELECT action,title,details_json,created FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
        should_add = not last or last["action"] != action or time.time() - datetime.fromisoformat(last["created"]).timestamp() > 3600
        if should_add:
            conn.execute("INSERT INTO decisions(created,action,title,explanation,details_json) VALUES(?,?,?,?,?)",
                         (sample["created"], action, title, explanation, json.dumps(details, ensure_ascii=False)))
    maybe_send_tesla_reminder(sample, prices)
    return sample


def dashboard_data():
    with db() as conn:
        sample = conn.execute("SELECT * FROM samples ORDER BY id DESC LIMIT 1").fetchone()
        decisions = conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 20").fetchall()
    sample_data = dict(sample) if sample else None
    market = dashboard_market(sample_data)
    prices, weather = market_data()
    if sample_data:
        sample_data["tesla_energy_today_kwh"] = tesla_energy_today()
    return {"sample": sample_data, **market, "optimization": optimization_plan(sample_data, prices, weather),
            "tesla_shadow": tesla_shadow_plan(sample_data, prices),
            "decisions": [{**dict(x), "details": json.loads(x["details_json"])} for x in decisions],
            "read_only": True, "automatic_writes": False}


def analysis_package(days=7):
    days = 1 if int(days) <= 1 else 7
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db() as conn:
        samples = [dict(row) for row in conn.execute(
            "SELECT * FROM samples WHERE created>=? ORDER BY id", (cutoff,))]
        decisions = [dict(row) for row in conn.execute(
            "SELECT * FROM decisions WHERE created>=? ORDER BY id", (cutoff,))]
    safe_cfg = options()
    safe_cfg = {key: safe_cfg.get(key) for key in (
        "deye_port", "slave_id", "timeout_seconds", "read_only", "price_area",
        "sample_interval_seconds", "solar_capacity_kwp", "solar_tilt_degrees",
        "solar_azimuth_degrees", "solar_performance_percent")}
    market = dashboard_market(samples[-1] if samples else None)
    prices, weather = market_data()
    optimization = optimization_plan(samples[-1] if samples else None, prices, weather)
    manifest = {
        "package_format": 1,
        "app": "Deye AI Bridge",
        "app_version": APP_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "history_days": days,
        "sample_count": len(samples),
        "decision_count": len(decisions),
        "privacy": "Adgangskoder, tokens, præcis placering og Deye-IP er udeladt.",
        "read_only": True,
    }
    files = {
        "manifest.json": manifest,
        "configuration_safe.json": safe_cfg,
        "energy_plan.json": energy_plan(),
        "samples.json": samples,
        "decisions.json": decisions,
        "market_and_forecast.json": market,
        "optimization_plan_48h.json": optimization,
    }
    if SNAPSHOT_FILE.exists():
        try: files["last_register_snapshot.json"] = json.loads(SNAPSHOT_FILE.read_text())
        except Exception: pass
    if FULL_SNAPSHOT_FILE.exists():
        try: files["full_register_snapshot.json"] = json.loads(FULL_SNAPSHOT_FILE.read_text())
        except Exception: pass
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, content in files.items():
            archive.writestr(filename, json.dumps(content, ensure_ascii=False, indent=2, default=str))
    return output.getvalue(), days


def decide(decision_id, status):
    if status not in ("approved", "rejected"):
        raise ValueError("Ugyldig beslutning")
    with db() as conn:
        row = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        if not row:
            raise ValueError("Forslaget findes ikke")
        conn.execute("UPDATE decisions SET status=?,decided=? WHERE id=?",
                     (status, datetime.now(timezone.utc).isoformat(), decision_id))
    return {"saved": True, "status": status, "executed": False,
            "message": "Beslutningen er gemt, men intet er sendt til inverteren."}


def full_snapshot(start, end):
    start, end = max(0, int(start)), min(2000, int(end))
    if end <= start or end - start > 2000:
        raise ValueError("Vælg et område på højst 2000 registre")
    old = None
    if FULL_SNAPSHOT_FILE.exists():
        try: old = json.loads(FULL_SNAPSHOT_FILE.read_text())
        except Exception: pass
    registers, errors = [], []
    for block in range(start, end, 100):
        count = min(100, end - block)
        try: registers.extend(read_holding(block, count))
        except Exception as exc: errors.append({"start": block, "count": count, "error": str(exc)})
    current = {"created": datetime.now(timezone.utc).isoformat(), "start": start, "end": end,
               "registers": registers, "errors": errors}
    changes = []
    if old:
        before = {r["address"]: r["uint16"] for r in old.get("registers", [])}
        for r in registers:
            if r["address"] in before and before[r["address"]] != r["uint16"]:
                changes.append({"address": r["address"], "before": before[r["address"]], "after": r["uint16"], "name": r["name"]})
    FULL_SNAPSHOT_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2))
    return {**current, "changes": changes, "previous": old.get("created") if old else None}


def collector_loop():
    while True:
        try:
            collect_sample()
        except Exception as exc:
            print("Shadow collector error:", exc, flush=True)
        time.sleep(int(options().get("sample_interval_seconds", 60)))


def known_scan():
    ranges = [(117, 1), (141, 6), (148, 30), (340, 1), (514, 16), (587, 5),
              (598, 22), (633, 21), (672, 8)]
    registers = []
    errors = []
    for start, count in ranges:
        try:
            registers.extend(read_holding(start, count))
        except Exception as exc:
            errors.append({"start": start, "count": count, "error": str(exc)})
    return {"registers": registers, "errors": errors,
            "known_count": sum(1 for r in registers if r["known"])}


def recv_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("Forbindelsen blev lukket")
        data.extend(chunk)
    return bytes(data)


def status():
    cfg = options()
    result = {"read_only": bool(cfg["read_only"]), "deye_host": cfg["deye_host"],
              "deye_port": cfg["deye_port"], "slave_id": cfg["slave_id"]}
    try:
        info = ha_request("/config")
        result["home_assistant"] = "forbundet"
        result["home_assistant_version"] = info.get("version")
    except Exception as exc:
        result["home_assistant"] = "fejl: " + str(exc)
    try:
        with socket.create_connection((str(cfg["deye_host"]), int(cfg["deye_port"])), timeout=2):
            pass
        result["deye_tcp"] = "åben"
    except Exception as exc:
        result["deye_tcp"] = "fejl: " + str(exc)
    return result


def snapshot(registers):
    previous = None
    if SNAPSHOT_FILE.exists():
        try:
            previous = json.loads(SNAPSHOT_FILE.read_text())
        except json.JSONDecodeError:
            pass
    current = {"created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "registers": registers}
    SNAPSHOT_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2))
    changes = []
    if previous:
        old = {x["address"]: x["uint16"] for x in previous.get("registers", [])}
        for item in registers:
            before = old.get(item["address"])
            if before is not None and before != item["uint16"]:
                changes.append({"address": item["address"], "before": before, "after": item["uint16"]})
    return {"saved": True, "previous_created": previous.get("created") if previous else None,
            "changes": changes, "registers": registers}


class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, status_code=200):
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        try:
            path = urlparse(self.path).path.rstrip("/")
            if path.endswith("/api/status"):
                return self.send_json(status())
            if path.endswith("/api/entities"):
                return self.send_json(deye_entities())
            if path.endswith("/api/notification-services"):
                return self.send_json(notification_services())
            if path.endswith("/api/snapshot"):
                if not SNAPSHOT_FILE.exists():
                    return self.send_json({"saved": False})
                return self.send_json(json.loads(SNAPSHOT_FILE.read_text()))
            if path.endswith("/api/plan"):
                return self.send_json(energy_plan())
            if path.endswith("/api/shadow"):
                return self.send_json(dashboard_data())
            if path.endswith("/api/analysis-package"):
                query = urllib.parse.parse_qs(urlparse(self.path).query)
                payload, days = analysis_package(query.get("days", ["7"])[0])
                stamp = datetime.now(ZoneInfo("Europe/Copenhagen")).strftime("%Y%m%d-%H%M")
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", "attachment; filename=deye-analyse-%dd-%s.zip" % (days, stamp))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if path.endswith("/tesla_icon.b64"):
                payload = (APP_DIR / "tesla_icon.b64").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=us-ascii")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            page = (APP_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            path = urlparse(self.path).path.rstrip("/")
            if path.endswith("/api/scan"):
                registers = read_holding(int(body.get("start", 0)), int(body.get("count", 1)))
                return self.send_json({"registers": registers})
            if path.endswith("/api/snapshot"):
                registers = read_holding(int(body.get("start", 0)), int(body.get("count", 1)))
                return self.send_json(snapshot(registers))
            if path.endswith("/api/known-scan"):
                return self.send_json(known_scan())
            if path.endswith("/api/plan"):
                return self.send_json(save_energy_plan(body))
            if path.endswith("/api/decision"):
                return self.send_json(decide(int(body.get("id")), str(body.get("status"))))
            if path.endswith("/api/full-snapshot"):
                return self.send_json(full_snapshot(body.get("start", 0), body.get("end", 1000)))
            self.send_json({"error": "Ukendt handling"}, 404)
        except (ValueError, KeyError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 502)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.client_address[0], fmt % args), flush=True)


if __name__ == "__main__":
    init_db()
    threading.Thread(target=collector_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
