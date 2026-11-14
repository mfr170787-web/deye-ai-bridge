"""Local browser consent for exact packages; independent of the Bridge child.

The browser receives a short-lived page nonce, never the approval key. Preview
creates a one-use ticket bound to the exact package hash and current revision.
MCP can stage/inspect packages but has no tool for obtaining local review tickets.
"""
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from maintenance import canonical, digest, atomic_json, MAX_BYTES
from image_recovery import GROUPS, ADDRESSES, STALE_FAULT, read_registers

PREFIX = '/api/private/updates'


class LocalUpdates:
    def __init__(self, manager, clock=time.monotonic):
        self.manager = manager
        self.clock = clock
        self.pages = {}
        self.tickets = {}
        from update_flow import UpdateFlow
        self.flow = UpdateFlow(self)

    def _expire(self):
        now=self.clock()
        self.pages={k:v for k,v in self.pages.items() if v>now}
        self.tickets={k:v for k,v in self.tickets.items() if v['expires']>now}

    def page(self, template):
        with self.manager.lock:
            self._expire()
            if len(self.pages)>=32:self.pages.pop(next(iter(self.pages)))
            nonce=secrets.token_urlsafe(32)
            self.pages[nonce]=self.clock()+1800
            return template.replace('__LOCAL_PAGE_NONCE__',nonce).encode()

    def authenticate_page(self, nonce):
        self._expire()
        if not isinstance(nonce,str) or nonce not in self.pages:
            raise PermissionError('Den lokale side er udløbet. Genindlæs siden og gennemgå forslaget igen.')

    def blockers(self):
        try:
            state=json.loads((self.manager.data/'automatic_control.json').read_text())
        except Exception:
            return ['Automatikstatus kan ikke læses. Ingen installation tilladt.']
        result=[]
        if state.get('status')!='disabled':result.append('Automatikken er ikke deaktiveret: '+str(state.get('status')))
        if state.get('restored') is not True:result.append('Gendannelsen er ikke bekræftet.')
        if state.get('error'):result.append('Registreret fejl: '+str(state['error']))
        if state.get('pending') is not None:result.append('En inverterkommando afventer afklaring.')
        if state.get('auto_resume') or state.get('tesla_only_active'):result.append('Automatisk genoptagelse eller bilstyring er aktiv.')
        return result

    def status(self):
        result=self.manager.status()
        result['approval_mode']='local_click'
        result['update_flow']=self.flow.status()
        result['installation_blockers']=self.blockers()
        try:
            raw=(self.manager.data/'automatic_control.json').read_bytes()
            state=json.loads(raw)
            result['fault_acknowledgement']={
                'available':state.get('status')=='fault' and state.get('restored') is True and state.get('error')==STALE_FAULT,
                'state_sha256':hashlib.sha256(raw).hexdigest()}
        except Exception:result['fault_acknowledgement']={'available':False}
        return result

    def _directory(self):
        path=self.manager.data/'bridge_update_proposals'
        if path.is_symlink():raise ValueError('Ugyldigt venteområde')
        return path

    def proposal(self, proposal_id):
        if not isinstance(proposal_id,str) or len(proposal_id)!=64 or any(c not in '0123456789abcdef' for c in proposal_id):
            raise ValueError('Ugyldigt forslags-id')
        path=self._directory()/(proposal_id+'.json')
        if path.is_symlink():raise ValueError('Ugyldig forslagspakke')
        with path.open('rb') as stream:raw=stream.read(MAX_BYTES+8001)
        if len(raw)>MAX_BYTES+8000:raise ValueError('Forslaget er for stort')
        item=json.loads(raw)
        if type(item) is not dict or set(item)!={'package','summary','submitted_at'}:
            raise ValueError('Ugyldigt forslag')
        if digest(item['package'])!=proposal_id:raise ValueError('Forslaget matcher ikke kontrolsummen')
        if not isinstance(item['summary'],str) or len(item['summary'].encode())>2000:raise ValueError('Ugyldig beskrivelse')
        if type(item['submitted_at']) not in (int,float) or not 0<=time.time()-item['submitted_at']<=86400:
            raise ValueError('Forslaget er udløbet eller har ugyldigt tidspunkt')
        return item

    def listing(self):
        result=[];invalid=0;revision=self.manager.revision()
        for path in self._directory().glob('*.json'):
            try:
                item=self.proposal(path.stem);p=item['package']
                self.manager.validate(p)
                result.append(dict(proposal_id=path.stem,summary=item['summary'],files=list(p['files']),
                    action=p['action'],submitted_at=item['submitted_at'],restart=True,rollback_on_failure=True,
                    status='awaiting_approval' if p['base_revision']==revision else 'stale'))
            except Exception:invalid+=1
        return dict(proposals=sorted(result,key=lambda i:i['submitted_at'],reverse=True),invalid_entries=invalid,max_pending=5)

    def submit(self, body):
        if type(body) is not dict or set(body)!={'package','summary'}:raise ValueError('Forslag kræver pakke og beskrivelse')
        if not isinstance(body['summary'],str) or not body['summary'].strip() or len(body['summary'].encode())>2000:
            raise ValueError('Beskrivelsen mangler eller er for lang')
        with self.manager.lock:
            preview=self.manager.preview(body['package']);proposal_id=preview['package_sha256']
            directory=self._directory();directory.mkdir(mode=0o700,exist_ok=True)
            for path in directory.glob('*.json'):
                try:self.proposal(path.stem)
                except ValueError:
                    # Clean expired well-formed entries only; do not follow links.
                    if not path.is_symlink():
                        try:
                            item=json.loads(path.read_text())
                            if type(item.get('submitted_at')) in (int,float) and time.time()-item['submitted_at']>86400:path.unlink()
                        except Exception:pass
            target=directory/(proposal_id+'.json')
            if not target.exists():
                if len(list(directory.glob('*.json')))>=5:raise ValueError('Venteområdet indeholder allerede fem forslag')
                atomic_json(target,dict(package=body['package'],summary=body['summary'],submitted_at=time.time()))
            else:self.proposal(proposal_id)
            return dict(status='awaiting_local_approval',proposal_id=proposal_id,files=list(body['package']['files']),
                        installed=False,restart_performed=False)

    def preview(self, body, nonce):
        if type(body) is not dict or set(body)!={'package'}:raise ValueError('Ugyldigt gennemgangsskema')
        with self.manager.lock:
            self.authenticate_page(nonce)
            result=self.manager.preview(body['package'])
            if len(self.tickets)>=32:raise ValueError('For mange åbne gennemgange; genindlæs senere')
            ticket=secrets.token_urlsafe(32)
            self.tickets[ticket]={'expires':self.clock()+900,'page':nonce,
                'hash':result['package_sha256'],'base':result['base_revision']}
            return {**result,'review_ticket':ticket,'consequence':'Dit lokale klik godkender pause og bekræftet gendannelse, backup, installation og Bridge-genstart. Kun tidligere aktiv drift genoptages efter kontrol af pakke og nye målinger. Fejl eller afbrydelse kræver manuel kontrol. Ingen Raspberry Pi- eller Home Assistant-genstart.'}

    def install(self, body, nonce):
        flow=self.flow.status()
        if flow and flow.get("phase") not in ("complete","failed","interrupted"):
            raise ValueError("Et opdateringsforløb er allerede i gang")
        if type(body) is not dict or set(body)!={'package','review_ticket','acknowledge'} or body['acknowledge'] is not True:
            raise ValueError('Et konkret lokalt klik på Opdater kræves')
        with self.manager.lock:
            self.authenticate_page(nonce)
            ticket=self.tickets.pop(body['review_ticket'],None)
            if (ticket is None or ticket['page']!=nonce or ticket['hash']!=digest(body['package'])
                    or ticket['base']!=self.manager.revision()):
                raise PermissionError('Gennemgangen er udløbet, brugt eller indholdet har ændret sig')
            # The approval key never leaves the pinned parent. Grant+apply are
            # contiguous; MCP cannot pick up a reusable approval between them.
            self.manager.grant(body['package'],self.manager.secret)
            result=self.manager.apply(body['package'])
            proposal_id=ticket['hash']
            path=self._directory()/(proposal_id+'.json')
            if path.exists() and not path.is_symlink():path.unlink()
            return {**result,'package_sha256':proposal_id}

    def acknowledge_fault(self, body, nonce, reader=read_registers):
        flow=self.flow.status()
        if flow and flow.get("phase") not in ("complete","failed","interrupted"):
            raise ValueError("Vent på opdateringsforløbet; manuelt stop er stadig tilgængeligt")
        if type(body) is not dict or set(body)!={'state_sha256','acknowledge'} or body['acknowledge'] is not True:
            raise ValueError('Lokal godkendelse af registerkontrol kræves')
        with self.manager.operation():
            self.authenticate_page(nonce)
            path=self.manager.data/'automatic_control.json';raw=path.read_bytes();state=json.loads(raw)
            if hashlib.sha256(raw).hexdigest()!=body['state_sha256']:raise ValueError('Status har ændret sig; kontrollér igen')
            if (state.get('status')!='fault' or state.get('restored') is not True or state.get('error')!=STALE_FAULT
                    or state.get('pending') is not None or state.get('auto_resume') or state.get('tesla_only_active')):
                raise ValueError('Fejlen kan ikke kvitteres sikkert')
            cfg=json.loads((self.manager.data/'options.json').read_text())
            endpoint=[cfg['deye_host'],cfg['deye_port'],cfg['slave_id']]
            baseline=state.get('baseline',{})
            if (state.get('endpoint')!=endpoint or type(baseline) is not dict or set(baseline)!=ADDRESSES
                    or any(type(value) is not int or not 0<=value<=65535 for value in baseline.values())):
                raise ValueError('Forbindelse eller oprindelige indstillinger afviger')
            for _ in range(3):
                observed={}
                for start,count in GROUPS:observed.update(reader(endpoint,3,start,count))
                if observed!=state['baseline']:raise ValueError('Gendannede registre afviger; ingen fejlkvittering')
            if path.read_bytes()!=raw:raise ValueError('Status ændrede sig under kontrollen')
            backup=self.manager.private/('fault-ack-'+secrets.token_hex(12)+'.json');atomic_json(backup,state)
            state.setdefault('events',[]).append(dict(at=datetime.now(timezone.utc).isoformat(),
                kind='restored_fault_acknowledged',previous_error=state['error'],reason='Local HA click; three live baseline reads'))
            state.update(status='disabled',error=None,auto_resume=False,tesla_only_active=False,
                         battery_timer_mode=None,command=None,stop_reason='local_verified_acknowledgement')
            # Preserve child ownership; atomic_json otherwise creates a root-only file.
            import os
            atomic_json(path,state);os.chown(path,1000,1000)
            return dict(status='disabled',restored=True,register_reads_verified=3,inverter_writes=0)
