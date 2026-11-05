import copy
import json
import tempfile
import threading
import unittest
from datetime import datetime as Date, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import automatic_control as mod


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=Date(2026,11,7,9,tzinfo=timezone.utc);self.elapsed=1000.
        outer=self
        class Clock(Date):
            @classmethod
            def now(cls,tz=None):return outer.now.astimezone(tz) if tz else outer.now.replace(tzinfo=None)
        for target,replacement in [('datetime',Clock),('time.time',lambda:self.now.timestamp()),('time.monotonic',lambda:self.elapsed),('time.sleep',self.advance)]:
            p=patch('automatic_control.'+target,replacement);p.start();self.addCleanup(p.stop)
        self.r={108:10,109:200,128:10,111:1,115:14,117:14,130:1,142:2,145:0,146:255,552:5,212:190,213:190,1100:0,587:5200,588:30,589:0,590:0}
        self.r.update(dict(zip(range(148,154),[1000,955,2055,2100,100,400])))
        self.r.update(dict(zip(range(166,172),[40,15,15,15,15,15])));self.r.update({a:1 for a in range(172,178)})
        self.original={str(a):self.r[a] for a in mod.ADDRESSES};self.writes=[];self.stale=False;self.badprice=False
        self.plan=dict(target_soc=80,minimum_soc_morning=20,max_charge_w=4500,max_discharge_w=4500)
        self.action='grid_charge';self.cloud=False;self.endpoint=('test',502,1)
        self.fail=None;self.ignore=False;self.foreign=False
        self.c=mod.AutomaticControl(Path(self.temp.name)/'auto.json',self.read,self.write,self.context,lambda:self.endpoint,threading.RLock())

    def advance(self,seconds):self.now+=timedelta(seconds=seconds);self.elapsed+=seconds

    def read(self,address,count):
        if address==62:
            d=self.now.astimezone(mod.TZ)
            v=[((d.year-2000)<<8)|d.month,(d.day<<8)|d.hour,(d.minute<<8)|d.second]
            return [dict(address=a,uint16=x) for a,x in enumerate(v,62)]
        return [dict(address=a,uint16=self.r[a]) for a in range(address,address+count)]

    def write(self,address,values):
        self.assertTrue(mod.valid_write(address,values));self.writes.append((address,list(values)))
        if not self.ignore:self.r.update({a:v for a,v in enumerate(values,address)})
        if self.fail==address:self.fail=None;raise TimeoutError('ambiguous accepted write')

    def context(self):
        return dict(options=dict(read_only=False,commissioning_enabled=True),plan=self.plan,
            sample=dict(created=(self.now-timedelta(seconds=130) if self.stale else self.now).isoformat()),
            price_source='Offentlig spotpris (reserve)' if self.badprice else 'Andel FlexEnergi + N1 og afgifter',price_updated=self.now.timestamp(),weather_updated=self.now.timestamp(),weather_error=None,
            optimization=dict(created=self.now.isoformat(),hours=[dict(time=self.now.replace(minute=0,second=0,microsecond=0).isoformat(),action=self.action,price_dkk_kwh=1.5,reserve_soc=30,projected_soc=50,charge_target_soc=60,discharge_floor_soc=20,reason='test')]))

    def verify_restore(self):
        for _ in range(13):self.advance(5);self.c.tick()
        self.assertTrue(self.c.state()['restored']);self.assertEqual(self.c.owned(),self.original)

    def test_enable_no_chatter_and_disable(self):
        self.c.enable(True);self.assertEqual(self.c.state()['status'],'active')
        self.assertEqual((self.r[108],self.r[109],self.r[128]),(86,0,86))
        self.assertEqual([self.r[a] for a in range(172,178)],[1]*6)
        self.r[590]=65536-4400;count=len(self.writes);self.advance(5);self.c.tick();self.assertEqual(count,len(self.writes))
        self.c.disable();self.verify_restore();self.assertEqual(self.c.state()['status'],'disabled')
        self.assertNotIn('endpoint',self.c.report())

    def test_hold_discharge_solar_and_soc_cap(self):
        self.c.enable(True);self.action='hold';self.c.tick()
        self.assertEqual((self.r[109],self.r[128]),(0,0));self.assertEqual(self.r[172],0)
        self.action='use_battery';self.c.tick();self.assertEqual(self.r[109],86)
        self.r[588]=20;self.c.tick();self.assertEqual(self.r[109],0)
        self.r[588]=80;self.action='solar_charge';self.c.tick();self.assertEqual(self.r[108],0)
        self.r[588]=79;self.c.tick();self.assertEqual(self.r[108],0)
        self.r[588]=78;self.c.tick();self.assertEqual(self.r[108],86)

    def test_stale_prices_and_configuration_block(self):
        for mutate in ('stale','badprice'):
            setattr(self,mutate,True)
            with self.assertRaises(ValueError):self.c.enable(True)
            self.assertFalse(self.writes);setattr(self,mutate,False)
        with self.assertRaises(ValueError):self.c.enable(False)
        self.assertFalse(self.writes)
        self.c.enable(True);self.stale=True;self.c.tick();self.verify_restore();self.assertEqual(self.c.state()['status'],'fault')

    def test_partial_timeout_and_ignored_write(self):
        self.fail=148
        with self.assertRaises(TimeoutError):self.c.enable(True)
        self.verify_restore();self.assertEqual(self.c.state()['status'],'fault')

    def test_foreign_edit_cancels_owned_currents_without_chasing(self):
        self.c.enable(True);self.r[169]=77;self.c.tick()
        self.assertEqual(self.c.state()['status'],'restore_failed')
        self.assertEqual(self.r[169],77);self.assertEqual((self.r[108],self.r[128]),(0,0))
        self.r.update({int(a):v for a,v in self.original.items()});self.c.disable();self.verify_restore()

    def test_restart_and_endpoint_guard(self):
        self.c.enable(True)
        restarted=mod.AutomaticControl(self.c.path,self.read,self.write,self.context,lambda:self.endpoint,threading.RLock())
        restarted.disable('process_restart');self.c=restarted;self.verify_restore();self.assertNotEqual(self.c.state()['status'],'active')

    def test_bms_and_effect_checks(self):
        self.r[212]=50;self.c.enable(True);self.assertEqual(self.r[108],50)
        self.r[590]=65536-5600;self.c.tick();self.verify_restore();self.assertEqual(self.c.state()['status'],'fault')

    def test_disable_cancels_pending_enable(self):
        original=self.c.context
        def provider():
            ctx=original();self.c.disable();return ctx
        self.c.context=provider
        with self.assertRaises(ValueError):self.c.enable(True)
        self.assertFalse(self.writes)

    def test_endpoint_change_never_writes_new_inverter(self):
        self.c.enable(True);before=len(self.writes);self.endpoint=('other',502,1);self.c.tick()
        self.assertEqual(len(self.writes),before);self.assertEqual(self.c.state()['status'],'restore_failed')

    def test_unconfirmed_write_never_arms(self):
        self.ignore=True
        with self.assertRaises(ValueError):self.c.enable(True)
        self.verify_restore();self.assertNotEqual(self.c.state()['status'],'active')

    def test_missing_effect_stops_after_grace(self):
        self.c.enable(True);self.advance(125);self.c.tick();self.verify_restore()
        self.assertEqual(self.c.state()['status'],'fault')

    def test_tesla_house_reserve(self):
        original=self.context
        def context():
            ctx=original();ctx['tesla_power_kw']=11;ctx['optimization']['dynamic_reserve']={'reserve_soc':50};return ctx
        self.c.context=context;self.action='use_battery';self.c.enable(True)
        self.assertEqual(self.r[109],0);self.assertEqual(self.r[166],50)
        self.r[588]=60;self.c.tick();self.assertGreater(self.r[109],0)

    def test_read_only_change_restores(self):
        self.c.enable(True);original=self.context
        def context():
            ctx=original();ctx['options']['read_only']=True;return ctx
        self.c.context=context;self.c.tick();self.verify_restore();self.assertEqual(self.c.state()['status'],'fault')

    def test_grid_loss_restores_original_backup_limits(self):
        self.c.enable(True);self.r[552]=1;self.c.tick();self.verify_restore()
        self.assertEqual(self.r[109],200);self.assertEqual(self.c.state()['status'],'fault')

    def test_invalid_writes(self):
        for a,v in [(212,[90]),(1100,[1]),(108,[True]),(148,[2360]),(166,[101]),(172,[2]),(107,[1,1])]:
            self.assertFalse(mod.valid_write(a,v))


class CalendarTests(unittest.TestCase):
    def test_missing_prices_dates_and_dst(self):
        with tempfile.TemporaryDirectory() as temp:
            plans=mod.DayPlans(Path(temp)/'days.json')
            now=Date(2026,11,7,10,tzinfo=mod.TZ)
            row=dict(time=now.replace(minute=0).isoformat(),action='hold',price_dkk_kwh=1,projected_soc=40,reserve_soc=30)
            out=plans.view({'hours':[row]},now=now)
            self.assertEqual(len(out['days']),2);self.assertEqual([len(d['hours']) for d in out['days']],[24,24]);self.assertEqual(out['days'][1]['known_hours'],0)
            out=plans.view({'hours':[]},now=Date(2026,10,25,12,tzinfo=mod.TZ));self.assertEqual(len(out['days'][0]['hours']),25)
            out=plans.view({'hours':[]},now=Date(2026,3,29,12,tzinfo=mod.TZ));self.assertEqual(len(out['days'][0]['hours']),23)

if __name__=='__main__':unittest.main()
