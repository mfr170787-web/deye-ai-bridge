#!/usr/bin/env python3
import json
import math
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
from contextlib import contextmanager
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from private_reads import read_history, read_source

APP_DIR = Path(os.environ.get('DEYE_APP_DIR', '/app'))
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
APP_VERSION = "0.9.0"
from automatic_control import AutomaticControl, DayPlans, valid_write as valid_auto_write
from charge_test import ChargeTest
from test_suite import TestSuite
from charge20_test import Charge20Test
from modbus_telemetry import ClockDiagnostics
from modbus_telemetry import REGISTERS as TELEMETRY_REGISTERS, decode as decode_telemetry
from energy_accounting import daily_energy
from monthly_register import MonthlyRegister
from shadow_planning import price_window, limits, reserve, charge_schedule, economic_dispatch, power_limited_plan, planning_weather

# Field names are generic. Actual entity IDs are selected and stored on the Pi.
TESLA_ENTITIES = dict.fromkeys((
    'tesla_connected','tesla_online','tesla_soc','tesla_charging_state',
    'tesla_charger_power_kw','tesla_charger_current_a','tesla_charger_voltage_v',
    'tesla_charge_energy_added_kwh','tesla_charge_limit','tesla_charge_current_limit_a',
    'tesla_charge_switch','tesla_time_to_full'))
TESLA_FIELDS = dict(zip(TESLA_ENTITIES, (
    ('binary_sensor','opladerkabel'),('binary_sensor','status'),('sensor','battery_level'),
    ('sensor','charging'),('sensor','charger_power'),('sensor','charger_current'),
    ('sensor','charger_voltage'),('sensor','charge_energy_added'),('number','charge_limit'),
    ('number','charge_current'),('switch','charge'),('sensor','time_to_full_charge'))))
TESLA_SELECTION = DATA_DIR/'tesla_selection.json'
TESLA_SELECTION_LOCK = threading.Lock()


