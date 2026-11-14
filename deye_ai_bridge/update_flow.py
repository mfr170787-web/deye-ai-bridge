"""Locally approved, journalled update transaction. Never resumes after interruption."""
import json
import threading
import time
import urllib.request
from maintenance import atomic_json, digest

TERMINAL = {'complete','failed','interrupted'}

class UpdateFlow:
    def __init__(self, local, request=None, clock=time.monotonic, wait=time.sleep):
        self.local=local; self.manager=local.manager; self.lock=threading.RLock()
        self.path=self.manager.private/'update-flow.json';self.clock=clock;self.wait=wait
        self.request=request or self._request
        self.value=json.loads(self.path.read_text()) if self.path.exists() else {}
        if self.value and self.value.get('phase') not in TERMINAL:
            self.record('interrupted',error='Opdateringsforløbet blev afbrudt. Ingen automatisk genoptagelse; kontrollér driften manuelt.')

    @staticmethod
    def _request(action=None):
        data=None if action is None else json.dumps({'action':action,'cloud_off':True}).encode()
        req=urllib.request.Request('http://127.0.0.1:8098/api/automation',data=data,
            headers={'Content-Type':'application/json','X-Deye-Control':'1'})
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req,timeout=90 if action else 5) as response:return json.load(response)

    def record(self,phase,**extra):
        with self.lock:
            self.value.update(phase=phase,**extra)
            atomic_json(self.path,self.value)

    def status(self):
        with self.lock:return dict(self.value)

    def cancel_resume(self):
        with self.lock:
            if self.value and self.value.get('phase') not in TERMINAL:
                self.record(self.value['phase'],resume_cancelled=True)

    @staticmethod
    def clean(state,active=False):
        return (state.get('status')==('active' if active else 'disabled')
            and (active or state.get('restored') is True)
            and not state.get('error') and state.get('pending') is None
            and not state.get('auto_resume') and not state.get('tesla_only_active')
            and not state.get('battery_timer_mode'))

    def start(self,body,nonce):
        if type(body) is not dict or set(body)!={'package','review_ticket','acknowledge','cloud_off'} or body['acknowledge'] is not True or body['cloud_off'] is not True:
            raise ValueError('Godkend den viste pakke, pause og mulig genoptagelse samt frakoblet cloudstyring.')
        with self.manager.lock,self.lock:
            self.local.authenticate_page(nonce)
            if self.value and self.value.get('phase') not in TERMINAL:raise ValueError('Et opdateringsforløb er allerede registreret')
            ticket=self.local.tickets.pop(body['review_ticket'],None)
            sha=digest(body['package'])
            if not ticket or ticket['page']!=nonce or ticket['hash']!=sha or ticket['base']!=self.manager.revision():
                raise PermissionError('Gennemgangen er udløbet eller ændret')
            self.manager.preview(body['package'])
            state=self.request()
            active=self.clean(state,True)
            if not active and not self.clean(state):raise ValueError('Uafklaret drift, fejl eller timer kræver manuel kontrol før opdatering')
            self.value={'package_sha256':sha,'was_active':active,'resume_cancelled':False}
            self.record('accepted')
            threading.Thread(target=self.run,args=(body['package'],),daemon=True).start()
            return self.status()

    def until(self,fn,seconds):
        deadline=self.clock()+seconds
        while self.clock()<deadline:
            result=fn()
            if result:return result
            self.wait(1)
        raise TimeoutError('Kontrollen blev ikke bekræftet inden tidsfristen')

    def run(self,package):
        resumed=False
        try:
            if self.value['was_active']:
                self.record('pausing')
                # Exactly one request. A lost response is NOT automatically resent.
                self.request('disable')
            self.record('verifying_restore')
            self.until(lambda:self.clean(self.request()),150)
            if self.manager.revision()!=package['base_revision']:raise ValueError('Kodegrundlaget ændrede sig under pausen')
            self.record('installing')
            with self.manager.lock:
                self.manager.grant(package,self.manager.secret)
                self.manager.apply(package)
            self.record('checking_startup')
            def installed():
                s=self.manager.status();op=s.get('operation') or {}
                if op.get('package_sha256')!=self.value['package_sha256']:return False
                if 'rolled_back' in op.get('status',''):raise ValueError('Opstart fejlede; tidligere kode er valgt')
                return op.get('status')=='installed' and s.get('active_release')==op.get('target')
            self.until(installed,100)
            if not self.clean(self.request()):raise ValueError('Driftsstatus er ikke sikkert deaktiveret efter installation')
            if self.value['was_active'] and not self.value['resume_cancelled']:
                self.record('checking_measurements')
                first_sample=(self.request().get('live_sample') or {}).get('created')
                def fresh():
                    s=self.request();age=s.get('live_sample_age_seconds')
                    if not self.clean(s):raise ValueError('Driften ændrede sig under efterkontrollen')
                    return ((s.get('live_sample') or {}).get('created') not in (None,first_sample)
                        and type(age) in (int,float) and 0<=age<=60 and not s.get('collector',{}).get('error'))
                self.until(fresh,100)
                # Serialize the final resume decision with local manual stop.
                with self.lock:
                    if not self.value['resume_cancelled']:
                        self.record('resuming');resumed=True
                        self.request('enable')
                if resumed:
                    self.record('verifying_operation')
                    deadline=self.clock()+125
                    samples=set()
                    while self.clock()<deadline:
                        s=self.request()
                        if not self.clean(s,True):raise ValueError('Automatisk drift blev ikke stabilt bekræftet')
                        age=s.get('live_sample_age_seconds')
                        if type(age) not in (int,float) or not 0<=age<=120:raise ValueError('Aktuelle målinger mangler')
                        samples.add((s.get('live_sample') or {}).get('created'))
                        self.wait(2)
                    if len(samples-{None})<2:raise ValueError('Nye målinger blev ikke bekræftet')
            self.record('complete',resumed=resumed and not self.value['resume_cancelled'])
        except Exception as exc:
            stop_error=None
            if resumed:
                try:self.request('disable')
                except Exception as stop:stop_error=type(stop).__name__
                try:
                    self.until(lambda:self.clean(self.request()),150)
                    stop_error=None
                except Exception as stop:stop_error=type(stop).__name__
            self.record('failed',error=str(exc),stop_unconfirmed=stop_error,
                message='Ingen yderligere genoptagelse. Kontrollér driftsstatus og gendannelse manuelt.')
