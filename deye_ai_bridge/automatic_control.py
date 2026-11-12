"""Explicitly armed local TOU controller. No BMS/remote/export-mode writes."""
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Europe/Copenhagen')
TIMES = [0,400,800,1200,1600,2000]
GROUPS = [(108,2),(128,1),(148,6),(166,12)]
ADDRESSES = [a for start,count in GROUPS for a in range(start,start+count)]
CONTEXT = [111,115,117,130,142,145,146,212,213,552,1100]


def stamp(value):
    d = datetime.fromisoformat(value)
    if d.tzinfo is None:
        raise ValueError('Tidsstempel mangler tidszone.')
    return d


def charge_window_request(ctx,row,soc,voltage):
    """Average current needed to finish the current contiguous charging window."""
    if row.get('action')!='grid_charge':return None,None
    rows=ctx['optimization']['hours']
    start=next((i for i,r in enumerate(rows) if r['time']==row['time']),None)
    if start is None or 'battery_power_w' not in row:return None,None
    window=[row]
    for candidate in rows[start+1:]:
        if candidate.get('action')!='grid_charge' or stamp(candidate['time'])-stamp(window[-1]['time'])!=timedelta(hours=1):break
        window.append(candidate)
    end=stamp(window[-1]['time'])+timedelta(hours=1)
    hours=max(1/60,(end-datetime.now(timezone.utc)).total_seconds()/3600)
    target=max(float(r.get('charge_target_soc',r['projected_soc'])) for r in window)
    capacity=float(ctx['plan'].get('battery_capacity_kwh',32))
    eta=(float(ctx['plan'].get('roundtrip_efficiency_percent',90))/100)**.5
    watts=max(0,target-soc)*capacity/100/eta/hours*1000
    # Ten percent timing margin; round up to 5 A, bounded later by all limits.
    amps=5*math.ceil(watts*1.1/voltage/5)
    return max(5,amps),target


def valid_write(address, values):
    if type(values) is not list or not values:
        return False
    if address==62:
        if len(values)!=3 or any(type(v) is not int or not 0<=v<=65535 for v in values):return False
        try:
            a,b,c=values
            target=datetime(2000+(a>>8),a&255,b>>8,b&255,c>>8,c&255,tzinfo=TZ)
            return abs((target-datetime.now(TZ)).total_seconds())<=10
        except ValueError:return False
    for a,v in enumerate(values,address):
        if type(v) is not int:
            return False
        if a in (108,109,128):
            ok = 0 <= v <= 240
        elif 148 <= a <= 153:
            ok = 0 <= v <= 2359 and v%100 < 60
        elif 166 <= a <= 171:
            ok = 0 <= v <= 100
        elif 172 <= a <= 177:
            ok = v in (0,1)
        else:
            return False
        if not ok:
            return False
    return True