REGISTER_MAP = {
    117: ("Batteri LowBatt-SOC", "%", 1, False),
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

REGISTER_MAP.update(TELEMETRY_REGISTERS)

def options():
    defaults = {
        "deye_host": "DIN_DEYE_IP",
        "deye_port": 502,
        "slave_id": 1,
        "timeout_seconds": 3,
        "read_only": True,
        "commissioning_enabled": False,
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


VICTRON_CONFIG = DATA_DIR / 'victron_selection.json'
VICTRON_CONTROL_LOCK = threading.Lock()
VICTRON_FIELDS = {
    'charging': ('switch', 'charging'),
    'current_limit': ('number', 'charging_current_limit'),
    **{key: ('sensor', key) for key in (
        'actual_charging_current', 'charge_mode', 'charger_status',
        'l1_power', 'l2_power', 'l3_power', 'session_energy',
        'session_time', 'total_energy', 'total_power')},
}


def victron_candidates(states):
    indexed = {item['entity_id']: item for item in states}
    candidates = []
    for entity, item in indexed.items():
        if not entity.startswith('switch.victron_') or not entity.endswith('_charging'):
            continue
        prefix = entity[len('switch.'):-len('charging')]
        mapping = {key: domain + '.' + prefix + suffix for key, (domain, suffix) in VICTRON_FIELDS.items()}
        if all(mapping[key] in indexed for key in ('charging', 'current_limit', 'charger_status', 'total_power', 'total_energy')):
            candidates.append({'id': entity, 'name': item.get('attributes', {}).get('friendly_name', entity), 'entities': mapping})
    return candidates


def victron_selected():
    try:
        return json.loads(VICTRON_CONFIG.read_text()).get('id')
    except (OSError, ValueError):
        return None


def victron_state():
    states = ha_request('/states')
    indexed = {item['entity_id']: item for item in states}
    candidates = victron_candidates(states)
    selected = next((c for c in candidates if c['id'] == victron_selected()), None)
    fields = {}
    if selected:
        for key, entity in selected['entities'].items():
            item = indexed.get(entity, {})
            fields[key] = {'entity_id': entity, 'state': item.get('state', 'unavailable'),
                           'unit': item.get('attributes', {}).get('unit_of_measurement', ''),
                           'updated': item.get('last_updated')}
    return {'fields': fields, 'candidates': [{'id': c['id'], 'name': c['name']} for c in candidates],
            'selected': selected['id'] if selected else None, 'mode': 'manual_only'}


def victron_action(body):
    action = body.get('action')
    if action not in ('select', 'on', 'off', 'current'):
        raise ValueError('Ukendt Victron-handling')
    with VICTRON_CONTROL_LOCK:
        candidates = victron_candidates(ha_request('/states'))
        if action == 'select':
            selected = next((c for c in candidates if c['id'] == body.get('id')), None)
            if not selected:
                raise ValueError('Vælg en fundet Victron-lader')
            VICTRON_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            temporary = VICTRON_CONFIG.with_suffix('.tmp')
            temporary.write_text(json.dumps({'id': selected['id']}))
            temporary.replace(VICTRON_CONFIG)
            return {'message': 'Lader valgt og gemt lokalt. Ingen ladekommando sendt.'}
        selected = next((c for c in candidates if c['id'] == victron_selected()), None)
        if not selected:
            raise ValueError('Vælg først en Victron-lader i kortet')
        if action == 'current':
            value = body.get('value')
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 6 <= value <= 32 or int(value) != value:
                raise ValueError('Ladestrøm skal være et helt tal fra 6 til 32 A')
            domain, suffix = VICTRON_FIELDS['current_limit']
            service, payload = 'number/set_value', {'value': int(value)}
        else:
            domain, suffix = VICTRON_FIELDS['charging']
            service, payload = 'switch/turn_' + action, {}
        entity = selected['entities']['current_limit' if action == 'current' else 'charging']
        state = ha_request('/states/' + entity)
        if state.get('state') in (None, 'unknown', 'unavailable'):
            raise ValueError('Laderen er utilgængelig; kommandoen er ikke sendt')
        payload['entity_id'] = entity
        ha_request('/services/' + service, 'POST', payload)
        return {'message': 'Kommando sendt til Home Assistant. Kontrollér status og faktisk effekt.',
                'action': action}


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


def last_tesla_reminder():
    try:
        return json.loads(TESLA_REMINDER_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def send_ha_notification(service, title, message):
    # Mirror to HA so clearing the push does not lose the recommendation.
    ha_request("/services/persistent_notification/create", "POST",
               {"title": title, "message": message,
                "notification_id": "deye_ai_tesla_charge_reminder"})
    if service and re.fullmatch(r"[a-z0-9_]+", str(service)):
        data = {"title": title, "message": message}
        if str(service).startswith("mobile_app_"):
            data["data"] = {"url": "/hassio/ingress/" + addon_slug(),
                            "tag": "deye_ai_tesla_charge_reminder"}
        return ha_request("/services/notify/" + str(service), "POST", data)


def maybe_notify_control_fault():
    """Independent of inverter access; persist successful alerts across restarts."""
    state_path=DATA_DIR/'automatic_control.json'
    alert_path=DATA_DIR/'control_alerts.json'
    try:
        state=json.loads(state_path.read_text())
    except (FileNotFoundError,json.JSONDecodeError):
        return
    status=state.get('status')
    # Recovery and its first attempts are silent. Alert only a terminal stop.
    if status not in ('fault','restore_failed') or (state.get('auto_resume') and not state.get('recovery_exhausted')):
        return
    reason=state.get('stop_reason') or state.get('error') or ''
    if not str(reason).startswith(('fault:','worker_fault:')) and not state.get('error'):
        return
    phase='restored' if state.get('restored') else 'manual'
    key=json.dumps([state.get('started'),reason,phase],ensure_ascii=False)
    try: ledger=json.loads(alert_path.read_text())
    except (FileNotFoundError,json.JSONDecodeError): ledger={'sent':[]}
    if key in ledger.get('sent',[]):return
    if time.time()-ledger.get('last_attempt',0)<300:return
    ledger['last_attempt']=time.time()
    alert_path.write_text(json.dumps(ledger,ensure_ascii=False))
    service=energy_plan().get('tesla_notify_service','')
    if not service or not re.fullmatch(r'mobile_app_[a-z0-9_]+',str(service)):
        print('Control alert: vælg telefonmodtager under Tesla-planen.',flush=True);return
    result={'restored':'Automatikken kunne ikke genoptages automatisk. Oprindelige indstillinger er gendannet og efterkontrolleret. Kontrollér Bridge.',
            'manual':'Automatikken kunne ikke genoptages automatisk, og gendannelse er IKKE bekræftet. Kontrollér inverteren manuelt.'}[phase]
    if state.get('recovery_exhausted'):
        result='Tre genforsøg mislykkedes. Automatikken er på pause og prøver igen efter 15 minutter. Kontrollér Bridge og forbindelsen.'
    message=result+' Fejl: '+str(reason)[:1000]
    ha_request('/services/notify/'+service,'POST',{'title':'Deye AI · fejl i automatisk styring','message':message,
        'data':{'tag':'deye_ai_control_fault','url':'/hassio/ingress/'+addon_slug()}})
    ledger.setdefault('sent',[]).append(key);ledger['sent']=ledger['sent'][-100:]
    ledger['last_message']=message;ledger['sent_at']=datetime.now(timezone.utc).isoformat()
    ledger['last_attempt']=0
    alert_path.write_text(json.dumps(ledger,ensure_ascii=False))
    try:
        ha_request('/services/persistent_notification/create','POST',{'title':'Deye AI · styringsfejl','message':message,'notification_id':'deye_ai_control_fault'})
    except Exception as exc:print('Control alert mirror failed: %s'%exc,flush=True)


def control_alert_worker():
    while True:
        try:maybe_notify_control_fault()
        except Exception as exc:print('Control phone notification failed: %s'%exc,flush=True)
        time.sleep(5)


def addon_slug():
    try:
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        req = urllib.request.Request("http://supervisor/addons/self/info",
                                     headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)["data"]["slug"]
    except Exception:
        return "deye_ai_bridge"


def tesla_candidates(states):
    indexed={item['entity_id']:item for item in states}
    candidates=[]
    for entity,item in indexed.items():
        if not entity.startswith('sensor.') or not entity.endswith('_battery_level'):
            continue
        prefix=entity[len('sensor.'):-len('battery_level')]
        mapping={key:domain+'.'+prefix+suffix for key,(domain,suffix) in TESLA_FIELDS.items()}
        if not all(mapping[key] in indexed for key in ('tesla_soc','tesla_charger_power_kw','tesla_charge_limit')):
            continue
        candidates.append({'id':entity,'name':item.get('attributes',{}).get('friendly_name',entity),'entities':mapping})
    return candidates


def tesla_selection():
    try:
        return json.loads(TESLA_SELECTION.read_text()).get('id')
    except (OSError,ValueError):
        return None


def tesla_devices():
    candidates=tesla_candidates(ha_request('/states'))
    return {'selected':tesla_selection(),'candidates':[{'id':c['id'],'name':c['name']} for c in candidates]}


def select_tesla(body):
    with TESLA_SELECTION_LOCK:
        selected=next((c for c in tesla_candidates(ha_request('/states')) if c['id']==body.get('id')),None)
        if not selected:
            raise ValueError('Vælg en fundet Tesla')
        TESLA_SELECTION.parent.mkdir(parents=True,exist_ok=True)
        tmp=TESLA_SELECTION.with_suffix('.tmp')
        tmp.write_text(json.dumps({'id':selected['id']}));tmp.replace(TESLA_SELECTION)
        return {'message':'Tesla valgt og gemt lokalt. Ingen kommando sendt til bilen.'}


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
    selected=next((c for c in tesla_candidates(list(states.values())) if c['id']==tesla_selection()),None)
    if not selected:
        return {'available':False,'error':'Vælg din Tesla i kortet Tesla · lokal forbindelse.'}
    for key, entity_id in selected['entities'].items():
        item = states.get(entity_id)
        value = item.get("state") if item else None
        if key in numeric:
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
        elif key in ("tesla_connected", "tesla_online"):
            value = None if value in (None, "unknown", "unavailable") else value == "on"
        result[key] = value
        result[key + "_entity_id"] = entity_id
        if key == "tesla_charger_power_kw":
            result["tesla_power_updated"] = item.get("last_updated") if item else None
    result["available"] = result.get("tesla_soc") is not None
    return result



MODBUS_LOCK = threading.Lock()
MODBUS_DIAGNOSTIC_LOCK = threading.Lock()
MODBUS_ERRORS = deque(maxlen=200)


def modbus_diagnostics():
    with MODBUS_DIAGNOSTIC_LOCK:
        return {"scope": "Denne Bridge-proces siden genstart; andre klienter er ikke omfattet",
                "errors": list(MODBUS_ERRORS)}


@contextmanager
def modbus_operation(operation, start=None, count=None):
    # Bound queue waits; never retry a potentially accepted write.
    started = time.monotonic()
    acquired = False
    try:
        acquired = MODBUS_LOCK.acquire(timeout=max(1, int(options()["timeout_seconds"])))
        if not acquired:
            raise TimeoutError("Modbus-køens ventetid blev overskredet")
        yield
    except Exception as exc:
        stamp = datetime.now(timezone.utc).isoformat()
        entry = {"at_utc": stamp, "operation": operation, "start": start,
                 "count": count, "duration_ms": round((time.monotonic()-started)*1000),
                 "error_type": type(exc).__name__}
        # No host, credentials, payloads, or arbitrary exception text in exports.
        with MODBUS_DIAGNOSTIC_LOCK:
            MODBUS_ERRORS.append(entry)
        raise RuntimeError(f"Modbus {operation}, register {start}, antal {count}, {stamp}: {type(exc).__name__}: {exc}") from exc
    finally:
        if acquired:
            MODBUS_LOCK.release()


def read_holding(start, count):
    # Retry reads only. Each attempt obtains a fresh TCP connection and lock.
    for attempt in range(3):
        try:
            return read_holding_once(start,count)
        except RuntimeError as exc:
            if not isinstance(exc.__cause__,(TimeoutError,ConnectionError,OSError)) or attempt==2:
                raise
            time.sleep(.3*(attempt+1))


def read_holding_once(start, count):
    with modbus_operation("read", start, count):
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
        "evening_start": "16:00", "max_charge_w": 11000, "charge_profile": "190A-v1",
        "max_export_w": 0, "allow_battery_sale": False,
        "main_fuse_ampere": 35, "automatic_control": False,
        "battery_capacity_kwh": 32.0, "max_discharge_w": 11000, "discharge_profile": "190A-v1",
        "roundtrip_efficiency_percent": 90,
        "tesla_target_soc": 80, "tesla_departure_time": "07:00",
        "tesla_arrival_time": "16:00", "tesla_battery_capacity_kwh": 75.0,
        "tesla_charge_power_kw": 11.0, "tesla_notify_service": "",
        "tesla_shadow_mode": True, "reserve_margin_percent": 15,
    }
    try:
        saved=json.loads(PLAN_FILE.read_text())
        defaults.update(saved)
        # Keep any explicitly chosen lower power limit, including 8000 W.
        if saved.get('charge_profile') not in ('150A-v1','190A-v1') and int(defaults['max_charge_w'])==4500:
            defaults['max_charge_w']=11000
        if saved.get('charge_profile')=='150A-v1' and int(defaults['max_charge_w']) in (8000,9000):
            defaults['max_charge_w']=11000
        defaults['charge_profile']='190A-v1'
        if saved.get('discharge_profile')!='190A-v1' and int(defaults['max_discharge_w'])==4500:
            defaults['max_discharge_w']=11000
        defaults['discharge_profile']='190A-v1'
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    defaults['target_soc'] = min(99, max(16, int(defaults['target_soc'])))
    defaults['minimum_soc_morning'] = max(15, min(98,int(defaults['minimum_soc_morning'])))
    defaults['max_charge_w'] = max(0, min(11000,int(defaults['max_charge_w'])))
    defaults['max_discharge_w'] = max(0,min(11000,int(defaults['max_discharge_w'])))
    defaults['allow_battery_sale'] = False
    defaults['max_export_w'] = 0
    return defaults


def save_energy_plan(data):
    plan = energy_plan()
    allowed = set(plan)
    plan.update({k: v for k, v in data.items() if k in allowed})
    plan["target_soc"] = max(10, min(99, int(plan["target_soc"])))
    plan["minimum_soc_morning"] = max(15, min(98, int(plan["minimum_soc_morning"])))
    plan["max_charge_w"] = max(0, min(11000, int(plan["max_charge_w"])))
    plan["max_export_w"] = max(0, min(12000, int(plan["max_export_w"])))
    plan["battery_capacity_kwh"] = max(5, min(100, float(plan["battery_capacity_kwh"])))
    plan["max_discharge_w"] = max(0, min(11000, int(plan["max_discharge_w"])))
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
    plan["allow_battery_sale"] = False
    plan["max_export_w"] = 0
    if plan["minimum_soc_morning"] >= plan["target_soc"]:
        raise ValueError("Mål-SOC skal være højere end minimumsreserven.")
    plan["saved_at"] = datetime.now(timezone.utc).isoformat()
    pending = PLAN_FILE.with_suffix(".tmp")
    pending.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    pending.replace(PLAN_FILE)
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
        for column in ("tesla_charging_state", "tesla_charge_switch", "tesla_time_to_full", "tesla_power_updated", "telemetry_json"):
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
        rows = conn.execute("SELECT * FROM samples WHERE created>=? AND load_w IS NOT NULL", (cutoff,)).fetchall()
    local_tz = ZoneInfo("Europe/Copenhagen")
    all_values = []
    for row in rows:
        try:
            value = max(0.0, float(row["load_w"]))
            ev = validated_tesla_power(dict(row))
            if ev is None:
                continue  # uncertain car data must not teach a false house profile
            value = max(0.0, value - ev * 1000)
            hour = datetime.fromisoformat(row["created"]).astimezone(local_tz).hour
            buckets[hour].append(value)
            all_values.append(value)
        except (TypeError, ValueError):
            continue
    fallback = sum(all_values) / len(all_values) if all_values else 1200.0
    profile = {hour: round(sum(values) / len(values) if values else fallback)
               for hour, values in buckets.items()}
    return profile, len(rows)


def validated_tesla_power(sample):
    """Unknown/stale or physically inconsistent power is not treated as zero."""
    if sample.get("tesla_connected") == 0:
        return 0.0
    if sample.get("tesla_connected") is None:
        return None
    power = sample.get("tesla_charger_power_kw")
    try:
        updated = datetime.fromisoformat(sample.get("tesla_power_updated") or "")
        measured = datetime.fromisoformat(sample["created"])
        age = (measured-updated).total_seconds()
        if not 0 <= age <= 300 or power is None:
            return None
        power = float(power)
        if not 0 <= power <= 22 or power*1000 > float(sample["load_w"])+500:
            return None
        return power
    except (ValueError, TypeError, KeyError):
        return None


def forecast_slots(prices, weather, now):
    profile, count = hourly_load_profile()
    radiation = {}
    hourly = (weather or {}).get("hourly", {})
    for stamp, value in zip(hourly.get("time", []), hourly.get("global_tilted_irradiance", [])):
        try:
            dt = datetime.fromisoformat(stamp).replace(tzinfo=now.tzinfo)
            radiation[dt] = max(0, float(value or 0))
        except (ValueError, TypeError):
            pass
    cfg = options()
    factor = cfg["solar_capacity_kwp"]*cfg["solar_performance_percent"]/100000
    slots = []
    for p in price_window(prices, now):
        fraction = min(1, max(0, (p["time"]+timedelta(hours=1)-now).total_seconds()/3600))
        slots.append({**p, "fraction": fraction, "load": profile[p["time"].hour]/1000,
                      "pv": radiation.get(p["time"], 0)*factor})
    return slots, count


BATTERY_COST_LOCK = threading.Lock()


def battery_cost(sample=None, plan=None):
    """Local estimated moving inventory. Unknown historical energy stays unknown."""
    path=DATA_DIR/'battery_cost.json'
    with BATTERY_COST_LOCK:
        try:
            state=json.loads(path.read_text())
        except (OSError,ValueError):
            state={}
        if sample is not None:
            plan=plan or energy_plan()
            capacity=float(plan['battery_capacity_kwh'])
            energy=max(0.,min(capacity,float(sample.get('soc') or 0)*capacity/100))
            t=datetime.fromisoformat(sample['created']).timestamp()
            previous=state.get('previous')
            known=float(state.get('known_kwh',0));unknown=float(state.get('unknown_kwh',energy));cost=float(state.get('cost_dkk',0))
            if previous and t>previous['t']:
                dt=t-previous['t']
                if dt<=120 and sample.get('battery_w') is not None and previous.get('battery_w') is not None:
                    watts=(float(sample['battery_w'])+previous['battery_w'])/2
                    if watts<0:
                        added=-watts*dt/3600000
                        pv=max(0.,(float(sample.get('pv_w') or 0)+previous['pv_w'])/2)
                        load=max(0.,(float(sample.get('load_w') or 0)+previous['load_w'])/2)
                        # Surplus PV is allocated first; ambiguous remainder is unknown.
                        solar=min(added,max(0.,pv-load)*dt/3600000)
                        grid=min(max(0.,added-solar),max(0.,(float(sample.get('grid_w') or 0)+previous['grid_w'])/2)*dt/3600000)
                        price=previous.get('price')
                        eta=(float(plan['roundtrip_efficiency_percent'])/100)**.5
                        known+=solar;cost+=solar*.03
                        if isinstance(price,(float,int)) and math.isfinite(price):
                            known+=grid;cost+=grid*price/eta
                        else:unknown+=grid
                        unknown+=max(0.,added-solar-grid)
                    elif watts>0:
                        fraction=max(0.,1-watts*dt/3600000/max(known+unknown,.001))
                        known*=fraction;unknown*=fraction;cost*=fraction
                else:
                    # No interpolation across missing telemetry. Reconcile as unknown.
                    pass
                total=known+unknown
                if energy>total:unknown+=energy-total
                elif total>0:
                    scale=energy/total;known*=scale;unknown*=scale;cost*=scale
            elif not previous:
                known=cost=0.;unknown=energy
            if not previous or t>previous['t']:
                state={'known_kwh':known,'unknown_kwh':unknown,'cost_dkk':cost,
                       'previous':{'t':t,'battery_w':sample.get('battery_w'),
                                   'pv_w':float(sample.get('pv_w') or 0),'load_w':float(sample.get('load_w') or 0),
                                   'grid_w':float(sample.get('grid_w') or 0),'price':sample.get('price_dkk')}}
                path.parent.mkdir(parents=True,exist_ok=True)
                tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(state));tmp.replace(path)
        known=float(state.get('known_kwh',0));unknown=float(state.get('unknown_kwh',0))
        return {'known_kwh':round(known,2),'unknown_kwh':round(unknown,2),
                'known_cost_dkk_kwh':round(state.get('cost_dkk',0)/known,3) if known>.001 else None,
                'basis':'Estimat: vægtet netpris inkl. ladetab; soloverskud 0,03 kr./kWh. Historisk og umålt energi er ukendt.'}


def optimization_plan(sample, prices, weather):
    if not sample:
        return {"status": "waiting", "message": "Venter på målinger.", "hours": []}
    now = datetime.now(ZoneInfo("Europe/Copenhagen"))
    weather, weather_info = planning_weather(weather,CACHE.get("weather_updated"),CACHE.get("weather_error"),now)
    slots, count = forecast_slots(prices, weather, now)
    if not slots:
        return {"status": "waiting", "message": "Venter på fremtidige priser.", "hours": []}
    plan = energy_plan()
    capacity = float(plan["battery_capacity_kwh"])
    eta = (plan["roundtrip_efficiency_percent"]/100)**.5
    soc = float(sample.get("soc") or 0)
    initial_soc = soc
    cheap, expensive = limits(prices, now)
    dynamic = reserve(slots, plan)
    # Do not release a Tesla allowance until a refill opportunity is covered.
    allowance = max(0, (soc-dynamic["reserve_soc"])*capacity/100*eta) if dynamic["forecast_complete"] else 0
    dynamic["tesla_available_kwh"] = round(allowance, 2)
    dynamic["actual_soc"] = initial_soc
    rows = economic_dispatch(slots, power_limited_plan(plan, sample), initial_soc)
    if rows:
        dynamic['reserve_soc'] = rows[0]['reserve_soc']
        dynamic['tesla_available_kwh'] = round(max(0, (initial_soc-dynamic['reserve_soc'])*capacity/100*eta),2) if dynamic['forecast_complete'] else 0
    return {"status": "learning", "message": "Samlet prisplan for kendte timer; sol vægtet 70 %, husforbrug med sikkerhedsmargin.",
            "created": now.isoformat(), "hours_available": len(rows), "learning_samples": count,
            "cheap_limit": cheap, "expensive_limit": expensive, "battery_capacity_kwh": capacity,
            "reserve_soc": dynamic["reserve_soc"], "target_soc": plan["target_soc"],
            "initial_soc": initial_soc, "dynamic_reserve": dynamic, **weather_info,
            "battery_sale_enabled": False, "read_only": True, "hours": rows}


def planner(sample, prices, weather):
    model = optimization_plan(sample, prices, weather)
    dynamic = model.get("dynamic_reserve", {})
    details = {"soc": sample.get("soc"), "price_dkk_kwh": sample.get("price_dkk"),
               "cheap_limit": model.get("cheap_limit"), "expensive_limit": model.get("expensive_limit"),
               "dynamic_reserve": dynamic}
    if not model.get("hours"):
        return "hold", "Venter på prisdata", model["message"], details
    row = model["hours"][0]
    if sample.get("soc", 0) < dynamic.get("reserve_soc", 0):
        return "protect", "Reserven til huset er begrænset", "Tesla bør bruge nettet eller vente; vurder næste genopladningsmulighed.", details
    titles = {"grid_charge": "Netopladning foreslås", "solar_charge": "Gem soloverskud",
              "use_battery": "Brug batteriet til huset", "hold": "Bevar fleksibiliteten"}
    return row["action"], titles[row["action"]], row["reason"], details


def tesla_charge_schedule(sample, prices):
    return charge_schedule(sample or {}, prices, energy_plan(), datetime.now(ZoneInfo("Europe/Copenhagen")))


def maybe_send_tesla_reminder(sample, prices):
    """Send at most one plug-in reminder for each departure window."""
    if not sample or sample.get("tesla_soc") is None or sample.get("tesla_connected") not in (False, 0):
        return
    schedule = tesla_charge_schedule(sample, prices)
    if schedule["energy_needed_kwh"] <= 0:
        return
    now = datetime.now(ZoneInfo("Europe/Copenhagen"))
    arrival = datetime.fromisoformat(schedule["arrival"])
    plug_by = datetime.fromisoformat(schedule["plug_in_by"])
    # Notify when the normal home window begins; waiting until the cheap slot
    # could make the reminder arrive after the household has gone to bed.
    weekend_day = datetime.fromisoformat(schedule["departure"])-timedelta(seconds=1)
    if weekend_day.weekday() in (5,6):
        quiet_end = now.replace(hour=8,minute=30,second=0,microsecond=0)
        if now.weekday() in (5,6) and (now < quiet_end or now.hour >= 22):
            return
        if now.weekday() not in (5,6):
            # Daytime weekend charging is announced after 08:30 on that day.
            if plug_by.date()>now.date() and (plug_by.hour,plug_by.minute)>=(8,30):
                return
            if now < arrival or now.hour >= 22:
                return
    elif now < arrival or now.hour < 12:
        return
    if not schedule["prices_complete"]:
        plug_by = max(now, arrival)
    key = schedule["departure"][:10] + ":" + str(schedule["target_soc"])
    previous = {}
    try:
        previous = json.loads(TESLA_REMINDER_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if previous.get("key") == key:
        return
    hours = schedule["selected_hours"]
    if schedule["status"] == "ready" and hours:
        first = datetime.fromisoformat(hours[0]["time"]).strftime("%H:%M")
        last_end = datetime.fromisoformat(hours[-1]["end"]).strftime("%H:%M")
        cheap_text = f"De valgte billige ladetimer ligger mellem kl. {first} og {last_end}."
    else:
        cheap_text = "Hele ladevinduets priser er ikke kendt eller ladetiden er utilstrækkelig. Tilslut bilen ved hjemkomst; planen er foreløbig."
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


def tesla_shadow_plan(sample, prices, optimization=None):
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
        action, title, explanation = "disconnected", "Tesla er ikke tilsluttet", "Ladeforslaget beregnes på forhånd. Tilslut bilen for at kunne lade i de valgte timer."
    elif car_soc >= target:
        action, title = "stop" if charging else "complete", "Tesla har nået målet"
        explanation = f"Bilen er på {car_soc:.0f} %, og grænsen er {target} %. Shadow Mode ville stoppe opladningen."
    else:
        cheap, _ = limits(prices, datetime.now(ZoneInfo("Europe/Copenhagen")))
        price = sample.get("price_dkk")
        home_soc = float(sample.get("soc") or 0)
        reserve = float((optimization or {}).get("reserve_soc", plan.get("minimum_soc_morning", 20)))
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
    if connected and car_soc < target:
        if schedule["status"] != "ready":
            action, title, explanation = "wait", "Ladeplanen er foreløbig", "Venter på fuldt prisvindue eller tilstrækkelig ladetid."
        else:
            now = datetime.now(ZoneInfo("Europe/Copenhagen"))
            selected_now = any(datetime.fromisoformat(r["time"]) <= now < datetime.fromisoformat(r["end"])
                               for r in schedule["selected_hours"])
            action = "grid_charge" if selected_now else "wait"
            title = "Valgt ladetid nu" if selected_now else "Vent til den valgte ladetid"
            explanation = "Netprisplanen er beregnet frem til afgang. Batteribidrag vurderes separat ud fra husreserven."
    return {"action": action, "title": title, "explanation": explanation,
            "connected": connected, "charging": charging, "soc": car_soc,
            "target_soc": target, "departure_time": plan.get("tesla_departure_time", "07:00"),
            "charge_schedule": schedule,
            "dynamic_reserve": (optimization or {}).get("dynamic_reserve"),
            "read_only": True}


def tesla_energy_today():
    """Integrate measured charger power since midnight.

    Tesla's charge-energy-added entity can retain an old session or cumulative
    value, so treating its first value as today's energy can produce impossible
    results. Power integration is independent of that counter.
    """
    local_tz = ZoneInfo("Europe/Copenhagen")
    start_local = datetime.now(local_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc).isoformat()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM samples "
            "WHERE created>=? AND tesla_charger_power_kw IS NOT NULL ORDER BY id",
            (start_utc,),
        ).fetchall()
    total_kwh = 0.0
    previous = None
    for row in rows:
        try:
            stamp = datetime.fromisoformat(row["created"])
            power_kw = validated_tesla_power(dict(row))
            if power_kw is None:
                previous = None
                continue
        except (TypeError, ValueError):
            continue
        if previous is not None:
            seconds = (stamp - previous[0]).total_seconds()
            if not 0 < seconds <= 120:
                previous = (stamp, power_kw, row["tesla_charging_state"])
                continue
            charging = (str(row["tesla_charging_state"] or "").lower() == "charging" or
                        str(previous[2] or "").lower() == "charging")
            if charging or power_kw > 0.05 or previous[1] > 0.05:
                total_kwh += ((previous[1] + power_kw) / 2.0) * seconds / 3600.0
        previous = (stamp, power_kw, row["tesla_charging_state"])
    return round(total_kwh, 2)


def read_optional_telemetry(regs):
    errors = []
    clock_reference = None
    # Isolated small reads: one unsupported register must not discard core data.
    for start, count in [(62,3), (108,2), (115,2), (127,4), (210,8),
                         (259,2), (1100,2), (1104,2), (1109,2)]:
        try:
            before = datetime.now(ZoneInfo("Europe/Copenhagen"))
            rows = read_holding(start, count)
            after = datetime.now(ZoneInfo("Europe/Copenhagen"))
            for item in rows:
                regs[item["address"]] = item
            if start == 62:
                clock_reference = before+(after-before)/2
        except Exception as exc:
            errors.append({"start":start,"count":count,"error":str(exc)})
    raw = {address:item.get("uint16") for address,item in regs.items()
           if address in TELEMETRY_REGISTERS or address == 587}
    return decode_telemetry(raw, clock_reference, errors)


def collect_sample():
    regs = {}
    for start, count in [(117, 1), (141, 37), (514, 16), (587, 5), (616, 4), (650, 4), (672, 2)]:
        for item in read_holding(start, count):
            regs[item["address"]] = item
    telemetry = read_optional_telemetry(regs)
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
        "telemetry_json": json.dumps(telemetry, ensure_ascii=False),
    }
    for key in [*TESLA_ENTITIES, "tesla_power_updated"]:
        sample[key] = tesla.get(key)
    action, title, explanation, details = planner(sample, prices, weather)
    with db() as conn:
        columns = [
            "created", "soc", "battery_w", "grid_w", "grid_l1_w", "grid_l2_w", "grid_l3_w",
            "load_w", "pv_w", "pv1_w", "pv2_w", "battery_charge_today",
            "battery_discharge_today", "grid_import_today", "grid_export_today", "load_today",
            "pv_today", "reg117", "reg141", "reg142", "reg145", "reg146", "tou_json",
            "price_dkk", "solar_tomorrow", *TESLA_ENTITIES.keys(), "tesla_power_updated", "telemetry_json",
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
    try:
        MONTHLY_REGISTER.ingest(sample,prices)
    except Exception as exc:
        print("Monthly register failed: %s"%exc,flush=True)
    try:
        battery_cost(sample)
    except Exception as exc:
        print('Battery cost register failed: %s'%exc,flush=True)
    maybe_send_tesla_reminder(sample, prices)
    publish_home_operation(sample, prices, weather)
    return sample


def publish_home_operation(sample, prices, weather):
    """Publish a native HA entity for movable Lovelace cards; no inverter writes."""
    try:
        model=optimization_plan(sample,prices,weather)
        rows=model.get('hours',[]);current=rows[0] if rows else {}
        nxt=next((r for r in rows[1:] if r.get('action')!=current.get('action')),{})
        a=AUTO_CONTROL.report()
        names={'use_battery':'Batteri til huset','grid_charge':'Opladning fra nettet','hold':'Bevar batterireserve','solar_charge':'Gem soloverskud'}
        labels={'active':'Aktiv','disabled':'Deaktiveret','fault':'Stoppet efter fejl','restore_failed':'Gendannelse ikke bekræftet','verifying_restore':'Efterkontrol','restoring':'Gendanner','enabling':'Aktiverer'}
        ha_request('/states/sensor.deye_ai_drift','POST',{'state':labels.get(a.get('status'),'Ukendt'),'attributes':{
            'friendly_name':'Deye AI · drift nu og næste','icon':'mdi:home-lightning-bolt',
            'nu':names.get(current.get('action'),'Afventer plan'), 'naeste':names.get(nxt.get('action'),'Afventer næste plan'),
            'naeste_tid':nxt.get('time',''),'bekraeftet':names.get((a.get('command') or {}).get('action'),'Ingen'),
            'opdateret':datetime.now(ZoneInfo('Europe/Copenhagen')).isoformat(),'fejl':a.get('error') or ''}})
    except Exception as exc:
        print('Home dashboard entity update failed: %s'%exc,flush=True)


def daily_accounting(prices):
    now = datetime.now(ZoneInfo("Europe/Copenhagen"))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = (start.astimezone(timezone.utc)-timedelta(seconds=120)).isoformat()
    with db() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM samples WHERE created>=? ORDER BY id", (cutoff,))]
    return daily_energy(rows, prices, now, validated_tesla_power)


def night_ev_charging(sample, now=None):
    now = now or datetime.now(ZoneInfo('Europe/Copenhagen'))
    if 7 <= now.hour < 18:
        return False
    # Prefer the locally selected EVSE. Reading never sends a charger command.
    try:
        data = victron_state()
        fields = data.get('fields', {})
        power = fields.get('total_power', {})
        updated = datetime.fromisoformat(power.get('updated') or '')
        if 0 <= now.timestamp()-updated.timestamp() <= 120:
            watts = float(power['state']) * (1000 if power.get('unit') == 'kW' else 1)
            if watts > 100:
                return True
        status = fields.get('charger_status', {})
        updated = datetime.fromisoformat(status.get('updated') or '')
        if 0 <= now.timestamp()-updated.timestamp() <= 120 and str(status.get('state','')).lower() in ('charging','start_charging'):
            return True
    except Exception:
        pass
    power = validated_tesla_power(sample)
    return power is not None and power > .1


def automation_context():
    if PLAN_FILE.exists():
        json.loads(PLAN_FILE.read_text())  # Corrupt saved settings must not silently arm using defaults.
    with db() as conn:
        row=conn.execute('SELECT * FROM samples ORDER BY id DESC LIMIT 1').fetchone()
    sample=dict(row) if row else {}
    prices,weather=market_data()
    model=optimization_plan(sample,prices,weather)
    DAY_PLANS.update(model)
    return dict(options=options(),plan=energy_plan(),sample=sample,optimization=model,tesla_power_kw=validated_tesla_power(sample),
                price_source=CACHE.get('price_source'),price_updated=CACHE.get('price_updated',0),
                weather_updated=CACHE.get('weather_updated',0),weather_error=CACHE.get('weather_error'),
                night_ev_charging=night_ev_charging(sample))


def day_plan_view(model):
    now=datetime.now(ZoneInfo('Europe/Copenhagen'))
    cutoff=now.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat()
    with db() as conn:
        samples=[dict(r) for r in conn.execute('SELECT created,soc,battery_w,grid_w FROM samples WHERE created>=? ORDER BY id',(cutoff,))]
    return DAY_PLANS.view(model,samples,AUTO_CONTROL.report().get('events',[]),now)


PILOT_FILE = DATA_DIR / 'pilot_observations.json'
PILOT_LOCK = threading.RLock()
PILOT_RESULTS = {'observed':'Manuelt observeret','not_observed':'Manuel afvigelse','not_tested':'Ikke afprøvet'}
PILOT_STEPS = [
    (1,'Udgangspunkt','Aflæs begge BMS: SOC, strøm, temperaturer og alarmer. Kontrollér friske invertermålinger. Inverterkredsen er sikret med 25 A.',False),
    (2,'Gennemgå planen','Kontrollér pris, solprognose, SOC-reserve og næste handling i planen. Gennemgangen udløser ingen kommando.',False),
    (3,'Automatisk ladestart','Aktivér automatisk styring under opsyn; tilbagelæs kommando og bekræft faktisk ladning. Maks. 190 A samlet opladning.',False),
    (4,'Stop under ladning','Deaktivér-knappen skal afslutte planstyringen og bekræfte de oprindelige indstillinger. Inverterens oprindelige drift kan derefter fortsætte.',False),
    (5,'Følg en planperiode','Handlingen skal følge planens tidspunkt, mål og begrundelse under opsyn.',False),
    (6,'Sol og dyre timer','Sammenhold soloverskud, batteriladning og husets batteriforbrug med de relevante planperioder. Observation alene beviser ikke automatisk styring.',False),
    (7,'Evaluér og hent analyse','Hent analysepakken med plan, målinger, tidslog, gamle testjournaler og dine observationer. Sammenlign plan og faktisk forløb.',False),
]


def pilot_records():
    with PILOT_LOCK:
        try:
            rows = json.loads(PILOT_FILE.read_text())
            return [r for r in rows if isinstance(r,dict)][-100:] if isinstance(rows,list) else []
        except (FileNotFoundError,json.JSONDecodeError):
            return []


def pilot_view(sample, optimization):
    now = datetime.now(timezone.utc)
    fresh = False
    if sample:
        try:
            stamp = datetime.fromisoformat(sample['created'])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            fresh = (0 <= (now-stamp).total_seconds() <= 120 and all(type(sample.get(k)) in (int,float) and math.isfinite(sample[k]) for k in ('soc','battery_w','grid_w','pv_w','load_w')))
        except (KeyError,TypeError,ValueError):
            pass
    current = None
    for row in optimization.get('hours',[]):
        try:
            stamp = datetime.fromisoformat(row['time'])
            if stamp.tzinfo is not None and stamp <= now < stamp+timedelta(hours=1):
                current = row
                break
        except (KeyError,TypeError,ValueError):
            pass
    following = next((r for r in optimization.get('hours',[]) if current and r['time']>current['time'] and r['action']!=current['action']),None)
    comparison = 'Målingen kan ikke sammenholdes med en aktuel planperiode.'
    observed = {k:sample.get(k) for k in ('soc','battery_w','grid_w','pv_w','load_w','grid_l1_w','grid_l2_w','grid_l3_w')} if sample else None
    if fresh and current and observed:
        power = observed.get('battery_w')
        if isinstance(power,(float,int)):
            comparison = ('Batteriet lader.' if power < -100 else 'Batteriet aflader.' if power > 100 else 'Batteriet er omtrent i ro.')+' Dette er en måling, ikke bekræftelse af en udført plankommando.'
    records = pilot_records()
    steps = []
    for number,title,expected,blocked in PILOT_STEPS:
        last = next((r for r in reversed(records) if r.get('step')==number),None)
        steps.append(dict(id=number,title=title,expected=expected,blocked=blocked,last=last))
    blockers = []
    if not fresh:
        blockers.append('Målingen mangler, er ugyldig eller er over 120 sekunder gammel.')
    if not current:
        blockers.append('Der mangler en pris- og prognoseplan for det aktuelle tidspunkt.')
    return dict(mode='Driftsplan og observationer · aktivering styres i Automatisk styring',automatic_control=AUTO_CONTROL.state().get('status')=='active',
                inverter_circuit_fuse_ampere=25, battery_charge_limit_a=190, created=now.isoformat(),plan_created=optimization.get('created'),sample_created=sample.get('created') if sample else None,
                current=current,next=following,observed=observed,fresh=fresh,comparison=comparison,
                blockers=blockers,steps=steps,records=records)


def save_pilot_observation(body):
    step,result,note = body.get('step'),body.get('result'),body.get('note','')
    if type(step) is not int or not 1 <= step <= 7 or (not isinstance(result,str) or result not in PILOT_RESULTS) or not isinstance(note,str) or len(note)>1000:
        raise ValueError('Vælg et gyldigt kontroltrin, resultat og højst 1000 tegn notat.')
    if PILOT_STEPS[step-1][3] and result=='observed':
        raise ValueError('Dette trin afventer styringsopdatering; det kan ikke markeres observeret endnu.')
    view = dashboard_data()['pilot']
    record = dict(at=datetime.now(timezone.utc).isoformat(),step=step,result=result,result_label=PILOT_RESULTS[result],note=note,
                  evidence_type='manual_observation',observation_issued_command=False,automatic_control_active=view['automatic_control'],
                  sample_created=view['sample_created'],fresh=view['fresh'],planned=view['current'],observed=view['observed'])
    with PILOT_LOCK:
        records = (pilot_records()+[record])[-100:]
        temp = PILOT_FILE.with_suffix('.tmp')
        with temp.open('w') as f:
            json.dump(records,f,ensure_ascii=False,indent=2)
            f.flush()
            os.fsync(f.fileno())
        temp.replace(PILOT_FILE)
    return pilot_view({'created':view['sample_created'],**(view['observed'] or {})} if view['sample_created'] else None,
                      {'created':view['plan_created'],'hours':[r for r in (view['current'],view['next']) if r]})


def legacy_tests():
    rows = []
    for key,name,test,finished in [('charge20','Tidligere 90 A-test',TWENTY_TEST,('idle','complete','failed')),
                                    ('suite','Tidligere samlet test',TEST_SUITE,('idle','complete','failed')),
                                    ('charge','Tidligere manuel test',CHARGE_TEST,('idle','restored'))]:
        state = test.state()
        rows.append(dict(key=key,name=name,status=state.get('status','idle'),busy=state.get('status','idle') not in finished,
                         error=state.get('error') or state.get('message'),baseline=state.get('baseline') or state.get('original'),
                         current_registers=state.get('current_registers'),restore_errors=state.get('restore_errors')))
    return rows


def dashboard_data():
    with db() as conn:
        sample = conn.execute("SELECT * FROM samples ORDER BY id DESC LIMIT 1").fetchone()
        decisions = conn.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 20").fetchall()
    sample_data = dict(sample) if sample else None
    market = dashboard_market(sample_data)
    prices, weather = market_data()
    accounting = daily_accounting(prices)
    if sample_data:
        sample_data["tesla_energy_today_kwh"] = accounting["car_kwh"]
    optimization = optimization_plan(sample_data, prices, weather)
    DAY_PLANS.update(optimization)
    return {"sample": sample_data, **market, "optimization": optimization, "pilot": pilot_view(sample_data, optimization), "automation": AUTO_CONTROL.report(), "day_plans": day_plan_view(optimization),
            "modbus_telemetry": json.loads(sample_data["telemetry_json"]) if sample_data and sample_data.get("telemetry_json") else None,
            "daily_energy": accounting,
            "last_tesla_reminder": last_tesla_reminder(),
            "tesla_shadow": tesla_shadow_plan(sample_data, prices, optimization),
            "decisions": [{**dict(x), "details": json.loads(x["details_json"])} for x in decisions],
            "read_only": bool(options()['read_only']), "automatic_writes": AUTO_CONTROL.state().get("status")=="active"}


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
        "read_only": bool(options()['read_only']),
        "commissioning_enabled": bool(options().get('commissioning_enabled', False)),
        "automatic_control": AUTO_CONTROL.state().get("status")=="active",
    }
    files = {
        "manifest.json": manifest,
        "modbus_diagnostics.json": modbus_diagnostics(),
        "clock_diagnostics.json": CLOCK_DIAGNOSTICS.report(),
        "charge20_test.json": TWENTY_TEST.report(),
        "test_suite.json": TEST_SUITE.report(),
        "charge_test.json": {k: v for k, v in CHARGE_TEST.state().items() if k != 'endpoint'},
        "configuration_safe.json": safe_cfg,
        "energy_plan.json": energy_plan(),
        "automatic_control.json": AUTO_CONTROL.report(),
        "battery_cost.json": battery_cost(),
        "control_alerts.json": json.loads((DATA_DIR / "control_alerts.json").read_text()) if (DATA_DIR / "control_alerts.json").exists() else {},
        "clock_sync.json": json.loads((DATA_DIR / "clock_sync.json").read_text()) if (DATA_DIR / "clock_sync.json").exists() else {},
        "two_day_plan.json": DAY_PLANS.view(optimization,samples,AUTO_CONTROL.report().get("events",[])),
        "pilot_validation.json": {"automatic_control": AUTO_CONTROL.state().get("status")=="active", "records": pilot_records()},
        "samples.json": samples,
        "decisions.json": decisions,
        "market_and_forecast.json": market,
        "optimization_plan_48h.json": optimization,
        "daily_energy.json": daily_accounting(prices),
        "tesla_shadow.json": tesla_shadow_plan(samples[-1] if samples else None, prices, optimization),
        "tesla_reminder.json": last_tesla_reminder(),
        "modbus_telemetry.json": json.loads(samples[-1]["telemetry_json"]) if samples and samples[-1].get("telemetry_json") else None,
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
        with modbus_operation("probe"), socket.create_connection((str(cfg["deye_host"]), int(cfg["deye_port"])), timeout=2):
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


def write_test_register(address, value):
    with modbus_operation("write", address, 1):
        # Private commissioning transport; never expose arbitrary register writes.
        if type(value) is not int or not ((address in (108, 128) and 0 <= value <= 240) or (address == 167 and value in (15, 30)) or (address == 150 and 0 <= value <= 2359 and value % 100 < 60)):
            raise ValueError('Register eller værdi uden for testens område')
        cfg = options()
        transaction = int(time.time() * 1000) & 0xffff
        pdu = struct.pack('>BHHBH', 16, address, 1, 2, value)
        packet = struct.pack('>HHHB', transaction, 0, len(pdu) + 1, int(cfg['slave_id'])) + pdu
        with socket.create_connection((str(cfg['deye_host']), int(cfg['deye_port'])), timeout=int(cfg['timeout_seconds'])) as sock:
            sock.sendall(packet)
            header = recv_exact(sock, 7)
            tx, protocol, length, unit = struct.unpack('>HHHB', header)
            if tx != transaction or protocol != 0 or unit != int(cfg['slave_id']) or not 2 <= length <= 254:
                raise RuntimeError('Uventet Modbus-skrivesvar')
            response = recv_exact(sock, length - 1)
        if response != struct.pack('>BHH', 16, address, 1):
            raise RuntimeError('Modbus-skrivning blev ikke bekræftet')


def write_test_tou(values):
    # Private bounded six-register transport; not exposed as an arbitrary API action.
    if type(values) is not list or len(values) != 6 or any(type(v) is not int or not 0 <= v <= 2359 or v % 100 > 59 for v in values):
        raise ValueError('TOU-test kræver præcis seks gyldige HHMM-tider.')
    with modbus_operation('write', 148, 6):
        cfg = options()
        transaction = int(time.time()*1000) & 0xffff
        pdu = struct.pack('>BHHB6H',16,148,6,12,*values)
        packet = struct.pack('>HHHB',transaction,0,len(pdu)+1,int(cfg['slave_id']))+pdu
        with socket.create_connection((str(cfg['deye_host']),int(cfg['deye_port'])), timeout=int(cfg['timeout_seconds'])) as sock:
            sock.sendall(packet)
            header = recv_exact(sock,7)
            tx,protocol,length,unit = struct.unpack('>HHHB',header)
            if tx != transaction or protocol != 0 or unit != int(cfg['slave_id']) or not 2 <= length <= 254:
                raise RuntimeError('Uventet TOU-skrivesvar')
            response = recv_exact(sock,length-1)
        if response != struct.pack('>BHH',16,148,6):
            raise RuntimeError('TOU-skrivning blev ikke bekræftet')


def write_automatic_values(address, values):
    if not valid_auto_write(address,values):
        raise ValueError('Automatisk kommando uden for tilladte registre/værdier.')
    with modbus_operation('write',address,len(values)):
        cfg=options();transaction=int(time.time()*1000)&0xffff
        count=len(values)
        pdu=struct.pack('>BHHB',16,address,count,count*2)+struct.pack('>'+'H'*count,*values)
        packet=struct.pack('>HHHB',transaction,0,len(pdu)+1,int(cfg['slave_id']))+pdu
        with socket.create_connection((str(cfg['deye_host']),int(cfg['deye_port'])),timeout=int(cfg['timeout_seconds'])) as sock:
            sock.sendall(packet);header=recv_exact(sock,7)
            tx,protocol,length,unit=struct.unpack('>HHHB',header)
            if tx!=transaction or protocol!=0 or unit!=int(cfg['slave_id']) or not 2<=length<=254:
                raise RuntimeError('Ugyldigt automatisk Modbus-svar')
            response=recv_exact(sock,length-1)
        if response!=struct.pack('>BHH',16,address,count):
            raise RuntimeError('Automatisk Modbus-skrivning ikke bekræftet')


MONTHLY_REGISTER = MonthlyRegister(DATA_DIR / 'monthly_register.db')

CLOCK_DIAGNOSTICS = ClockDiagnostics(DATA_DIR / 'clock_diagnostics.db', read_holding)
CHARGE_TEST = ChargeTest(DATA_DIR / 'charge_test.json', read_holding, write_test_register, options)
CHARGE_TEST.write_tou = write_test_tou


TEST_SUITE = TestSuite(DATA_DIR / 'test_suite.json', CHARGE_TEST)
TWENTY_TEST = Charge20Test(DATA_DIR / 'charge20_test.json', CHARGE_TEST)
TEST_SUITE.other_busy = TWENTY_TEST.busy
TWENTY_TEST.other_busy = TEST_SUITE.busy
DAY_PLANS = DayPlans(DATA_DIR / 'two_day_plan_history.json')
AUTO_CONTROL = AutomaticControl(DATA_DIR / 'automatic_control.json',read_holding,write_automatic_values,
    automation_context,CHARGE_TEST.endpoint,CHARGE_TEST.lock,
    lambda: TEST_SUITE.busy() or TWENTY_TEST.busy() or CHARGE_TEST.state().get('status','idle') not in ('idle','restored'))
CHARGE_TEST.observation_paused = lambda: TWENTY_TEST.busy() or AUTO_CONTROL.busy()


def maintenance_proxy(handler, method, suffix, body=None):
    """Fixed loopback service, never a caller-supplied hostname or URL."""
    if suffix.split('?')[0] not in ('status', 'ui', 'logs', 'preview', 'grant', 'apply'):
        return handler.send_json({'error': 'Ukendt vedligeholdelseskald'}, 404)
    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request('http://127.0.0.1:8100/'+suffix, data=payload, method=method,
                                 headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(req, timeout=60)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        data = response.read(1000001)
        if len(data)>1000000: raise ValueError('Svar for stort')
        handler.send_response(response.code)
        handler.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
        handler.send_header('Content-Length', str(len(data)))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.end_headers();handler.wfile.write(data)


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
            if path == '/api/private/runtime-health':
                return self.send_json({'ready': True, 'version': APP_VERSION})
            if path.startswith('/api/private/maintenance/'):
                suffix = path.removeprefix('/api/private/maintenance/')
                if suffix not in ('status', 'ui', 'logs'):
                    return self.send_json({'error': 'Ukendt læsekald'}, 404)
                query = urlparse(self.path).query
                return maintenance_proxy(self, 'GET', suffix+('?' + query if query else ''))
            if path == '/api/private/history':
                params = urllib.parse.parse_qs(urlparse(self.path).query)
                if set(params) - {'after_id', 'window_end'} or any(len(v)!=1 for v in params.values()):
                    return self.send_json({'error': 'Ugyldige parametre'}, 400)
                return self.send_json(read_history(DB_FILE, int(params.get('after_id', ['0'])[0]),
                                                  params.get('window_end', [None])[0]))
            if path == '/api/private/source':
                params = urllib.parse.parse_qs(urlparse(self.path).query)
                if set(params) - {'name'} or any(len(v)!=1 for v in params.values()):
                    return self.send_json({'error': 'Ugyldige parametre'}, 400)
                return self.send_json(read_source(APP_DIR, params.get('name', [None])[0]))
            if path.endswith('/api/tesla-selection'):
                return self.send_json(tesla_devices())
            if path.endswith('/api/battery-cost'):
                return self.send_json(battery_cost())
            if path.endswith('/api/victron'):
                return self.send_json(victron_state())
            if path.endswith('/api/monthly-register'):
                return self.send_json({'months':MONTHLY_REGISTER.months()})
            if path.endswith('/api/monthly-register/excel'):
                params=urllib.parse.parse_qs(urlparse(self.path).query)
                month=params.get('month',[''])[0]
                payload=MONTHLY_REGISTER.download(month)
                self.send_response(200)
                self.send_header('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                self.send_header('Content-Disposition',f'attachment; filename=elregnskab-{month}.xlsx')
                self.send_header('Content-Length',str(len(payload)))
                self.send_header('Cache-Control','no-store')
                self.end_headers();self.wfile.write(payload);return
            if path.endswith('/api/automation'):
                return self.send_json(AUTO_CONTROL.report())
            if path.endswith('/api/legacy-tests'):
                return self.send_json(legacy_tests())
            if path.endswith('/api/charge20/report'):
                payload = json.dumps(TWENTY_TEST.report(), ensure_ascii=False, indent=2).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Disposition', 'attachment; filename=deye-90a-test.json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if path.endswith('/api/charge20'):
                return self.send_json(TWENTY_TEST.view())
            if path.endswith('/api/test-suite/report'):
                payload = json.dumps(TEST_SUITE.report(), ensure_ascii=False, indent=2).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Disposition', 'attachment; filename=deye-test-rapport.json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if path.endswith('/api/test-suite'):
                return self.send_json(TEST_SUITE.view())
            if path.endswith('/api/charge-test'):
                return self.send_json(CHARGE_TEST.view())
            if path.endswith("/api/clock-diagnostics"):
                return self.send_json(CLOCK_DIAGNOSTICS.report(False))
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
        # Shared lock covers state-changing handlers. Deployment takes the
        # exclusive lock, preventing a concurrent activation during its checks.
        if os.environ.get('DEYE_MANAGED_RUNTIME') == '1' and not urlparse(self.path).path.startswith('/api/private/maintenance/'):
            import fcntl
            with Path('/data/bridge_maintenance.operation').open('r') as operation:
                fcntl.flock(operation, fcntl.LOCK_SH)
                try:
                    if Path('/data/bridge_maintenance.blocked').exists():
                        return self.send_json({'error': 'Vedligeholdelse pågår'}, 503)
                    return self._handle_POST()
                finally: fcntl.flock(operation, fcntl.LOCK_UN)
        return self._handle_POST()

    def _handle_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 802048:
                return self.send_json({'error': 'Forespørgsel for stor'}, 413)
            body = json.loads(self.rfile.read(length) or b"{}")
            path = urlparse(self.path).path.rstrip("/")
            if path.startswith('/api/private/maintenance/'):
                suffix = path.removeprefix('/api/private/maintenance/')
                if suffix not in ('preview', 'grant', 'apply'):
                    return self.send_json({'error': 'Ukendt handling'}, 404)
                return maintenance_proxy(self, 'POST', suffix, body)
            if path.endswith('/api/tesla-selection'):
                if self.headers.get('X-Deye-Control') != '1':
                    return self.send_json({'error':'Manglende styringsheader'},403)
                return self.send_json(select_tesla(body))
            if path.endswith('/api/victron'):
                if self.headers.get('X-Deye-Control') != '1':
                    return self.send_json({'error':'Manglende styringsheader'},403)
                return self.send_json(victron_action(body))
            if path.endswith('/api/automation'):
                if self.headers.get('X-Deye-Control') != '1':
                    return self.send_json({'error':'Manglende styringsheader'},403)
                if body.get('action')=='enable':
                    return self.send_json(AUTO_CONTROL.enable(body.get('cloud_off')))
                if body.get('action')=='battery_timer':
                    return self.send_json(AUTO_CONTROL.battery_timer(body.get('mode'),body.get('hours')))
                if body.get('action')=='disable':
                    return self.send_json(AUTO_CONTROL.disable())
                raise ValueError('Ukendt styringshandling')
            if path.endswith('/api/pilot-observation'):
                return self.send_json(save_pilot_observation(body))
            if any(path.endswith(p) for p in ('/api/charge20','/api/test-suite','/api/charge-test')) and body.get('action') in ('start','restart'):
                raise ValueError('De gamle ladetest er udgået. Kun stop, gendannelse og rapporter er bevaret.')
            if path.endswith('/api/charge20'):
                if self.headers.get('X-Deye-Test') != '1':
                    return self.send_json({'error': 'Manglende test-header'}, 403)
                if body.get('action') == 'start':
                    return self.send_json(TWENTY_TEST.start(body))
                if body.get('action') == 'stop':
                    return self.send_json(TWENTY_TEST.restore())
                raise ValueError('Ukendt testhandling')
            if path.endswith('/api/test-suite'):
                if self.headers.get('X-Deye-Test') != '1':
                    return self.send_json({'error': 'Manglende test-header'}, 403)
                actions = {'start': lambda: TEST_SUITE.start(body), 'stop': TEST_SUITE.stop, 'restart': TEST_SUITE.begin_restart}
                if body.get('action') not in actions:
                    raise ValueError('Ukendt testhandling')
                return self.send_json(actions[body['action']]())
            if path.endswith('/api/charge-test'):
                with CHARGE_TEST.lock:
                    if TEST_SUITE.busy() or TWENTY_TEST.busy():
                        raise ValueError('Brug stopknappen i det aktive testkort.')
                    if self.headers.get('X-Deye-Test') != '1':
                        return self.send_json({'error': 'Manglende test-header'}, 403)
                    if body.get('action') == 'restore':
                        return self.send_json(CHARGE_TEST.restore())
                    if body.get('action') != 'start':
                        raise ValueError('Ukendt testhandling')
                    return self.send_json(CHARGE_TEST.start(body))
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
    with db() as conn:
        MONTHLY_REGISTER.backfill(dict(r) for r in conn.execute('SELECT created,telemetry_json,price_dkk FROM samples ORDER BY id'))
    MONTHLY_REGISTER.finalize()
    threading.Thread(target=CLOCK_DIAGNOSTICS.worker, daemon=True).start()
    threading.Thread(target=TEST_SUITE.worker, args=(TWENTY_TEST,), daemon=True).start()
    threading.Thread(target=collector_loop, daemon=True).start()
    threading.Thread(target=AUTO_CONTROL.worker,daemon=True).start()
    threading.Thread(target=control_alert_worker,daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()

