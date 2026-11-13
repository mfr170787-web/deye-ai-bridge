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

    def test_transient_readback_requires_two_fresh_matches_without_write(self):
        self.c.enable(True)
        state=self.c.state();current=self.c.owned();current['108']=85
        writes=len(self.writes)
        self.c.check_owned(state,current)
        self.assertEqual(current,state['expected'])
        self.assertEqual(len(self.writes),writes)
        self.assertEqual(state['events'][-1]['kind'],'readback_recovered')

    def test_resume_only_after_verified_read_timeout(self):
        self.c.enable(True)
        self.c.disable('fault: Modbus read, register 148: TimeoutError: timed out')
        for _ in range(20):
            self.advance(5);self.c.tick()
            if self.c.state().get('restored'):break
        self.assertTrue(self.c.state()['restored'])
        self.c.tick()
        self.assertEqual(self.c.state()['status'],'active')
        self.assertTrue(any(e['kind']=='auto_resume_ready' for e in self.c.state()['events']))

    def test_resume_remains_silent_until_enable_has_started(self):
        self.c.enable(True)
        self.c.disable('fault: Modbus read, register 148: TimeoutError: timed out')
        for _ in range(20):
            self.advance(5);self.c.tick()
            if self.c.state().get('restored'):break
        original_enable=self.c.enable
        def observe_transition(ack):
            state=self.c.state()
            self.assertEqual(state['status'],'fault')
            self.assertTrue(state['auto_resume'], 'Notifier must still see recovery pending')
            return original_enable(ack)
        with patch.object(self.c,'enable',side_effect=observe_transition):self.c.tick()
        self.assertEqual(self.c.state()['status'],'active')

    def test_manual_stop_cancels_pending_resume(self):
        self.c.enable(True)
        self.c.disable('fault: Modbus read, register 148: ConnectionError')
        for _ in range(20):
            self.advance(5);self.c.tick()
            if self.c.state().get('restored'):break
        self.c.disable();self.c.tick()
        self.assertNotEqual(self.c.state()['status'],'active')
        self.assertFalse(self.c.state().get('auto_resume'))

    def test_restore_retries_when_connection_returns(self):
        self.c.enable(True)
        read=self.c.read
        def offline(*args):raise ConnectionError('Modbus read, register 108: ConnectionError: closed')
        self.c.read=offline
        self.c.disable('fault: Modbus read, register 212: ConnectionError: closed')
        self.assertEqual(self.c.state()['status'],'restore_failed')
        writes=len(self.writes);self.c.tick();self.assertEqual(len(self.writes),writes)
        self.c.read=read;self.advance(31);self.c.tick()
        self.assertEqual(self.c.state()['status'],'verifying_restore')
        for _ in range(20):
            self.advance(5);self.c.tick()
            if self.c.state().get('restored'):break
        self.assertTrue(self.c.state()['restored']);self.c.tick()
        self.assertEqual(self.c.state()['status'],'active')

    def test_restore_retry_rejects_foreign_register(self):
        self.c.enable(True);state=self.c.state()
        state.update(status='restore_failed',auto_resume=True,error='Modbus read, register 108: TimeoutError',stop_reason='fault: Modbus read, register 108: TimeoutError')
        self.c.save(state);self.r[148]=1234;writes=len(self.writes)
        self.c.tick()
        self.assertEqual(len(self.writes),writes)
        self.assertFalse(self.c.state()['auto_resume'])

    def test_first_write_ignored_is_retried_after_fresh_read(self):
        original=self.c.write;ignored=[]
        def flaky(address,values):
            if address==148 and not ignored:
                ignored.append(True);self.writes.append((address,list(values)));return
            original(address,values)
        self.c.write=flaky;self.c.enable(True)
        self.assertEqual(self.c.state()['status'],'active')
        self.assertEqual(sum(a==148 for a,v in self.writes),2)
        self.assertTrue(any(e['kind']=='write_retry' for e in self.c.state()['events']))

    def test_persistent_ignored_write_is_bounded(self):
        self.ignore=True
        with self.assertRaises(ValueError):self.c.enable(True)
        self.assertLessEqual(sum(a==172 for a,v in self.writes),3)
        self.assertNotEqual(self.c.state()['status'],'active')

    def clock_with_drift(self,seconds):
        self.drift=seconds
        read=self.c.read;write=self.c.write
        def clock_read(address,count):
            if address!=62:return read(address,count)
            d=(self.now+timedelta(seconds=self.drift)).astimezone(mod.TZ)
            return [dict(address=a,uint16=v) for a,v in enumerate([((d.year-2000)<<8)|d.month,(d.day<<8)|d.hour,(d.minute<<8)|d.second],62)]
        def clock_write(address,values):
            if address!=62:return write(address,values)
            self.assertTrue(mod.valid_write(address,values));self.writes.append((address,values));self.drift=0
        self.c.read=clock_read;self.c.write=clock_write

    def test_clock_accepts_under_two_minutes_without_writes(self):
        self.clock_with_drift(-90)
        self.assertAlmostEqual(self.c.clock(),-90)
        self.assertFalse(self.writes)

    def test_clock_sync_confirms_and_limits_to_once_daily(self):
        self.clock_with_drift(-180)
        self.assertLess(abs(self.c.clock()),10)
        self.assertEqual(len(self.writes),1)
        self.drift=-180
        with self.assertRaises(ValueError):self.c.clock()
        self.assertEqual(len(self.writes),1)
        record=json.loads(self.c.path.with_name('clock_sync.json').read_text())
        self.assertEqual(record['status'],'confirmed')

    def test_charge_190a_and_lower_bms_limit(self):
        self.plan['max_charge_w']=11000
        self.c.enable(True)
        self.assertEqual((self.r[108],self.r[128]),(190,190))
        self.r[212]=120;self.c.tick()
        self.assertEqual((self.r[108],self.r[128]),(120,120))
        self.action='use_battery';self.c.tick()
        self.assertLessEqual(self.r[109],190)

    def test_discharge_190a_respects_bms_power_reserve_and_tesla(self):
        self.plan['max_discharge_w']=11000;self.action='use_battery'
        self.c.enable(True);self.assertEqual(self.r[109],190)
        self.r[213]=120;self.c.tick();self.assertEqual(self.r[109],120)
        self.plan['max_discharge_w']=4500;self.c.tick();self.assertEqual(self.r[109],86)
        self.plan['max_discharge_w']=11000;self.r[588]=20;self.c.tick()
        self.assertEqual(self.r[109],0)
        self.r[588]=30;self.c.tick();self.c.battery_timer('no_discharge',1)
        self.assertEqual(self.r[109],0)
        self.c.battery_timer('cancel');self.assertEqual(self.r[109],120)

    def test_charge_window_uses_lower_current_when_time_allows(self):
        ctx=self.context();row=ctx['optimization']['hours'][0]
        row.update(battery_power_w=-4000,charge_target_soc=45,projected_soc=45)
        second=dict(row,time=(self.now+timedelta(hours=1)).isoformat(),charge_target_soc=60,projected_soc=60)
        ctx['optimization']['hours'].append(second)
        amps,target=mod.charge_window_request(ctx,row,30,52)
        self.assertEqual(target,60)
        self.assertTrue(100<=amps<=110)
        # A short window needs the ceiling; apply() still clamps the request.
        self.advance(110*60)
        amps,_=mod.charge_window_request(ctx,{**row},30,52)
        self.assertGreater(amps,190)

    def test_voltage_jitter_does_not_rewrite_current_or_grid_flags(self):
        self.plan['max_charge_w']=8000;self.c.enable(True)
        previous=self.r[108];self.writes=[]
        self.r[587]=5310;self.c.tick()
        self.assertEqual(self.r[108],previous)
        self.assertFalse(any(a==172 for a,v in self.writes))

    def test_successful_reconnects_do_not_exhaust_restart_budget(self):
        self.c.enable(True)
        for _ in range(4):
            self.c.disable('fault: Modbus read, register 148: TimeoutError: timed out')
            for _ in range(20):
                self.advance(5);self.c.tick()
                if self.c.state().get('restored'):break
            self.c.tick()
            self.assertEqual(self.c.state()['status'],'active')
            self.assertEqual(self.c.state()['resume_attempts'],[])

    def test_failed_restart_budget_cools_down_then_recovers(self):
        self.c.enable(True)
        self.c.disable('fault: Modbus read, register 148: TimeoutError: timed out')
        for _ in range(20):
            self.advance(5);self.c.tick()
            if self.c.state().get('restored'):break
        state=self.c.state();state['resume_attempts']=[self.now.timestamp()]*3;self.c.save(state)
        self.c.tick();self.assertTrue(self.c.state()['auto_resume'])
        self.assertTrue(self.c.state()['recovery_exhausted'])
        self.advance(901)
        for _ in range(4):self.c.tick();self.advance(15)
        self.assertEqual(self.c.state()['status'],'active')






    def test_fractional_dispatch_reserve_does_not_block_integer_soc(self):
        original=self.context
        def context():
            ctx=original();ctx['tesla_power_kw']=0
            ctx['optimization']['hours'][0].update(discharge_floor_soc=66.4,projected_soc=66.4)
            return ctx
        self.c.context=context;self.action='use_battery';self.r[588]=67
        self.c.enable(True)
        self.assertGreater(self.r[109],0);self.assertEqual(self.r[166],66)
        self.assertEqual(self.c.state()['command']['action'],'use_battery')
        self.r[588]=66;self.c.tick()
        self.assertEqual(self.r[109],0)
        self.assertEqual(self.c.state()['command']['action'],'hold')
        self.assertIn('Reservehold',self.c.state()['command']['reason'])
        # A higher hard minimum must never be rounded down.
        self.plan['minimum_soc_morning']=67;self.r[588]=67;self.c.tick()
        self.assertEqual(self.r[109],0);self.assertEqual(self.r[166],67)

    def test_tesla_reserve_is_still_rounded_up(self):
        original=self.context
        def context():
            ctx=original();ctx['tesla_power_kw']=11
            ctx['optimization']['dynamic_reserve']={'reserve_soc':66.4}
            ctx['optimization']['hours'][0]['discharge_floor_soc']=66.4
            return ctx
        self.c.context=context;self.action='use_battery';self.r[588]=67
        self.c.enable(True)
        self.assertEqual(self.r[166],67);self.assertEqual(self.r[109],0)

    def test_charge_current_software_trip(self):
        self.plan['max_charge_w']=11000;self.c.enable(True)
        self.r[590]=65536-11500
        self.c.tick();self.verify_restore()
        self.assertEqual(self.c.state()['status'],'fault')

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

    def test_no_discharge_timer_expires_and_restores_fresh_plan(self):
        self.action='use_battery';self.c.enable(True)
        self.c.battery_timer('no_discharge',1)
        self.assertEqual(self.r[109],0)
        self.advance(3590);self.c.tick();self.assertEqual(self.r[109],0)
        self.advance(11);self.c.tick()
        self.assertIsNone(self.c.state()['battery_timer_mode'])
        self.assertGreater(self.r[109],0)
        self.assertTrue(any(e['kind']=='battery_timer_finished' for e in self.c.state()['events']))

    def test_charge_timer_uses_deadline_soc_and_all_limits(self):
        self.action='hold';self.plan['max_charge_w']=11000;self.c.enable(True)
        self.c.battery_timer('charge',1)
        self.assertEqual(self.r[109],0);self.assertEqual(self.r[128],190)
        self.assertEqual(self.r[166],80)
        self.c.battery_timer('charge',12)
        self.assertGreater(self.r[128],0);self.assertLess(self.r[128],100)
        self.r[212]=20;self.c.tick();self.assertLessEqual(self.r[128],20)
        self.r[588]=80;self.c.tick();self.assertEqual(self.r[128],0)
        self.assertEqual(self.r[109],0)
        self.c.battery_timer('cancel');self.assertEqual(self.c.state()['command']['action'],'hold')
        self.assertIsNone(self.c.state()['battery_timer_mode'])

    def test_charge_timer_expiry_replans_and_modes_are_exclusive(self):
        self.action='use_battery';self.c.enable(True)
        self.c.battery_timer('no_discharge',1);self.c.battery_timer('charge',1)
        self.assertEqual(self.c.state()['battery_timer_mode'],'charge')
        self.advance(3601);self.c.tick()
        self.assertGreater(self.r[109],0);self.assertEqual(self.r[128],0)
        self.c.battery_timer('charge',1);self.c.battery_timer('no_discharge',1)
        self.assertEqual(self.c.state()['battery_timer_mode'],'no_discharge')

    def test_timer_requires_active_and_valid_hours_and_manual_stop_cancels(self):
        with self.assertRaises(ValueError):self.c.battery_timer('charge',1)
        self.c.enable(True)
        for hours in (0,13,True,1.5,None):
            with self.assertRaises(ValueError):self.c.battery_timer('charge',hours)
        self.c.battery_timer('no_discharge',12);self.c.disable()
        self.assertIsNone(self.c.state()['battery_timer_mode'])

    def test_timer_deadline_survives_communication_recovery(self):
        self.c.enable(True);self.c.battery_timer('no_discharge',2)
        deadline=self.c.state()['battery_timer_until']
        self.c.disable('fault: Modbus read, register 148: TimeoutError: timed out')
        for _ in range(20):
            self.advance(5);self.c.tick()
            if self.c.state().get('restored'):break
        self.c.tick()
        self.assertEqual(self.c.state()['battery_timer_mode'],'no_discharge')
        self.assertEqual(self.c.state()['battery_timer_until'],deadline)
        self.assertEqual(self.r[109],0)

    def test_weather_outage_continues_zero_solar_and_replans_when_soc_changes(self):
        original=self.context
        def fallback_context():
            ctx=original();ctx['weather_updated']=self.now.timestamp()-10000;ctx['weather_error']='timeout'
            ctx['optimization'].update(weather_mode='zero_solar',weather_notice='0 kWh sol')
            ctx['optimization']['hours'][0]['pv_forecast_kwh']=0
            return ctx
        self.c.context=fallback_context;self.action='hold';self.c.enable(True)
        self.advance(4*3600);self.c.tick()
        self.assertEqual(self.c.state()['status'],'active')
        self.assertEqual(self.c.state()['weather_notice'],'0 kWh sol')
        self.r[588]=26;self.action='grid_charge';self.c.tick()
        self.assertEqual(self.c.state()['command']['action'],'grid_charge')
        self.assertGreater(self.r[128],0)
        self.c.context=original;self.r[590]=65536-4000;self.c.tick()
        self.assertEqual(self.c.state()['weather_notice'],'')

    def test_stale_weather_never_credits_old_solar_and_bad_prices_still_block(self):
        original=self.context
        def invalid_context():
            ctx=original();ctx['weather_updated']=0;ctx['weather_error']='timeout'
            ctx['optimization'].update(weather_mode='zero_solar')
            ctx['optimization']['hours'][0]['pv_forecast_kwh']=1
            return ctx
        self.c.context=invalid_context
        with self.assertRaises(ValueError):self.c.enable(True)
        self.assertEqual(self.writes,[])
        self.c.context=original;self.badprice=True
        with self.assertRaises(ValueError):self.c.enable(True)

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
        self.c.enable(True)
        self.assertEqual(self.c.state()['status'],'active')
        self.assertEqual(sum(a==148 for a,v in self.writes),1)

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
        self.r[590]=65536-12500;self.c.tick();self.verify_restore();self.assertEqual(self.c.state()['status'],'fault')

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

    def test_night_ev_blocks_discharge_and_releases_to_fresh_plan(self):
        original=self.context
        night=[True]
        def context():
            ctx=original();ctx['night_ev_charging']=night[0];ctx['tesla_power_kw']=11
            return ctx
        self.c.context=context;self.action='use_battery';self.r[588]=80
        self.c.enable(True);self.assertEqual(self.r[109],0)
        self.assertIn('Natlig bilopladning',self.c.state()['command']['reason'])
        night[0]=False;self.c.tick();self.assertGreater(self.r[109],0)

    def test_normal_grid_target_never_exceeds_90(self):
        self.plan['target_soc']=99
        original=self.context
        def context():
            ctx=original();ctx['optimization']['hours'][0].update(charge_target_soc=99,projected_soc=99)
            return ctx
        self.c.context=context;self.c.enable(True)
        self.assertEqual(self.r[166],90)

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
    def test_history_uses_measured_energy_even_when_last_plan_is_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans=mod.DayPlans(Path(tmp)/'days.json');start=Date(2026,11,11,18,tzinfo=mod.TZ)
            samples=[dict(created=(start+timedelta(minutes=i)).isoformat(),soc=55-i/60*6,battery_w=2000,grid_w=100) for i in range(61)]
            row=dict(time=start.isoformat(),action='hold',price_dkk_kwh=5.36,projected_soc=49,reserve_soc=42)
            events=[dict(kind='command_confirmed',at=start.isoformat(),command=dict(time=start.isoformat(),action='use_battery')),dict(kind='command_confirmed',at=(start+timedelta(minutes=59)).isoformat(),command=dict(time=start.isoformat(),action='hold'))]
            view=plans.view({'hours':[row]},samples,events,now=start+timedelta(hours=1))
            hour=view['days'][0]['hours'][18]
            self.assertTrue(hour['past']);self.assertEqual(hour['action'],'hold')
            self.assertEqual(hour['actual_energy']['battery_out_kwh'],2)
            self.assertEqual(hour['actual_energy']['grid_import_kwh'],.1)
            self.assertEqual(hour['actual_coverage_seconds'],3600)
            self.assertEqual(hour['confirmed_actions'],['hold','use_battery'])

    def test_energy_gap_is_not_invented_and_charge_discharge_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans=mod.DayPlans(Path(tmp)/'days.json');start=Date(2026,11,11,18,tzinfo=mod.TZ)
            samples=[dict(created=start.isoformat(),soc=55,battery_w=-2000,grid_w=100),dict(created=(start+timedelta(minutes=1)).isoformat(),soc=55,battery_w=2000,grid_w=100),dict(created=(start+timedelta(minutes=10)).isoformat(),soc=55,battery_w=2000,grid_w=100)]
            h=plans.view({'hours':[]},samples,now=start+timedelta(hours=1))['days'][0]['hours'][18]
            self.assertEqual(h['actual_coverage_seconds'],60)
            self.assertAlmostEqual(h['actual_energy']['battery_out_kwh'],.008,places=3)
            self.assertEqual(h['actual_energy']['battery_out_kwh'],h['actual_energy']['battery_in_kwh'])

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