class AutomaticControl:
    def __init__(self, path, read, write, context, endpoint, lock, other_busy=lambda:False):
        self.path,self.read,self.write,self.context,self.endpoint = path,read,write,context,endpoint
        self.lock,self.other_busy = lock,other_busy
        self.generation=0

    def state(self):
        return json.loads(self.path.read_text()) if self.path.exists() else dict(status='disabled',restored=True,events=[])

    def save(self,s):
        pending=self.path.with_suffix('.tmp')
        with pending.open('w') as f:
            json.dump(s,f,ensure_ascii=False,indent=2,allow_nan=False)
            f.flush();os.fsync(f.fileno())
        pending.replace(self.path)

    def log(self,s,kind,**fields):
        s.setdefault('events',[]).append(dict(at=datetime.now(timezone.utc).isoformat(),kind=kind,**fields))
        s['events']=s['events'][-1000:]

    def report(self):
        with self.lock:
            return {k:v for k,v in self.state().items() if k!='endpoint'}

    def busy(self):
        return self.state().get('status') not in ('disabled','fault') or not self.state().get('restored',True)

    def values(self,start,count=1):
        rows=self.read(start,count)
        result={r['address']:r['uint16'] for r in rows}
        if any(a not in result or type(result[a]) is not int or not 0<=result[a]<=65535 for a in range(start,start+count)):
            raise ValueError('Ugyldig registeraflæsning.')
        return [result[a] for a in range(start,start+count)]

    def owned(self):
        return {str(a):v for start,count in GROUPS for a,v in zip(range(start,start+count),self.values(start,count))}

    def clock(self,strict=True):
        offsets=[];consecutive=0;deadline=time.monotonic()+14
        sync_path=self.path.with_name('clock_sync.json')
        for i in range(8):
            if i:
                if time.monotonic()>=deadline:break
                time.sleep(2)
            before=datetime.now(TZ);a,b,c=self.values(62,3);after=datetime.now(TZ)
            dt=datetime(2000+(a>>8),a&255,b>>8,b&255,c>>8,c&255,tzinfo=TZ)
            delta=(dt-(before+(after-before)/2)).total_seconds()
            if abs(delta)<120:
                consecutive+=1
                if i==0 or consecutive>=2:return delta
            else:
                consecutive=0;offsets.append(delta)
                if len(offsets)>=3 and max(offsets[-3:])-min(offsets[-3:])<=5:
                    old=json.loads(sync_path.read_text()) if sync_path.exists() else {}
                    if time.time()-old.get('attempt_at',0)<86400:
                        raise ValueError('Ur synkroniseret/forsøgt inden for 24 timer; afvigelsen er igen mindst to minutter.')
                    state=self.state()
                    if state.get('endpoint') and state['endpoint']!=list(self.endpoint()):raise ValueError('Forbindelsen er ændret; ingen urskrivning.')
                    now=datetime.now(TZ)
                    values=[((now.year-2000)<<8)|now.month,(now.day<<8)|now.hour,(now.minute<<8)|now.second]
                    record=dict(attempt_at=time.time(),target=now.isoformat(),offset_before=delta,status='requested')
                    sync_path.write_text(json.dumps(record))
                    try:
                        self.write(62,values)
                    except Exception as exc:
                        # Missing acknowledgement is resolved by reads; never blindly repeat.
                        record['reply_error']=str(exc)
                    matches=0
                    for _ in range(6):
                        time.sleep(1);ref=datetime.now(TZ);x,y,z=self.values(62,3)
                        actual=datetime(2000+(x>>8),x&255,y>>8,y&255,z>>8,z&255,tzinfo=TZ)
                        residual=(actual-ref).total_seconds()
                        matches=matches+1 if abs(residual)<=10 else 0
                        if matches>=2:
                            record.update(status='confirmed',offset_after=residual);sync_path.write_text(json.dumps(record))
                            return residual
                    record['status']='failed';sync_path.write_text(json.dumps(record))
                    raise ValueError('Synkronisering af inverteruret blev ikke bekræftet.')
        raise ValueError('Inverteruret kunne ikke bekræftes inden for to minutter.')

    def guards(self,ctx,strict=False):
        cfg=ctx['options'];plan=ctx['plan'];now=datetime.now(timezone.utc)
        if cfg.get('read_only',True) or not cfg.get('commissioning_enabled',False):
            raise ValueError('Skrivning kræver read_only: false og commissioning_enabled: true.')
        if not 0 <= (now-stamp(ctx['sample']['created'])).total_seconds() <= 120:
            raise ValueError('Målinger er over 120 sekunder gamle eller ugyldige.')
        if ctx.get('price_source')!='Andel FlexEnergi + N1 og afgifter' or not 0<=time.time()-ctx.get('price_updated',0)<=3600:
            raise ValueError('Aktuelle samlede elpriser inkl. transport og afgifter mangler.')
        model=ctx['optimization']
        zero_solar=model.get('weather_mode')=='zero_solar' and bool(model.get('hours')) and all(r.get('pv_forecast_kwh')==0 for r in model['hours'])
        if (not 0<=time.time()-ctx.get('weather_updated',0)<=7200 or ctx.get('weather_error')) and not zero_solar:
            raise ValueError('Solprognosen mangler uden en genberegnet plan med 0 kWh sol.')
        if not 0 <= (now-stamp(model['created'])).total_seconds() <= 180:
            raise ValueError('Planen er ikke aktuel.')
        row=next((r for r in model['hours'] if stamp(r['time'])<=now<stamp(r['time'])+timedelta(hours=1)),None)
        if row is None or row.get('action') not in ('hold','solar_charge','grid_charge','use_battery'):
            raise ValueError('Ingen gyldig plan for den aktuelle time.')
        if any(not isinstance(row.get(k),(float,int)) or not math.isfinite(row[k]) for k in ('price_dkk_kwh','reserve_soc','projected_soc')):
            raise ValueError('Planen indeholder ugyldige tal.')
        context={a:self.values(a)[0] for a in CONTEXT}
        daybit=1 << (datetime.now(TZ).weekday()+1)
        if context[111]!=1 or not context[146]&1 or (context[146]!=1 and not context[146]&daybit) or context[130]!=1:
            raise ValueError('SOC-styring, TOU for i dag og netladning skal være aktiveret.')
        if context[142] not in (1,2) or context[145]!=0 or context[1100]!=0:
            raise ValueError('Kræver Zero Export, Solar Sell slået fra og Remote-styring deaktiveret.')
        if not context[552]&4:
            raise ValueError('Netrelæet er åbent; gendan inverterens oprindelige indstillinger ved netudfald.')
        if not 0<=context[212]<=500 or not 0<=context[213]<=500:
            raise ValueError('Ugyldige BMS-strømgrænser.')
        voltage,soc,_,raw_w=self.values(587,4)
        power=raw_w-65536 if raw_w>32767 else raw_w
        if not 4000<=voltage<=6000 or not 0<=soc<=100:
            raise ValueError('Ugyldig batterispænding/SOC.')
        if power < -12000 or -power/(voltage/100 if voltage else 1)>210:
            raise ValueError('Opladning over softwaregrænsen 12000 W eller 210 A.')
        delta=self.clock(strict)
        floor=max(15,int(plan['minimum_soc_morning']),context[115]+1,context[117])
        maximum=int(plan['target_soc'])
        if not floor<maximum<=99:
            raise ValueError('Mål-SOC skal være højere end reserven og højst 99 %.')
        return row,context,dict(voltage_v=voltage/100,soc=soc,battery_w=power,clock_offset_seconds=round(delta,1)),floor,maximum

    def check_owned(self,s,current):
        expected=s['expected'];pending=s.get('pending') or {}
        def mismatch(values):
            return any(v not in (expected[a],pending.get(a,expected[a])) for a,v in values.items())
        if mismatch(current):
            # Fresh reads only: never repeat an ambiguous write.
            confirmed=0
            for _ in range(4):
                time.sleep(.5)
                fresh=self.owned()
                confirmed=0 if mismatch(fresh) else confirmed+1
                current.clear();current.update(fresh)
                if confirmed>=2:
                    self.log(s,'readback_recovered');self.save(s);return
            self.log(s,'external_change',values=current);self.save(s)
            raise ValueError('Indstillinger ændret uden for styringen; automatisk overskrivning er blokeret.')

    def command(self,s,address,values):
        if not valid_write(address,values):raise ValueError('Kommando uden for tilladt område.')
        current=self.owned();self.check_owned(s,current)
        desired={str(a):v for a,v in enumerate(values,address)}
        if all(current[a]==v for a,v in desired.items()):return
        expected=dict(s['expected']);expected.update(desired)
        s['pending']=desired;self.save(s)
        last_error=None
        for attempt in range(3):
            if s['endpoint']!=list(self.endpoint()):raise ValueError('Forbindelsen er ændret.')
            # Every repeat is an idempotent absolute assignment, guarded by fresh reads.
            current=self.owned();self.check_owned(s,current)
            if current!=expected:
                self.log(s,'write_requested' if attempt==0 else 'write_retry',address=address,values=values,attempt=attempt+1);self.save(s)
                try:self.write(address,values)
                except (TimeoutError,ConnectionError,OSError,RuntimeError) as exc:
                    if isinstance(exc,RuntimeError) and not isinstance(exc.__cause__,(TimeoutError,ConnectionError,OSError)) and not any(t in str(exc) for t in ('TimeoutError','ConnectionError')):raise
                    last_error=exc
                    self.log(s,'write_reply_missing',address=address,error=str(exc));self.save(s)
            deadline=time.monotonic()+10;stable_since=None;matches=0
            while True:
                try:
                    current=self.owned();self.check_owned(s,current)
                    if current==expected:
                        matches+=1
                        if stable_since is None:stable_since=time.monotonic()
                        if matches>=3 and time.monotonic()-stable_since>=2:
                            s.update(expected=expected,pending=None);self.save(s);return
                    else:stable_since=None;matches=0
                except (TimeoutError,ConnectionError,OSError,RuntimeError) as exc:
                    if isinstance(exc,RuntimeError) and not isinstance(exc.__cause__,(TimeoutError,ConnectionError,OSError)) and not any(t in str(exc) for t in ('TimeoutError','ConnectionError')):raise
                    last_error=exc;stable_since=None;matches=0
                if time.monotonic()>=deadline:break
                time.sleep(.5)
            # A further attempt begins with a new full register check. Unknown values abort.
        if last_error:raise last_error
        raise ValueError('Kommando ikke stabilt tilbagelæst efter tre forsøg.')

    def enable(self,ack):
        if ack is not True:raise ValueError('Bekræft at Copilot/cloudlogger er frakoblet.')
        with self.lock:
            self.generation+=1;generation=self.generation
        ctx=self.context()
        with self.lock:
            if generation!=self.generation:raise ValueError('Aktivering blev annulleret af en nyere stopanmodning.')
            old=self.state()
            if self.other_busy() or self.busy():raise ValueError('Afslut aktiv styring eller gendannelse først.')
            row,c,m,floor,maximum=self.guards(ctx,True)
            baseline=self.owned()
            for start,count in GROUPS:
                if not valid_write(start,[baseline[str(a)] for a in range(start,start+count)]):
                    raise ValueError('Oprindelige indstillinger ligger uden for understøttet gendannelse.')
            s=dict(status='enabling',restored=False,started=datetime.now(timezone.utc).isoformat(),endpoint=list(self.endpoint()),
                   baseline=baseline,expected=dict(baseline),pending=None,events=old.get('events',[]),observations=[],error=None,resume_attempts=old.get('resume_attempts',[]))
            s.update({k:v for k,v in old.items() if k.startswith('tesla_only_')})
            self.log(s,'enabled',baseline=baseline);self.save(s)
            try:
                # No charge/discharge demand while reshaping the TOU schedule.
                self.command(s,172,[0]*6)
                self.command(s,108,[0,0]);self.command(s,128,[0])
                self.command(s,148,TIMES)
                s['status']='active';self.save(s)
                self.apply(s,ctx,row,c,m,floor,maximum)
                s['resume_attempts']=[];s['recovery_exhausted']=False;self.save(s)
            except Exception as exc:
                self.disable('fault: '+str(exc))
                raise
            return self.report()

    def tesla_grid_mode(self,enabled):
        if type(enabled) is not bool:raise ValueError('Vælg til eller fra.')
        ctx=self.context()
        with self.lock:
            s=self.state()
            if s.get('status')!='active':raise ValueError('Aktivér automatisk styring først.')
            row,c,m,floor,maximum=self.guards(ctx)
            for key in list(s):
                if key.startswith('tesla_only_'):s.pop(key)
            s.update(tesla_only_active=enabled,effect_action=None)
            if enabled:
                s.update(tesla_only_started=time.time(),tesla_only_until=time.time()+12*3600,
                         tesla_only_seen_connected=ctx['sample'].get('tesla_connected')==1,
                         tesla_only_seen_charging=False)
            self.log(s,'tesla_grid_mode',enabled=enabled);self.save(s)
            try:self.apply(s,ctx,row,c,m,floor,maximum)
            except Exception as exc:
                self.disable('fault: '+str(exc));raise
            return self.report()

    def tesla_override(self,s,ctx,row):
        if not s.get('tesla_only_active'):return row
        sample=ctx['sample'];now=time.time()
        connected=sample.get('tesla_connected')
        power=ctx.get('tesla_power_kw')
        if connected==1:s['tesla_only_seen_connected']=True
        if isinstance(power,(int,float)) and power>.2:s['tesla_only_seen_charging']=True
        state=str(sample.get('tesla_charging_state') or '').lower()
        ended=(connected==0 and s.get('tesla_only_seen_connected')) or (s.get('tesla_only_seen_charging') and isinstance(power,(int,float)) and power<=.2 and state in ('complete','stopped','disconnected'))
        if ended:s.setdefault('tesla_only_stop_since',now)
        else:s.pop('tesla_only_stop_since',None)
        reason=None
        if now>=s.get('tesla_only_until',0):reason='Maksimal varighed på 12 timer nået.'
        elif connected==0 and not s.get('tesla_only_seen_connected') and now-s.get('tesla_only_started',now)>=1800:reason='Ingen bil tilsluttet inden for 30 minutter.'
        elif ended and now-s.get('tesla_only_stop_since',now)>=60:reason='Tesla er stoppet eller frakoblet.'
        if reason:
            s['tesla_only_active']=False;s['effect_action']=None
            self.log(s,'tesla_grid_mode_finished',reason=reason)
            return row
        return {**row,'action':'hold','reason':'Tesla – lad uden husbatteri: afladning og netopladning af husbatteriet er midlertidigt blokeret.'}

    def apply(self,s,ctx,row,c,m,floor,maximum):
        if s['endpoint']!=list(self.endpoint()):raise ValueError('Inverterforbindelsen er ændret.')
        row=self.tesla_override(s,ctx,row)
        plan=ctx['plan'];soc=m['soc'];voltage=m['voltage_v'];action=row['action']
        charge=min(190,c[212],max(0,int(float(plan['max_charge_w'])/voltage)))
        if charge>100:charge=5*(charge//5)
        requested,window_target=charge_window_request(ctx,row,soc,voltage)
        if requested is not None:charge=min(charge,requested)
        previous=(s.get('command') or {}).get('charge_a')
        if previous is not None and abs(previous-charge)<=3:
            charge=min(previous,190,c[212],max(0,int(float(plan['max_charge_w'])/voltage)))
        discharge=min(190,c[213],max(0,int(float(plan['max_discharge_w'])/voltage)))
        if soc>=maximum:s['charge_paused']=True
        elif soc<=maximum-2:s['charge_paused']=False
        if s.get('charge_paused'):charge=0
        target=max(floor,min(maximum,int(math.ceil(row.get('charge_target_soc',row['projected_soc'])))))
        if window_target is not None:target=max(floor,min(maximum,int(math.ceil(window_target))))
        # TOU accepts whole percentages. Rounding a planned discharge endpoint up
        # can equal the measured SOC and prevent every remaining fraction of the hour.
        # Allow less than one percentage point of dispatch tolerance, never below
        # the hard minimum; hold and Tesla protection remain conservative.
        planned_floor=float(row.get('discharge_floor_soc',floor))
        rounded_floor=math.floor(planned_floor) if action=='use_battery' else math.ceil(planned_floor)
        discharge_floor=max(math.ceil(floor),min(100,int(rounded_floor)))
        reserve_for_car=False
        if ctx.get('tesla_power_kw') is None or ctx.get('tesla_power_kw',0)>0:
            house_reserve=int(math.ceil(ctx['optimization'].get('dynamic_reserve',{}).get('reserve_soc',floor)))
            if house_reserve>discharge_floor:
                discharge_floor=min(100,house_reserve);reserve_for_car=True
        grid=action=='grid_charge' and soc<target and charge>0
        allow_discharge=action=='use_battery' and soc>discharge_floor
        if not allow_discharge:discharge=0
        effective=action if grid or allow_discharge or action=='solar_charge' else 'hold'
        target_values=dict(s['expected'])
        target_values.update({'108':charge,'109':discharge,'128':charge if grid else 0})
        target_values.update({str(a):target if grid else discharge_floor for a in range(166,172)})
        target_values.update({str(a):int(grid) for a in range(172,178)})
        current=self.owned();self.check_owned(s,current)
        if any(current[str(a)] != TIMES[a-148] for a in range(148,154)):
            raise ValueError('TOU-tiderne er ændret under styringen.')
        if target_values!=s['expected']:
            mode_changed=any(target_values[str(a)]!=s['expected'][str(a)] for a in range(166,178))
            # Current-only adjustment does not interrupt an unchanged charging mode.
            if mode_changed:
                self.command(s,172,[0]*6)
                self.command(s,166,[target if grid else discharge_floor]*6)
            if [charge,discharge]!=[s['expected']['108'],s['expected']['109']]:
                self.command(s,108,[charge,discharge])
            if target_values['128']!=s['expected']['128']:
                self.command(s,128,[charge if grid else 0])
            if mode_changed and grid:self.command(s,172,[1]*6)
        if action=='use_battery' and not allow_discharge:
            row={**row,'reason':f"Reservehold: SOC {soc:g} % er ved eller under den aktive reserve {discharge_floor} %. "+row['reason']}
        desired=dict(action=effective,requested_action=action,time=row['time'],reason=row['reason']+(' Husreserven beskyttes mens Tesla lader eller bildata er ukendte.' if reserve_for_car else ''),
                     charge_a=charge,discharge_a=discharge,grid_charge=grid,soc_target=target if grid else discharge_floor,
                     maximum_soc=maximum,reserve_soc=discharge_floor)
        signature=json.dumps(desired,sort_keys=True)
        changed=signature!=s.get('signature')
        if changed:
            self.log(s,'command_confirmed',command=desired,observed=m)
            s.update(signature=signature,command=desired,command_since=time.time())
        notice=ctx['optimization'].get('weather_notice','')
        weather_changed=notice!=s.get('weather_notice','')
        if weather_changed:
            self.log(s,'weather_fallback' if notice else 'weather_forecast_restored',notice=notice)
        s['weather_notice']=notice
        s.update(status='active',observed=m,checked_at=datetime.now(timezone.utc).isoformat())
        record_observation=time.time()-s.get('last_observation',0)>=30
        if record_observation:
            s.setdefault('observations',[]).append(dict(at=s['checked_at'],**m,action=effective))
            s['observations']=s['observations'][-500:];s['last_observation']=time.time()
        # Measurement is separate from register confirmation.
        charging=m['battery_w'] < -100
        discharging=m['battery_w'] > 100
        s['effect']='Ladning målt' if charging else 'Afladning målt' if discharging else 'Batteriet omtrent i ro'
        if s.get('effect_action')!=effective:
            s.update(effect_action=effective,effect_since=time.time())
        if time.time()-s.get('effect_since',time.time())>120:
            if grid and soc<=target-2 and not charging:
                raise ValueError('Netladning er kommanderet, men ikke målt efter 120 sekunder.')
            if not allow_discharge and m['battery_w']>300:
                raise ValueError('Reservehold er kommanderet, men batteriet aflader fortsat over 300 W.')
        if changed or record_observation or weather_changed:self.save(s)

    def disable(self,reason='manual_disable'):
        with self.lock:
            self.generation+=1
            s=self.state()
            if reason=='manual_disable':
                s['auto_resume']=False;s['tesla_only_active']=False;self.save(s)
            if s.get('status') in ('disabled','fault') and s.get('restored',True):return self.report()
            if s.get('status')=='verifying_restore':return self.report()
            s['auto_resume']=(reason.startswith('fault: Modbus read,') and any(t in reason for t in ('TimeoutError','ConnectionError')))
            if not s['auto_resume']:s['tesla_only_active']=False
            s.update(status='restoring',error=reason if reason!='manual_disable' else None,stop_reason=reason)
            self.log(s,'disable_requested',reason=reason);self.save(s)
            try:
                if s['endpoint']!=list(self.endpoint()):raise ValueError('Forbindelsen er ændret; kontrollér den oprindelige inverter manuelt.')
                # Reject any third-party value before touching any owned setting.
                current=self.owned()
                if current==s['baseline']:
                    s.update(status='verifying_restore',expected=current,pending=None,verify_started=time.time(),verify_checks=0)
                    self.save(s);return self.report()
                self.check_owned(s,current)
                # An ambiguous pending value may have applied: adopt observed owned state solely to restore.
                s.update(expected=current,pending=None);self.save(s)
                self.command(s,172,[0]*6)
                self.command(s,108,[0,0]);self.command(s,128,[0])
                b=s['baseline']
                self.command(s,166,[b[str(a)] for a in range(166,172)])
                self.command(s,148,[b[str(a)] for a in range(148,154)])
                self.command(s,108,[b['108'],b['109']]);self.command(s,128,[b['128']])
                self.command(s,172,[b[str(a)] for a in range(172,178)])
                s.update(status='verifying_restore',verify_started=time.time(),verify_checks=0,restored=False)
                self.log(s,'restore_written');self.save(s)
            except Exception as exc:
                self.emergency_cancel(s)
                s.update(status='restore_failed',error=str(exc),restored=False)
                self.log(s,'restore_failed',error=str(exc));self.save(s)
            return self.report()

    def emergency_cancel(self,s):
        if s.get('endpoint') != list(self.endpoint()):
            self.log(s,'emergency_cancel_blocked',reason='Forbindelsen er ændret; ingen skrivning til en anden inverter.');self.save(s);return
        # Cancel only current-limit values still attributable to this journal.
        # Never overwrite a foreign TOU setting to chase an external controller.
        for a in (108,128,109):
            try:
                actual=self.values(a)[0]
                if actual not in (s['expected'][str(a)],(s.get('pending') or {}).get(str(a),s['expected'][str(a)])):
                    continue
                s['pending']={str(a):0};self.log(s,'emergency_zero_requested',address=a);self.save(s)
                self.write(a,[0])
                if self.values(a)[0]!=0:raise ValueError('Nulværdi ikke bekræftet.')
                s['expected'][str(a)]=0;s['pending']=None;self.save(s)
            except Exception as exc:
                self.log(s,'emergency_zero_failed',address=a,error=str(exc));self.save(s)

    def tick(self):
        s=self.state()
        if s.get('status')=='restore_failed' and s.get('auto_resume'):
            self.retry_communication_restore(s)
            return
        if s.get('status')=='fault' and s.get('restored') and s.get('auto_resume'):
            self.resume_after_communication_fault(s)
            return
        if s.get('status')=='verifying_restore':
            with self.lock:
                s=self.state()
                try:
                    if s['endpoint']!=list(self.endpoint()) or self.owned()!=s['baseline']:
                        raise ValueError('Gendannelsen blev ikke fastholdt. Kontrollér manuelt.')
                    s['verify_checks']+=1
                    if time.time()-s['verify_started']>=60 and s['verify_checks']>=3:
                        s.update(status='fault' if s.get('error') else 'disabled',restored=True,command=None,observed=None,effect=None,finished=datetime.now(timezone.utc).isoformat())
                        self.log(s,'restore_verified')
                    self.save(s)
                except Exception as exc:
                    s.update(status='restore_failed',error=str(exc),restored=False)
                    if not self.communication_read_error(str(exc)):s['auto_resume']=False
                    self.save(s)
            return
        if s.get('status')!='active':return
        try:
            ctx=self.context()  # Network/forecast fetching does not hold the stop lock.
            with self.lock:
                s=self.state()
                if s.get('status')!='active':return
                row,c,m,floor,maximum=self.guards(ctx)
                self.apply(s,ctx,row,c,m,floor,maximum)
        except Exception as exc:
            self.disable('fault: '+str(exc))

    @staticmethod
    def communication_read_error(error):
        return 'Modbus read,' in error and any(t in error for t in ('TimeoutError','ConnectionError'))

    def retry_communication_restore(self,s):
        if time.time()-s.get('restore_retry_at',0)<30:return
        with self.lock:
            s=self.state()
            if s.get('status')!='restore_failed' or not s.get('auto_resume'):return
            if not self.communication_read_error(s.get('error','')):
                s['auto_resume']=False;self.save(s);return
            s['restore_retry_at']=time.time();self.save(s)
            try:
                if s['endpoint']!=list(self.endpoint()):raise ValueError('Forbindelsen er ændret.')
                previous=None
                for _ in range(3):
                    current=self.owned()
                    if current!=s['baseline']:self.check_owned(s,current)
                    if previous is not None and current!=previous:raise ValueError('Registre er ikke stabile efter forbindelsesfejl.')
                    previous=current;time.sleep(.5)
                attempts=[t for t in s.get('restore_retry_attempts',[]) if time.time()-t<3600]
                if len(attempts)>=3:
                    if s.get('restore_resume_after',0)>time.time():return
                    if s.get('restore_resume_after'):
                        attempts=[];s.pop('restore_resume_after',None)
                    else:
                        s['restore_resume_after']=time.time()+900;s['recovery_exhausted']=True
                        self.log(s,'restore_retry_cooldown',reason='Tre gendannelsesforsøg; pause 15 minutter.');self.save(s);return
                attempts.append(time.time());s['restore_retry_attempts']=attempts
                self.log(s,'communication_restored_retry');self.save(s)
                self.disable(s['stop_reason'])
            except Exception as exc:
                s=self.state()
                if not self.communication_read_error(str(exc)):
                    s['auto_resume']=False;s['error']=str(exc)
                    self.log(s,'restore_retry_blocked',error=str(exc));self.save(s)

    def resume_after_communication_fault(self,s):
        if time.time()-s.get('resume_last_attempt',0)<60:return
        with self.lock:
            s=self.state()
            if not s.get('auto_resume') or not s.get('restored') or s.get('status')!='fault':return
            attempts=[t for t in s.get('resume_attempts',[]) if time.time()-t<3600]
            if s.get('resume_after',0)>time.time():return
            if s.get('resume_after'):
                try:
                    if s['endpoint']!=list(self.endpoint()) or self.owned()!=s['baseline']:raise ValueError('Gendannede registre afviger.')
                    self.guards(self.context(),True)
                    since=s.setdefault('recovery_stable_since',time.time())
                    s['recovery_stable_checks']=s.get('recovery_stable_checks',0)+1
                    self.save(s)
                    if time.time()-since<30 or s['recovery_stable_checks']<3:return
                    s.pop('resume_after',None);s.pop('recovery_stable_since',None);s.pop('recovery_stable_checks',None)
                    s['resume_attempts']=[];attempts=[];self.save(s)
                except Exception as exc:
                    s.pop('recovery_stable_since',None);s['recovery_stable_checks']=0;self.save(s)
                    if not self.communication_read_error(str(exc)):
                        s['auto_resume']=False;s['error']=str(exc);self.save(s)
                    return
            if len(attempts)>=3:
                s['resume_after']=time.time()+900;s['recovery_exhausted']=True
                self.log(s,'auto_resume_cooldown',reason='Tre mislykkede genforsøg; ny kontrol efter 15 minutter.');self.save(s);return
            attempts.append(time.time());s.update(resume_attempts=attempts,resume_last_attempt=time.time());self.save(s)
            try:
                if s['endpoint']!=list(self.endpoint()):raise ValueError('Forbindelsen er ændret.')
                # Require repeated baseline confirmation; do not adopt foreign values.
                for _ in range(3):
                    if self.owned()!=s['baseline']:raise ValueError('Gendannede registre afviger.')
                    time.sleep(.5)
                self.guards(self.context(),True)
                # Keep recovery pending until enable() persists its enabling state.
                # Clearing it here exposed a false terminal fault to the notifier.
                self.log(s,'auto_resume_ready');self.save(s)
                self.enable(True)
            except Exception as exc:
                current=self.state()
                if current.get('status')=='fault':
                    self.log(current,'auto_resume_wait',error=str(exc));self.save(current)

    def worker(self):
        with self.lock:
            s=self.state();s['auto_resume']=False;self.save(s)
        if self.busy():self.disable('process_restart: Gendan før ny manuel aktivering.')
        while True:
            time.sleep(5)
            try:self.tick()
            except Exception as exc:
                self.disable('worker_fault: '+str(exc))


class DayPlans:
    def __init__(self,path):
        self.path=path;self.lock=threading.RLock()

    def read(self):
        return json.loads(self.path.read_text()) if self.path.exists() else dict(rows={},updated=None)

    def update(self,model):
        with self.lock:
            data=self.read();now=datetime.now(timezone.utc)
            if data.get('updated') and (now-stamp(data['updated'])).total_seconds()<300:return
            cutoff=now.astimezone(TZ).date()-timedelta(days=1)
            data['rows']={k:v for k,v in data['rows'].items() if stamp(k).astimezone(TZ).date()>=cutoff}
            for row in model.get('hours',[]):data['rows'][row['time']]=row
            data['updated']=now.isoformat()
            pending=self.path.with_suffix('.tmp')
            with pending.open('w') as f:
                json.dump(data,f,ensure_ascii=False,allow_nan=False);f.flush();os.fsync(f.fileno())
            pending.replace(self.path)

    def view(self,model,samples=(),events=(),now=None):
        now=now or datetime.now(TZ)
        with self.lock:data=self.read()
        rows=dict(data['rows']);rows.update({r['time']:r for r in model.get('hours',[])})
        by_utc={stamp(k).astimezone(timezone.utc):v for k,v in rows.items()}
        actual={}
        ordered=[]
        for sample in samples:
            try:
                at=stamp(sample['created']).astimezone(timezone.utc)
                hour=at.astimezone(TZ).replace(minute=0,second=0,microsecond=0).astimezone(timezone.utc)
                slot=actual.setdefault(hour,dict(watts=[],soc=None,coverage_seconds=0.,battery_out_kwh=0.,battery_in_kwh=0.,grid_import_kwh=0.))
                if type(sample.get('battery_w')) in (float,int):slot['watts'].append(sample['battery_w'])
                if sample.get('soc') is not None:slot['soc']=sample['soc']
                ordered.append((at,sample))
            except (KeyError,ValueError,TypeError):continue
        ordered.sort(key=lambda x:x[0])
        def positive_energy(a,b,seconds):
            if a>=0 and b>=0:return (a+b)*.5*seconds/3600000
            if a<=0 and b<=0:return 0.
            return max(a,b)**2/abs(b-a)*.5*seconds/3600000
        for (ta,a),(tb,b) in zip(ordered,ordered[1:]):
            seconds=(tb-ta).total_seconds()
            if not 0<seconds<=120:continue
            begin=ta;stop=min(tb,now.astimezone(timezone.utc))
            while begin<stop:
                hour=begin.replace(minute=0,second=0,microsecond=0)
                cut=min(stop,hour+timedelta(hours=1));duration=(cut-begin).total_seconds()
                slot=actual.setdefault(hour,dict(watts=[],soc=None,coverage_seconds=0.,battery_out_kwh=0.,battery_in_kwh=0.,grid_import_kwh=0.))
                fa=(begin-ta).total_seconds()/seconds;fb=(cut-ta).total_seconds()/seconds
                valid=True
                for key in ('battery_w','grid_w'):
                    x,y=a.get(key),b.get(key)
                    if type(x) not in (int,float) or type(y) not in (int,float) or not math.isfinite(x) or not math.isfinite(y):valid=False;break
                if valid:
                    x,y=a['battery_w'],b['battery_w'];left=x+(y-x)*fa;right=x+(y-x)*fb
                    slot['battery_out_kwh']+=positive_energy(left,right,duration)
                    slot['battery_in_kwh']+=positive_energy(-left,-right,duration)
                    x,y=a['grid_w'],b['grid_w']
                    slot['grid_import_kwh']+=positive_energy(x+(y-x)*fa,x+(y-x)*fb,duration)
                    slot['coverage_seconds']+=duration
                begin=cut
        confirmed={};actions={}
        for event in events:
            if event.get('kind')=='command_confirmed':
                key=stamp(event['command']['time']).astimezone(timezone.utc);confirmed[key]=event
                actions.setdefault(key,set()).add(event['command']['action'])
        days=[]
        for offset in (0,1):
            date=now.date()+timedelta(days=offset)
            begin=datetime.combine(date,datetime.min.time(),TZ).astimezone(timezone.utc)
            end=datetime.combine(date+timedelta(days=1),datetime.min.time(),TZ).astimezone(timezone.utc)
            hours=[];t=begin
            while t<end:
                row=dict(by_utc.get(t) or {})
                m=actual.get(t,{})
                row.update(time=t.astimezone(TZ).isoformat(),known=bool(row),past=t+timedelta(hours=1)<=now,
                           actual_battery_w=round(sum(m['watts'])/len(m['watts'])) if m.get('watts') else None,
                           actual_soc=m.get('soc'),command_confirmed=confirmed.get(t),
                           actual_energy={k:round(m.get(k,0),3) for k in ('battery_out_kwh','battery_in_kwh','grid_import_kwh')},
                           actual_coverage_seconds=round(m.get('coverage_seconds',0)),
                           confirmed_actions=sorted(actions.get(t,set())))
                hours.append(row);t+=timedelta(hours=1)
            days.append(dict(date=date.isoformat(),hours=hours,known_hours=sum(r['known'] for r in hours)))
        return dict(created=datetime.now(timezone.utc).isoformat(),days=days,plan_created=model.get('created'))
