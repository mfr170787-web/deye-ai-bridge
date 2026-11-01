"""Explicit, bounded commissioning tests. No scheduling or BMS overrides."""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo
import threading
import time


class ChargeTest:
    def __init__(self, path, read, write, options):
        self.path, self.read, self.write, self.options = path, read, write, options
        self.lock = threading.RLock()

    def save(self, state):
        pending = self.path.with_suffix('.tmp')
        with pending.open('w') as f:
            f.write(json.dumps(state, indent=2))
            f.flush()
            os.fsync(f.fileno())
        pending.replace(self.path)

    def state(self):
        if not self.path.exists():
            return {'status': 'idle'}
        return json.loads(self.path.read_text())

    def value(self, address):
        return self.read(address, 1)[0]['uint16']

    def endpoint(self):
        c = self.options()
        return [c['deye_host'], c['deye_port'], c['slave_id']]

    def start(self, body):
        with self.lock:
            c = self.options()
            if c.get('read_only', True) or not c.get('commissioning_enabled', False):
                raise ValueError('Test er låst. Kræver read_only: false og commissioning_enabled: true i tilføjelsens konfiguration.')
            if body.get('copilot_off') is not True or body.get('supervised') is not True:
                raise ValueError('Bekræft at Copilot er slået fra, og at du overvåger testen.')
            if self.state().get('status') not in ('idle', 'restored'):
                raise ValueError('Afslut eller gendan den eksisterende test først.')
            soc_test = body.get('kind') == 'soc_time2'
            address = 167 if soc_test else {'charge_limit': 108, 'grid_limit': 128}.get(body.get('kind'))
            amps, seconds = body.get('amps'), body.get('seconds')
            if type(seconds) is not int or not 15 <= seconds <= 120:
                raise ValueError('Varighed skal være 15–120 sekunder.')
            if soc_test:
                self.soc_guard(seconds)
                requested = 30
            else:
                if address is None or type(amps) is not int or not 1 <= amps <= 10:
                    raise ValueError('Vælg ladegrænse og 1–10 A eller SOC-test af Time 2.')
                if not amps <= self.value(212) <= 500:
                    raise ValueError('BMS tillader ikke den valgte strøm, eller aflæsningen er ugyldig.')
                requested = amps
            original = self.value(address)
            if soc_test:
                if original != 15:
                    raise ValueError('Time 2 skal stå på 15 % før SOC-testen; målet testes ved 30 %.')
            elif not 0 <= original <= 240:
                raise ValueError('Oprindelig grænse er ugyldig; testen starter ikke.')
            if address == 128 and amps > original and self.value(108) > 10:
                raise ValueError('Sænk først den anden ladegrænse til højst 10 A med en separat test.')
            state = {'status': 'active', 'endpoint': self.endpoint(), 'started': time.time(),
                     'expires': time.time() + seconds, 'address': address,
                     'original': original, 'requested': requested, 'kind': body.get('kind'), 'before': self.measure(),
                     'message': 'Backup gemt; skrivning afventer.'}
            self.save(state)  # Persist before touching the inverter, including ambiguous timeouts.
            try:
                self.write(address, requested)
                actual = self.value(address)
                if actual != requested:
                    raise RuntimeError('Tilbagelæsning matcher ikke testværdien.')
                state.update(verified=actual, message='Testværdi tilbagelæst. Kontrollér den målte effekt.')
                self.save(state)
            except Exception as exc:
                state['message'] = str(exc)
                self.save(state)
                self.restore()
                raise
            return self.view()

    def soc_guard(self, seconds):
        # Only the manually verified Time 2 target, never remote SOC or other slots.
        limits = [self.value(108), self.value(128)]
        if not all(1 <= v <= 10 for v in limits) or not max(limits) <= self.value(212) <= 500:
            raise ValueError('SOC-test kræver begge ladegrænser på 1–10 A og gyldig BMS-tilladelse.')
        if self.value(111) != 1 or self.value(146) not in (1, 255) or self.value(173) != 1 or self.value(130) != 1:
            raise ValueError('SOC-styring, Time of Use og netladning i Time 2 skal være aktiveret.')
        if self.value(1100) != 0:
            raise ValueError('Remote-styring skal være deaktiveret under SOC-testen.')
        a, b, c = [self.value(i) for i in (62, 63, 64)]
        stamp = datetime(2000 + (a >> 8), a & 255, b >> 8, b & 255, c >> 8, c & 255)
        now = datetime.now(ZoneInfo('Europe/Copenhagen')).replace(tzinfo=None)
        if abs((stamp - now).total_seconds()) > 60:
            raise ValueError('Inverteruret afviger mere end ét minut.')
        start, end = self.value(149), self.value(150)
        def minute(v):
            if not 0 <= v // 100 <= 23 or not 0 <= v % 100 <= 59:
                raise ValueError('Ugyldigt klokkeslæt i Time 2.')
            return v // 100 * 60 + v % 100
        t = stamp.hour * 3600 + stamp.minute * 60 + stamp.second
        if not minute(start) * 60 <= t or t + seconds + 30 >= minute(end) * 60:
            raise ValueError('Testen skal ligge helt inden for Time 2 uden midnatsskift.')
        soc = self.value(588)
        if not 15 <= soc < 30:
            raise ValueError('SOC-test kræver batteriets SOC mellem 15 og 29 %.')

    def measure(self):
        rows = {r['address']: r for r in self.read(587, 5)}
        w = rows[590]['int16']
        return {'voltage_v': rows[587]['uint16'] / 100, 'soc': rows[588]['uint16'],
                'battery_w': w, 'charging_w': max(0, -w), 'measured_at': time.time()}

    def restore(self):
        with self.lock:
            s = self.state()
            if s.get('status') in ('idle', 'restored'):
                return s
            try:
                if s['endpoint'] != self.endpoint():
                    raise RuntimeError('Forbindelsen er ændret; gendannelse er blokeret.')
                current = self.value(s['address'])
                if current not in (s['requested'], s['original']):
                    raise RuntimeError('En anden styring har ændret værdien. Kontrollér inverteren manuelt; ingen overskrivning.')
                if current != s['original']:
                    self.write(s['address'], s['original'])
                if self.value(s['address']) != s['original']:
                    raise RuntimeError('Gendannelse kunne ikke bekræftes.')
                s.update(status='restored', message='Oprindelig indstilling gendannet og tilbagelæst.')
            except Exception as exc:
                s.update(status='restore_failed', message=str(exc))
            self.save(s)
            return s

    def view(self):
        with self.lock:
            s = self.state()
            s['enabled'] = not self.options().get('read_only', True) and self.options().get('commissioning_enabled', False)
            try:
                s['measurement'] = self.measure()
            except Exception as exc:
                s['measurement_error'] = str(exc)
            return s

    def worker(self):
        # On process restart, recover any unfinished test immediately.
        if self.state().get('status') not in ('idle', 'restored'):
            self.restore()
        while True:
            time.sleep(2)
            with self.lock:
                s = self.state()
                if s.get('status') == 'active' and s.get('kind') == 'soc_time2':
                    try:
                        self.soc_guard(max(0, s['expires'] - time.time()))
                    except Exception:
                        self.restore()
                        continue
                if s.get('status') == 'restore_failed' or (s.get('status') == 'active' and time.time() >= s['expires']):
                    self.restore()
