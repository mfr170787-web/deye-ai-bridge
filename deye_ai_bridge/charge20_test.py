"""Fixed supervised 20 A test; restores all three settings, never overrides BMS."""
import json
import os
import time


class Charge20Test:
    BASE = {108: 10, 128: 10, 167: 15}
    TARGET = {108: 20, 128: 20, 167: 30}

    def __init__(self, path, charge):
        self.path, self.charge = path, charge

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

    def registers(self):
        return {str(a): self.charge.value(a) for a in self.BASE}

    def start(self, body):
        with self.charge.lock:
            c = self.charge.options()
            if getattr(self, 'other_busy', lambda: False)() or self.busy() or self.charge.state().get('status') not in ('idle', 'restored'):
                raise ValueError('Afslut den eksisterende test og efterkontrol først.')
            if c.get('read_only', True) or not c.get('commissioning_enabled', False):
                raise ValueError('Kræver read_only: false og commissioning_enabled: true.')
            if any(body.get(k) is not True for k in ('copilot_off', 'supervised', 'both_packs_ready')):
                raise ValueError('Bekræft Copilot slået fra, overvågning og adgang til begge BMS-appvisninger.')
            self.charge.soc_guard(120)
            if self.registers() != {str(a):v for a,v in self.BASE.items()}:
                raise ValueError('Start kræver 108=10 A, 128=10 A og 167=15 %.')
            if not 20 <= self.charge.value(212) <= 500:
                raise ValueError('Den tilkoblede BMS tillader ikke mindst 20 A.')
            s = dict(status='active', started=time.time(), expires=time.time()+120,
                     endpoint=self.charge.endpoint(), baseline=self.registers(),
                     written=[], samples=[], events=[], stop_reason=None,
                     before=self.charge.measure())
            self.save(s)
            try:
                for a,v in self.TARGET.items():
                    # Record possible mutation BEFORE transport, including ambiguous timeouts.
                    s['written'].append(a)
                    self.log(s, 'write_requested', address=a, value=v)
                    self.save(s)
                    self.charge.write(a,v)
                    actual=self.charge.value(a)
                    self.log(s, 'write_readback', address=a, value=actual)
                    self.save(s)
                    if actual != v:
                        raise ValueError('Testværdi kunne ikke bekræftes ved register '+str(a))
                s['expires']=time.time()+120
                self.save(s)
            except Exception as exc:
                self.restore('start_error: '+str(exc))
                raise
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
            if s['endpoint'] != self.charge.endpoint():
                errors.append('Forbindelsen er ændret; ingen skrivning.')
            else:
                # Lower the SOC demand first, then the grid and global limits.
                for a in (167,128,108):
                    if a not in s['written']:
                        continue
                    try:
                        actual=self.charge.value(a)
                        if actual not in (self.BASE[a],self.TARGET[a]):
                            raise ValueError('Anden værdi ved '+str(a)+'; ingen overskrivning.')
                        if actual != self.BASE[a]:
                            self.log(s,'restore_write',address=a,value=self.BASE[a])
                            self.save(s)
                            self.charge.write(a,self.BASE[a])
                        actual=self.charge.value(a)
                        self.log(s,'restore_readback',address=a,value=actual)
                        if actual not in (self.BASE[a], self.TARGET[a]):
                            raise ValueError('Uventet værdi efter gendannelse ved '+str(a))
                        if actual != self.BASE[a]:
                            self.log(s, 'restore_pending', address=a, value=actual)
                    except Exception as exc:
                        errors.append(str(exc))
            s['restore_errors']=errors
            s.update(status='restore_failed' if errors else 'settling_restore',
                     settle_deadline=time.time()+10, restore_started=time.time(), restore_checks=0)
            self.save(s)
            return s

    def tick(self):
        with self.charge.lock:
            s=self.state()
            if s.get('status')=='active':
                try:
                    if time.time() >= s['expires']:
                        self.restore('expired')
                        return
                    c=self.charge.options()
                    if c.get('read_only',True) or not c.get('commissioning_enabled',False):
                        raise ValueError('Skrivning slået fra under testen.')
                    self.charge.soc_guard(max(0,s['expires']-time.time()), ceiling=20)
                    if self.registers() != {str(a):v for a,v in self.TARGET.items()}:
                        raise ValueError('Testindstillinger er ændret uden for testen.')
                    m=self.charge.measure()
                    s['samples'].append(m)
                    self.save(s)
                    if m['charging_w']>1500:
                        raise ValueError('Målt ladeeffekt over testens stopgrænse på 1500 W.')
                except Exception as exc:
                    self.restore('guard_failed: '+str(exc))
            elif s.get('status') in ('settling_restore', 'verifying_restore', 'restore_failed'):
                try:
                    if s['endpoint'] != self.charge.endpoint():
                        raise ValueError('Forbindelsen er ændret.')
                    current=self.registers()
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
                        elif any(current[str(a)] not in (self.BASE[a],self.TARGET[a]) for a in self.BASE):
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
            labels={'idle':'Klar til 20 A-test','active':'20 A-TEST KØRER – AFLÆS BEGGE BMS NU',
                    'restoring':'STOPPER – GENDANNER INDSTILLINGER','settling_restore':'AFVENTER TILBAGELÆSNING – HØJST 10 SEKUNDER','verifying_restore':'EFTERKONTROL KØRER',
                    'restore_failed':'GENDANNELSE FEJLEDE – KONTROLLÉR MANUELT','failed':'TEST AFBRUDT','complete':'20 A-TEST AFSLUTTET'}
            if status=='active':
                progress=min(65,int(max(0,time.time()-s['started'])/120*65))
            elif status=='settling_restore':
                progress=65
            elif status=='verifying_restore':
                progress=min(99,65+int(max(0,time.time()-s['restore_started'])/60*35))
            else:
                progress=100 if status=='complete' else 0
            return dict(status=status, headline=labels.get(status,status),progress_percent=progress,
                        instruction='Under opladning: tag et billede af hver BMS med strøm, spænding og temperatur. Bridge kan kun se den tilkoblede BMS. Efter testen: hent rapporten, slå skrivning fra og genstart. Ved fejl kontrolleres 108=10, 128=10 og 167=15 manuelt.',
                        stop_reason=s.get('stop_reason'), error=s.get('error'),restore_errors=s.get('restore_errors'),
                        latest=s.get('after') or (s.get('samples') or [s.get('before')])[-1],
                        current_registers=s.get('current_registers'))

    def report(self):
        with self.charge.lock:
            s={k:v for k,v in self.state().items() if k!='endpoint'}
            s['note']='20 A er en indstillet samlet øvre grænse, ikke en garanteret faktisk ladestrøm. Begge pakkers strøm skal aflæses manuelt; Bridge ser kun den kommunikerende BMS. Ingen BMS-grænser ændres. Effektstop er softwarebaseret og virker ikke ved Pi-/netværksfejl. Godkender ikke højere strøm eller fuld automatisk drift.'
            return s
