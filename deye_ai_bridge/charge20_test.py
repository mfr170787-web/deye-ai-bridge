"""Fixed supervised 90 A test; restores all three settings, never overrides BMS."""
import json
import os
import time
import math
from datetime import datetime
from zoneinfo import ZoneInfo


class Charge20Test:
    BASE = {108: 10, 128: 10, 167: 15}
    TARGET = {108: 90, 128: 90, 167: 30}

    def __init__(self, path, charge):
        self.path, self.charge = path, charge
        self.monotonic_deadline = None

    def state(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {'status': 'idle'}

    def save(self, s):
        tmp = self.path.with_suffix('.tmp')
        with tmp.open('w') as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.path)

    def busy(self):
        return self.state().get('status') not in ('idle', 'complete', 'failed')

    def log(self, s, action, **detail):
        s.setdefault('events', []).append(dict(at=time.time(), action=action, **detail))
        s['events'] = s['events'][-300:]

    def registers(self, s=None):
        addresses = list(self.BASE)
        if s and '150' in s.get('baseline', {}):
            addresses.append(150)
        return {str(a): self.charge.value(a) for a in addresses}

    def time2_plan(self, seconds):
        start, end, following = [self.charge.value(a) for a in (149, 150, 151)]
        def minute(v):
            if type(v) is not int or not 0 <= v <= 2359 or v % 100 > 59:
                raise ValueError('Ugyldigt TOU-klokkeslæt; ingen forlængelse.')
            return v // 100 * 60 + v % 100
        a, b, c = minute(start), minute(end), minute(following)
        c = c or 1440
        now = datetime.now(ZoneInfo('Europe/Copenhagen'))
        current = now.hour*60 + now.minute + now.second/60
        needed = math.ceil(current + seconds/60 + 2)
        if not a < b < c or current < a or needed >= c or needed >= 1440:
            raise ValueError(f'Start afvist før skrivning. Time 2-start {start:04d}, slut {end:04d}, Time 4-start {following:04d}; testen kræver slut cirka {needed//60:02d}:{needed%60:02d}. Der skal være plads før Time 4 og midnat, og Time 2-start må ikke ligge i fremtiden. Tilpas TOU manuelt eller vælg en kortere test.')
        if needed <= b:
            return None
        return dict(start=start, original=end, following=following,
                    target=(needed//60)*100+needed%60)

    def check_time2_context(self, s):
        plan = s.get('time2_extension')
        if plan and (self.charge.value(149) != plan['start'] or self.charge.value(151) != plan['following']):
            raise ValueError('Nabogrænserne i TOU er ændret under testen.')

    def start(self, body):
        with self.charge.lock:
            c = self.charge.options()
            if getattr(self, 'other_busy', lambda: False)() or self.busy() or self.charge.state().get('status') not in ('idle', 'restored'):
                raise ValueError('Afslut den eksisterende test og efterkontrol først.')
            if c.get('read_only', True) or not c.get('commissioning_enabled', False):
                raise ValueError('Kræver read_only: false og commissioning_enabled: true.')
            if any(body.get(k) is not True for k in ('copilot_off', 'supervised', 'both_packs_ready')):
                raise ValueError('Bekræft Copilot slået fra, overvågning og adgang til begge BMS-appvisninger.')
            seconds = body.get("seconds", 120)
            if type(seconds) is not int or seconds not in (120, 600, 1800, 3600):
                raise ValueError("Vælg en test på 120, 600, 1800 eller 3600 sekunder.")
            extension = self.time2_plan(seconds) if body.get('extend_time2') is True else None
            self.charge.soc_guard(seconds, time2_end_override=extension['target'] if extension else None)
            if self.registers() != {str(a):v for a,v in self.BASE.items()}:
                raise ValueError('Start kræver 108=10 A, 128=10 A og 167=15 %.')
            if not 90 <= self.charge.value(212) <= 500:
                raise ValueError('Den tilkoblede BMS tillader ikke mindst 90 A.')
            self.monotonic_deadline = time.monotonic()+seconds
            s = dict(status='preparing', started=time.time(), expires=time.time()+seconds, duration_seconds=seconds,
                     endpoint=self.charge.endpoint(), baseline=self.registers(),
                     written=[], samples=[], events=[], stop_reason=None,
                     test_current_a=90, clock_policy='bounded_one_hour_back_recheck', timer_basis='minimum_of_wall_and_process_monotonic_deadline', target={str(a):v for a,v in self.TARGET.items()},
                     prepare_index=0, prepare_deadline=time.time()+25,
                     before=self.charge.measure())
            if extension:
                s['time2_extension'] = extension
                s['baseline']['150'] = extension['original']
                s['target']['150'] = extension['target']
                s.update(prepare_index=-1, confirm_started=None, confirm_checks=0)
            self.save(s)
            return self.view()

    def restore(self, reason='manual_stop'):
        with self.charge.lock:
            s = self.state()
            if s.get('status') in ('idle','complete','failed','verifying_restore','settling_restore'):
                return s
            s['stop_reason'] = s.get('stop_reason') or reason
            s['status']='restoring'
            self.save(s)
            errors=[]
            # Old journals still belong to the earlier 20 A test.
            target=s.get('target', {'108':20,'128':20,'167':30})
            if s['endpoint'] != self.charge.endpoint():
                errors.append('Forbindelsen er ændret; ingen skrivning.')
            else:
                # Lower the SOC demand first, then the grid and global limits.
                for a in (167,128,108,150):
                    if a not in s['written']:
                        continue
                    try:
                        actual=self.charge.value(a)
                        baseline = s['baseline'][str(a)]
                        if a == 150:
                            self.check_time2_context(s)
                            if any(self.charge.value(k) != v for k, v in self.BASE.items()):
                                raise ValueError('Time 2-grænsen er ikke gendannet, fordi 10 A / 15 % ikke er bekræftet. Gendan manuelt.')
                        if actual not in (baseline,target[str(a)]):
                            raise ValueError('Anden værdi ved '+str(a)+'; ingen overskrivning.')
                        # A stale baseline must not skip cancellation of a journalled write.
                        self.log(s,'restore_write',address=a,value=baseline)
                        self.save(s)
                        self.charge.write(a,baseline)
                        actual=self.charge.value(a)
                        self.log(s,'restore_readback',address=a,value=actual)
                        if actual not in (baseline, target[str(a)]):
                            raise ValueError('Uventet værdi efter gendannelse ved '+str(a))
                        if actual != baseline:
                            self.log(s, 'restore_pending', address=a, value=actual)
                    except Exception as exc:
                        errors.append(str(exc))
            s['restore_errors']=errors
            s.update(status='restore_failed' if errors else 'settling_restore',
                     settle_deadline=time.time()+10, restore_started=time.time(), restore_checks=0)
            self.save(s)
            return s

    def remaining(self, s):
        # A new process cannot resume an active test using a fresh time budget.
        if self.monotonic_deadline is None:
            return 0
        return max(0, min(s['expires']-time.time(), self.monotonic_deadline-time.monotonic()))

    def guarded_time(self, s):
        try:
            self.check_time2_context(s)
            if s.get('time2_extension') and self.charge.value(150) != s['target']['150']:
                raise ValueError('Time 2-sluttiden er ændret under testen.')
            self.charge.soc_guard(self.remaining(s), ceiling=90, recover_clock=True)
            if self.remaining(s) <= 0:
                raise ValueError('Testens tidsgrænse er nået.')
        finally:
            evidence = getattr(self.charge, 'guard_snapshot', {})
            if len(evidence.get('clock_attempts', [])) > 1:
                self.log(s, 'clock_recheck', evidence=evidence)
                self.save(s)

    def prepare_time2(self, s):
        self.check_time2_context(s)
        self.charge.soc_guard(self.remaining(s), time2_end_override=s['target']['150'])
        current = self.registers(s)
        if any(current[str(a)] != v for a, v in self.BASE.items()):
            raise ValueError('Udgangspunktet er ændret før TOU-forlængelsen.')
        if self.charge.measure()['charging_w'] > 5500:
            raise ValueError('Målt ladeeffekt over 5500 W før TOU-forlængelsen.')
        if current['150'] not in (s['baseline']['150'], s['target']['150']):
            raise ValueError('Time 2-sluttiden er ændret uden for testen.')
        if 150 not in s['written']:
            if current['150'] != s['baseline']['150']:
                raise ValueError('Time 2 er ændret før skrivning.')
            s['written'].append(150)
            self.log(s, 'time2_extend_write', address=150, value=s['target']['150'], original=s['baseline']['150'])
            s.update(prepare_deadline=time.time()+25)
            self.save(s)
            self.charge.write(150, s['target']['150'])
            return
        self.log(s, 'time2_extend_readback', value=current['150'])
        if current['150'] == s['target']['150']:
            if s.get('confirm_started') is None:
                s.update(confirm_started=time.time(), confirm_checks=0)
            s['confirm_checks'] += 1
            if time.time()-s['confirm_started'] >= 10 and s['confirm_checks'] >= 3:
                s.update(prepare_index=0, prepare_deadline=time.time()+25, confirm_started=None, confirm_checks=0)
        else:
            s.update(confirm_started=None, confirm_checks=0)
        if s['prepare_index'] == -1 and time.time() >= s['prepare_deadline']:
            raise ValueError('Forlænget Time 2 ikke stabilt bekræftet inden 25 sekunder.')
        self.save(s)

    def tick(self):
        with self.charge.lock:
            s=self.state()
            if s.get('status')=='preparing':
                try:
                    if s['endpoint'] != self.charge.endpoint():
                        raise ValueError('Forbindelsen er ændret.')
                    cfg=self.charge.options()
                    if cfg.get('read_only',True) or not cfg.get('commissioning_enabled',False):
                        raise ValueError('Skrivning slået fra under klargøring.')
                    if self.remaining(s)<=0:
                        raise ValueError('Testens samlede tidsgrænse er nået under klargøring.')
                    if s['prepare_index'] == -1:
                        self.prepare_time2(s)
                        return
                    self.guarded_time(s)
                    if not 90 <= self.charge.value(212) <= 500:
                        raise ValueError('BMS-tilladelsen er under 90 A under klargøring.')
                    m=self.charge.measure()
                    if m['charging_w']>5500:
                        raise ValueError('Målt ladeeffekt over 5500 W under klargøring.')
                    current=self.registers(s)
                    keys=list(self.TARGET)
                    index=s['prepare_index']
                    address=keys[index]
                    # Only baseline or the journalled test value is acceptable.
                    if any(current[str(a)] not in (self.BASE[a],self.TARGET[a]) or
                           (a not in s['written'] and current[str(a)]!=self.BASE[a])
                           for a in self.BASE):
                        raise ValueError('Uventet registerværdi under klargøring.')
                    if address not in s['written']:
                        s['written'].append(address)
                        self.log(s,'write_requested',address=address,value=self.TARGET[address])
                        s.update(prepare_deadline=time.time()+25,confirm_started=None,confirm_checks=0)
                        self.save(s)  # Intent is durable before the sole write attempt.
                        self.charge.write(address,self.TARGET[address])
                        return
                    expected={str(a):(self.TARGET[a] if a in s['written'] else self.BASE[a]) for a in self.BASE}
                    if s.get('time2_extension'):
                        expected['150'] = s['target']['150']
                    self.log(s,'prepare_readback',values=current)
                    if current==expected:
                        if s.get('confirm_started') is None:
                            s.update(confirm_started=time.time(),confirm_checks=0)
                        s['confirm_checks']+=1
                        if time.time()-s['confirm_started']>=10 and s['confirm_checks']>=3:
                            if index==len(keys)-1:
                                s.update(status='active',active_started=time.time())
                            else:
                                s.update(prepare_index=index+1,confirm_started=None,confirm_checks=0)
                    else:
                        s.update(confirm_started=None,confirm_checks=0)
                    if s['status']=='preparing' and time.time()>=s['prepare_deadline']:
                        raise ValueError('Stabil testværdi ikke bekræftet inden 25 sekunder ved '+str(address))
                    self.save(s)
                except Exception as exc:
                    s['guard_snapshot']=getattr(self.charge,'guard_snapshot',None)
                    self.save(s)
                    self.restore('prepare_error: '+str(exc))
            elif s.get('status')=='active':
                try:
                    if self.remaining(s)<=0:
                        self.restore('expired')
                        return
                    c=self.charge.options()
                    if c.get('read_only',True) or not c.get('commissioning_enabled',False):
                        raise ValueError('Skrivning slået fra under testen.')
                    self.guarded_time(s)
                    if self.registers(s) != s.get('target', {str(a):v for a,v in self.TARGET.items()}):
                        raise ValueError('Testindstillinger er ændret uden for testen.')
                    m=self.charge.measure()
                    s['samples'].append(m)
                    self.save(s)
                    if m['charging_w']>5500:
                        raise ValueError('Målt ladeeffekt over testens stopgrænse på 5500 W.')
                except Exception as exc:
                    s['guard_snapshot'] = getattr(self.charge, 'guard_snapshot', None)
                    self.save(s)
                    self.restore('guard_failed: '+str(exc))
            elif s.get('status') in ('settling_restore', 'verifying_restore', 'restore_failed'):
                try:
                    if s['endpoint'] != self.charge.endpoint():
                        raise ValueError('Forbindelsen er ændret.')
                    current=self.registers(s)
                    s.update(current_registers=current, checked_at=time.time())
                    self.log(s,'restore_check',values=current)
                    # All checks in this phase are read-only; never repeat a write.
                    if s['status']=='restore_failed':
                        if current==s['baseline']:
                            s.update(status='verifying_restore',manual_recovery=True,
                                     restore_started=time.time(),restore_checks=0,
                                     error='Udgangspunktet ses igen; efterkontrol kører. Den oprindelige testfejl bevares i rapporten.')
                    elif s['status']=='settling_restore':
                        if current==s['baseline']:
                            s.update(status='verifying_restore',restore_started=time.time(),restore_checks=0)
                            self.log(s,'baseline_first_observed',values=current)
                        elif any(current[a] not in (s['baseline'][a],s.get('target', {'108':20,'128':20,'167':30})[a]) for a in current):
                            s.update(status='restore_failed',error='En anden registerværdi er set. Gendan manuelt; ingen automatisk overskrivning.')
                        elif time.time()>=s['settle_deadline']:
                            s.update(status='restore_failed',error='Udgangspunktet er ikke bekræftet inden 10 sekunder. Gendan manuelt; ingen nye skrivninger.')
                    elif current!=s['baseline']:
                        s.update(status='restore_failed',error='Værdien ændrede sig under efterkontrollen. Gendan manuelt; ingen nye skrivninger.')
                    else:
                        s['restore_checks']+=1
                        s['after']=self.charge.measure()
                        if time.time()-s['restore_started']>=60 and s['restore_checks']>=3:
                            passed=s['stop_reason'] in ('expired','manual_stop') and not s.get('manual_recovery')
                            s.update(status='complete' if passed else 'failed',finished=time.time(),
                                     error=None if passed else 'Testen blev afbrudt, men udgangspunktet er nu bekræftet i mindst 60 sekunder.')
                except Exception as exc:
                    s.update(restore_started=time.time(),restore_checks=0,error='Efterkontrol fejlede: '+str(exc))
                    if s['status']=='settling_restore' and time.time()>=s['settle_deadline']:
                        s['status']='restore_failed'
                self.save(s)

    def view(self):
        with self.charge.lock:
            s=self.state()
            status=s.get('status')
            labels={'preparing':'KLARGØRER – BEKRÆFTER STABILE TESTVÆRDIER','idle':'Klar til 90 A-test','active':'90 A-TEST KØRER – AFLÆS BEGGE BMS NU',
                    'restoring':'STOPPER – GENDANNER INDSTILLINGER','settling_restore':'AFVENTER TILBAGELÆSNING – HØJST 10 SEKUNDER','verifying_restore':'EFTERKONTROL KØRER',
                    'restore_failed':'GENDANNELSE FEJLEDE – KONTROLLÉR MANUELT','failed':'TEST AFBRUDT','complete':'90 A-TEST AFSLUTTET'}
            if status=='preparing':
                progress=max(0,min(30,s.get('prepare_index',0)*10))
            elif status=='active':
                progress=min(65,int(max(0,time.time()-s['started'])/s.get('duration_seconds',120)*65))
            elif status=='settling_restore':
                progress=65
            elif status=='verifying_restore':
                progress=min(99,65+int(max(0,time.time()-s['restore_started'])/60*35))
            else:
                progress=100 if status=='complete' else 0
            return dict(status=status, headline=labels.get(status,status).replace('90 A',str(s.get('test_current_a',90 if status=='idle' else 20))+' A'),progress_percent=progress,
                        instruction='Under opladning: tag et billede af hver BMS med strøm, spænding og temperatur. Bridge kan kun se den tilkoblede BMS. Efter testen: hent rapporten, slå skrivning fra og genstart. Ved fejl kontrolleres 108=10, 128=10 og 167=15 manuelt.',
                        duration_seconds=s.get('duration_seconds',120), time2_extension=s.get('time2_extension'),
                        remaining_seconds=max(0,int(self.remaining(s))) if status in ('preparing','active') else None,
                        stop_reason=s.get('stop_reason'), error=s.get('error'),restore_errors=s.get('restore_errors'),
                        latest=s.get('after') or (s.get('samples') or [s.get('before')])[-1],
                        current_registers=s.get('current_registers'))

    def report(self):
        with self.charge.lock:
            s={k:v for k,v in self.state().items() if k!='endpoint'}
            s['note']=str(s.get('test_current_a',20))+' A er en indstillet samlet øvre grænse, ikke en garanteret faktisk ladestrøm. Begge pakkers strøm skal aflæses manuelt; Bridge ser kun den kommunikerende BMS. Ingen BMS-grænser ændres. Effektstop er softwarebaseret og virker ikke ved Pi-/netværksfejl. Godkender ikke højere strøm eller fuld automatisk drift.'
            return s
