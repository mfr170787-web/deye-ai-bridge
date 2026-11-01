"""User-started, supervised commissioning sequence; no unattended scheduler."""
import json
import os
import time
import uuid


class TestSuite:
    STEPS = [('charge_limit', 5, 30), ('grid_limit', 5, 30),
             ('soc_time2', 0, 60), ('soc_time2', 0, 120), ('soc_time2', 0, 120)]
    NAMES = ['Ladegrænse 5 A og gendannelse', 'Netladegrænse 5 A og gendannelse',
             'SOC-test med automatisk udløb', 'SOC-test med stop efter 30 sekunder',
             'SOC-test med manuel genstart af tilføjelsen']

    def __init__(self, path, charge):
        self.path, self.charge = path, charge
        self.boot = uuid.uuid4().hex

    def state(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {'status': 'idle'}

    def save(self, s):
        temp = self.path.with_suffix('.tmp')
        with temp.open('w') as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        temp.replace(self.path)

    def busy(self):
        return self.state().get('status') in ('running', 'waiting_restart', 'restart_active', 'stopping')

    def start(self, body):
        with self.charge.lock:
            if self.busy() or getattr(self, 'other_busy', lambda: False)():
                raise ValueError('Et samlet testforløb er allerede i gang.')
            c = self.charge.options()
            if c.get('read_only', True) or not c.get('commissioning_enabled', False):
                raise ValueError('Aktivér commissioning_enabled og slå read_only fra før start.')
            if body.get('supervised') is not True or body.get('copilot_off') is not True:
                raise ValueError('Bekræft overvågning og Copilot slået fra.')
            if self.charge.state().get('status') not in ('idle', 'restored'):
                raise ValueError('Den tidligere test skal være afsluttet og efterkontrolleret.')
            self.charge.soc_guard(120)
            baseline = {str(a): self.charge.value(a) for a in (108, 128, 167)}
            if baseline != {'108': 10, '128': 10, '167': 15}:
                raise ValueError('Start kræver præcis 108=10 A, 128=10 A og 167=15 %.')
            s = dict(status='running', started=time.time(), deadline=time.time()+1800,
                     boot=self.boot, baseline=baseline, index=0, results=[],
                     instruction='Automatiske test kører. Bliv ved inverter og batterier. Stop ved behov.')
            self.save(s)
            try:
                self.launch(s)
            except Exception as exc:
                self.fail(s, str(exc))
                raise
            return self.view()

    def launch(self, s):
        i = s['index']
        kind, amps, seconds = self.STEPS[i]
        # Persist intent before calling the controller, which persists its own backup.
        s.update(status='restart_active' if i == 4 else 'running', boot=self.boot,
                 step_started=time.time(), samples=[], next_sample=0)
        self.save(s)
        self.charge.start(dict(kind=kind, amps=amps, seconds=seconds,
                              copilot_off=True, supervised=True))
        s['test_started'] = self.charge.state()['started']
        if i == 4:
            s['instruction'] = 'GENSTART NU kun Deye AI Bridge-tilføjelsen via Home Assistant inden 120 sekunder. Genstart ikke inverteren eller hele Home Assistant. Åbn derefter Bridge igen.'
        self.save(s)

    def begin_restart(self):
        with self.charge.lock:
            s = self.state()
            if s.get('status') != 'waiting_restart':
                raise ValueError('Forløbet venter ikke på genstartstesten.')
            if time.time() > s['deadline']:
                self.fail(s, 'Forløbets tidsfrist er udløbet.')
                return self.view()
            self.charge.soc_guard(120)
            self.launch(s)
            return self.view()

    def fail(self, s, message):
        s['failure_test'] = {k:v for k,v in self.charge.state().items() if k != 'endpoint'}
        s.update(status='stopping', error=message,
                 instruction='Forløbet er afbrudt. Gendannelse og efterkontrol forsøges. Kontrollér inverteren ved fejl.')
        self.save(s)
        self.charge.restore("suite_abort", message)

    def stop(self):
        with self.charge.lock:
            s = self.state()
            if self.busy():
                self.fail(s, 'Stoppet af brugeren.')
            return self.view()

    def tick(self):
        with self.charge.lock:
            s = self.state()
            if not self.busy():
                return
            c = self.charge.state()
            if s['status'] == 'stopping':
                if c.get('status') in ('idle', 'restored', 'restore_failed', 'restore_mismatch'):
                    s.update(status='failed', finished=time.time(),
                             instruction='Forløbet er stoppet. Hent rapport. Kontrollér 108=10, 128=10 og 167=15. Slå derefter skrivning fra og genstart tilføjelsen.')
                    self.save(s)
                return
            opts = self.charge.options()
            if opts.get('read_only', True) or not opts.get('commissioning_enabled', False):
                self.fail(s, 'Skrivning er slået fra under forløbet.')
                return
            if time.time() > s['deadline']:
                self.fail(s, 'Forløbets tidsfrist er udløbet.')
                return
            if s['status'] == 'waiting_restart':
                return
            if s['boot'] != self.boot:
                if s['index'] != 4 or c.get('status') not in ('verifying_restore', 'restored'):
                    self.fail(s, 'Uventet genstart. Automatisk fortsættelse er afbrudt.')
                    return
                s.update(boot=self.boot, restart_observed=True,
                         instruction='Genstart registreret. Gendannelse efterkontrolleres automatisk.')
            if c.get('started') != s.get('test_started'):
                self.fail(s, 'Teststatus er ændret uden for forløbet.')
                return
            if c.get('restore_reason') == 'guard_failed':
                self.fail(s, 'Beskyttelseskontrol afbrød testen: '+str(c.get('restore_detail')))
                return
            if c.get('status') in ('restore_failed', 'restore_mismatch'):
                self.fail(s, 'Gendannelsen fejlede eller registeret ændrede sig igen.')
                return
            if time.time() >= s.get('next_sample', 0):
                s['samples'].append(self.charge.measure())
                s['next_sample'] = time.time()+5
            if s['index'] == 3 and c.get('status') == 'active' and time.time()-s['step_started'] >= 30:
                self.charge.restore("planned_stop", "Planlagt stop efter 30 sekunder.")
            if c.get('status') == 'restored':
                clean = {k:v for k,v in c.items() if k != 'endpoint'}
                s['results'].append(dict(name=self.NAMES[s['index']], controller=clean,
                                         measurements=s['samples'], restart_observed=s.get('restart_observed', False)))
                if s['index'] == 4:
                    if not s.get('restart_observed'):
                        self.fail(s, 'Ingen genstart registreret inden testens udløb. Genstartstesten er ikke bestået.')
                        return
                    final = {str(a):self.charge.value(a) for a in (108,128,167)}
                    s['final_registers'] = final
                    if final != s['baseline']:
                        self.fail(s, 'Slutindstillinger matcher ikke udgangspunktet.')
                        return
                    s.update(status='complete', finished=time.time(),
                             instruction='Testforløb afsluttet. Hent rapport. Sæt read_only: true og commissioning_enabled: false, gem og genstart tilføjelsen. Ladegrænser er tilbage på 10 A og Time 2 på 15 %.')
                else:
                    s['index'] += 1
                    if s['index'] == 4:
                        s.update(status='waiting_restart',
                                 instruction='De automatiske trin er afsluttet. Når du er klar: Tryk Klargør genstartstest. Genstart derefter kun tilføjelsen inden 120 sekunder. Ingen test er aktiv i denne pause.')
                    else:
                        self.save(s)
                        self.launch(s)
                        return
            self.save(s)

    def view(self):
        with self.charge.lock:
            s = self.state()
            c = self.charge.view()
            completed = len(s.get('results', []))
            fraction = 0
            if s.get('status') in ('running', 'restart_active'):
                duration = 30 if s.get('index') in (0, 1, 3) else 60 if s.get('index') == 2 else 120
                if c.get('status') == 'active':
                    fraction = .5 * min(1, max(0, time.time()-s.get('step_started', time.time()))/duration)
                elif c.get('status') == 'verifying_restore':
                    fraction = .5 + .5 * min(1, max(0, time.time()-c.get('restore_started', time.time()))/60)
            progress = 100 if s.get('status') == 'complete' else min(99, int((completed+fraction)/5*100))
            return dict(status=s.get('status'), instruction=s.get('instruction', 'Klar til ét samlet overvåget testforløb.'),
                        step=self.NAMES[s['index']] if 'index' in s else None,
                        completed=completed, progress_percent=progress, total_steps=5,
                        phase='Efterkontrol' if c.get('status') == 'verifying_restore' else 'Test aktiv' if c.get('status') == 'active' else 'Afventer',
                        error=s.get('error'), charge_test=c)

    def report(self):
        s = self.state()
        s['report_note'] = 'Registertest ved højst 10 A. Målinger dokumenterer observeret effekt; rapporten godkender ikke høj strøm, fasebeskyttelse eller fuld automatisk styring. En genstart er kun prøvet hvis restart_observed er sand.'
        s['latest_test'] = {k:v for k,v in self.charge.state().items() if k != 'endpoint'}
        return s

    def worker(self, extra=None):
        if self.charge.state().get('status') not in ('idle', 'restored', 'verifying_restore', 'restore_mismatch'):
            self.charge.restore("process_restart", "Uafsluttet test fundet ved opstart.")
        if extra is not None and extra.state().get('status') in ('active', 'restoring'):
            extra.restore('process_restart')
        while True:
            time.sleep(2)
            try:
                self.charge.tick()
                self.tick()
                if extra is not None:
                    extra.tick()
            except Exception as exc:
                with self.charge.lock:
                    if self.busy():
                        self.fail(self.state(), 'Kontrolfejl: '+str(exc))
