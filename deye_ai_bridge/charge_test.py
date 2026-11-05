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
                self.event(state, 'test_readback', value=actual)
                state.update(verified=actual, message='Testværdi tilbagelæst. Kontrollér den målte effekt.')
                self.save(state)
            except Exception as exc:
                state['message'] = str(exc)
                self.save(state)
                self.restore("start_error", str(exc))
                raise
            return self.view()

    def soc_guard(self, seconds, ceiling=10, recover_clock=False, time2_end_override=None, time2_start_override=None):
        # Existing tests retain 10 A; only the separate fixed 90 A test supplies 90.
        if type(ceiling) is not int or ceiling not in (10, 90):
            raise ValueError('Kun 10 A eller den særskilte 90 A-test er tilladt.')
        self.guard_snapshot = {'at': time.time(), 'registers': {}}
        def value(address):
            v = self.value(address)
            self.guard_snapshot['registers'][str(address)] = v
            return v
        # Only the manually verified Time 2 target, never remote SOC or other slots.
        limits = [value(108), value(128)]
        if not all(1 <= v <= ceiling for v in limits) or not max(limits) <= value(212) <= 500:
            raise ValueError(f'SOC-test kræver begge ladegrænser på 1–{ceiling} A og gyldig BMS-tilladelse.')
        if value(111) != 1 or value(146) not in (1, 255) or value(173) != 1 or value(130) != 1:
            raise ValueError('SOC-styring, Time of Use og netladning i Time 2 skal være aktiveret.')
        if value(1100) != 0:
            raise ValueError('Remote-styring skal være deaktiveret under SOC-testen.')
        start, end = value(149), value(150)
        if time2_start_override is not None:
            start = time2_start_override
        if time2_end_override is not None:
            end = time2_end_override
        def minute(v):
            if not 0 <= v // 100 <= 23 or not 0 <= v % 100 <= 59:
                raise ValueError('Ugyldigt klokkeslæt i Time 2.')
            return v // 100 * 60 + v % 100
        def within(stamp):
            t = stamp.hour * 3600 + stamp.minute * 60 + stamp.second
            return minute(start)*60 <= t and t+seconds+30 < minute(end)*60
        attempts = []
        recovered = False
        consecutive = 0
        deadline = time.monotonic()+5
        for attempt in range(8):
            if attempt:
                if time.monotonic() >= deadline:
                    break
                time.sleep(.5)
            clock_before = datetime.now(ZoneInfo('Europe/Copenhagen')).replace(tzinfo=None)
            clock_rows = self.read(62, 3)
            clock_after = datetime.now(ZoneInfo('Europe/Copenhagen')).replace(tzinfo=None)
            clock_values = {row['address']: row['uint16'] for row in clock_rows}
            self.guard_snapshot['registers'].update({str(k): v for k, v in clock_values.items()})
            self.guard_snapshot['clock_read_seconds'] = (clock_after-clock_before).total_seconds()
            a, b, c = [clock_values[i] for i in (62, 63, 64)]
            stamp = datetime(2000+(a >> 8), a & 255, b >> 8, b & 255, c >> 8, c & 255)
            now = clock_before+(clock_after-clock_before)/2
            offset = (stamp-now).total_seconds()
            evidence = dict(inverter_local=stamp.isoformat(), reference_local=now.isoformat(), offset_seconds=offset,
                            raw_registers={str(k):v for k,v in clock_values.items()})
            attempts.append(evidence)
            self.guard_snapshot['clock'] = evidence
            self.guard_snapshot['clock_attempts'] = attempts
            if not within(stamp) or not within(now):
                raise ValueError('Testen skal ligge helt inden for Time 2 ved både invertertid og dansk tid.')
            if abs(offset) <= 60:
                consecutive += 1
                if attempt == 0 or consecutive >= 2:
                    if attempt and time.monotonic() >= deadline:
                        break
                    recovered = True
                    break
            else:
                consecutive = 0
                # Only the observed one-hour-back pattern; never start on a bad clock.
                if not recover_clock or ceiling != 90 or not -3620 <= offset <= -3590:
                    raise ValueError('Inverteruret afviger mere end ét minut.')
        if not recovered:
            raise ValueError('Inverteruret blev ikke stabilt inden for den korte genaflæsning.')
        self.guard_snapshot['clock_recovered'] = len(attempts)>1
        soc = value(588)
        if not 15 <= soc < 30:
            raise ValueError('SOC-test kræver batteriets SOC mellem 15 og 29 %.')

    def measure(self):
        rows = {r['address']: r for r in self.read(587, 5)}
        w = rows[590]['int16']
        return {'voltage_v': rows[587]['uint16'] / 100, 'soc': rows[588]['uint16'],
                'battery_w': w, 'charging_w': max(0, -w), 'measured_at': time.time()}

    def event(self, s, action, **details):
        s.setdefault('events', []).append(dict(at=time.time(), action=action, **details))
        s['events'] = s['events'][-100:]

    def restore(self, reason="manual_stop", detail=None):
        with self.lock:
            s = self.state()
            if s.get('status') in ('idle', 'restored', 'verifying_restore', 'restore_mismatch'):
                return s
            if 'restore_reason' not in s:
                s['restore_reason'] = reason
                s['restore_detail'] = detail
                s['guard_snapshot'] = getattr(self, 'guard_snapshot', None)
                self.event(s, 'restore_requested', reason=reason, detail=detail)
                self.save(s)
            try:
                if s['endpoint'] != self.endpoint():
                    raise RuntimeError('Forbindelsen er ændret; gendannelse er blokeret.')
                current = self.value(s['address'])
                self.event(s, 'restore_read', value=current)
                if current not in (s['requested'], s['original']):
                    s['status'] = 'restore_mismatch'
                    raise RuntimeError('Værdien er ændret. Kontrollér inverteren manuelt; ingen overskrivning.')
                if current != s['original']:
                    self.event(s, 'restore_write', value=s['original'])
                    self.save(s)
                    self.write(s['address'], s['original'])
                actual = self.value(s['address'])
                self.event(s, 'restore_readback', value=actual)
                s['current_register'] = actual
                s['register_checked_at'] = time.time()
                if actual != s['original']:
                    raise RuntimeError('Gendannelse kunne ikke bekræftes.')
                s.update(status='verifying_restore', restore_started=time.time(),
                         restore_checks=1, message='Oprindelig værdi tilbagelæst én gang. Kontrolleres i 60 sekunder; endnu ikke godkendt.')
            except Exception as exc:
                if s.get('status') != 'restore_mismatch':
                    s['status'] = 'restore_failed'
                s['message'] = str(exc)
                self.event(s, 'restore_error', error=str(exc))
            self.save(s)
            return s

    def check_restore(self):
        # Read only: never chase a value changed after the initial restoration.
        with self.lock:
            s = self.state()
            if getattr(self, 'observation_paused', lambda: False)():
                return s
            if s.get('status') not in ('verifying_restore', 'restored', 'restore_mismatch'):
                return s
            try:
                if s['endpoint'] != self.endpoint():
                    raise RuntimeError('Forbindelsen er ændret; kontrol afventer.')
                actual = self.value(s['address'])
                s.update(current_register=actual, register_checked_at=time.time())
                if actual != s['original']:
                    s.update(status='restore_mismatch', message='Efterkontrol: registeret matcher ikke den oprindelige værdi. Ingen automatisk overskrivning. Gendan manuelt og læs igen.')
                    self.event(s, 'restore_mismatch', value=actual)
                elif s['status'] == 'restore_mismatch':
                    s.update(status='verifying_restore', restore_started=time.time(), restore_checks=1,
                             message='Oprindelig værdi ses igen. Ny efterkontrol i 60 sekunder.')
                    self.event(s, 'manual_restore_seen', value=actual)
                elif s['status'] == 'restored' and 'restore_started' not in s:
                    s.update(status='verifying_restore', restore_started=time.time(), restore_checks=1,
                             message='Ældre teststatus kontrolleres på ny i 60 sekunder.')
                elif s['status'] == 'verifying_restore':
                    s['restore_checks'] += 1
                    self.event(s, 'restore_check', value=actual)
                    if time.time() - s['restore_started'] >= 60 and s['restore_checks'] >= 3:
                        s.update(status='restored', restored_at=time.time(),
                                 message='Oprindelig værdi bekræftet ved gentagne aflæsninger over mindst 60 sekunder. Dette bekræfter registeret, ikke ladeeffektens stop.')
                s.pop('register_check_error', None)
            except Exception as exc:
                s['register_check_error'] = str(exc)
                # A failed read breaks the continuous verification interval.
                if s['status'] == 'verifying_restore':
                    s.update(restore_started=time.time(), restore_checks=0)
                elif s['status'] == 'restored':
                    s.update(status='verifying_restore', restore_started=time.time(), restore_checks=0,
                             message='Aktuel registerkontrol fejlede; gendannelse er ikke længere bekræftet.')
                self.event(s, 'check_error', error=str(exc))
            self.save(s)
            return s

    def view(self):
        with self.lock:
            s = self.check_restore()
            s['enabled'] = not self.options().get('read_only', True) and self.options().get('commissioning_enabled', False)
            try:
                s['measurement'] = self.measure()
            except Exception as exc:
                s['measurement_error'] = str(exc)
            return s

    def tick(self):
        with self.lock:
            s = self.state()
            status = s.get('status')
            if status in ('verifying_restore', 'restore_mismatch'):
                self.check_restore()
            elif status == 'active':
                try:
                    if time.time() >= s['expires']:
                        self.restore('expired', 'Testens varighed er udløbet.')
                    elif s.get('kind') == 'soc_time2':
                        self.soc_guard(max(0, s['expires'] - time.time()))
                except Exception as exc:
                    self.restore('guard_failed', str(exc))
            elif status == 'restore_failed':
                self.restore()

    def worker(self):
        # Recover unfinished writes on restart; completed tests are read-only checked.
        if self.state().get('status') not in ('idle', 'restored', 'verifying_restore', 'restore_mismatch'):
            self.restore('process_restart', 'Uafsluttet test fundet ved opstart.')
        while True:
            time.sleep(2)
            try:
                self.tick()
            except Exception:
                # A transient read or disk failure must not kill the recovery worker.
                continue
