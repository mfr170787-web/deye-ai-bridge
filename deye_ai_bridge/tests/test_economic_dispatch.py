import sys
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shadow_planning import economic_dispatch,power_limited_plan


class EconomicDispatchTests(unittest.TestCase):
    plan=dict(battery_capacity_kwh=32,roundtrip_efficiency_percent=90,
              minimum_soc_morning=20,target_soc=99,max_charge_w=4500,
              max_discharge_w=4500,reserve_margin_percent=15)

    def slots(self,prices,loads=None,pvs=None):
        now=datetime(2026,11,9,tzinfo=ZoneInfo('Europe/Copenhagen'))
        return [dict(time=now+timedelta(hours=i),price=p,load=(loads or [1]*len(prices))[i],
                     pv=(pvs or [0]*len(prices))[i],fraction=1.) for i,p in enumerate(prices)]

    def test_real_price_pattern_buys_at_night_not_midday(self):
        prices=[2.5735,2.5335,2.5235,2.5035,2.5035,2.5835,3.3037,4.1238,4.3737,4.1238,3.8637,3.6537,
                3.6337,3.6337,3.8337,4.8037,5.5437,7.2817,6.5717,5.7417,4.8017,3.6537,3.4437,3.0037]
        rows=economic_dispatch(self.slots(prices),self.plan,20)
        self.assertTrue(any(r['action']=='grid_charge' for r in rows[:6]))
        self.assertFalse(any(r['action']=='grid_charge' for r in rows[6:]))
        self.assertTrue(all(r['action']=='use_battery' for r in rows[17:20]))
        self.assertTrue(all(r['projected_soc']>=20 for r in rows))

    def test_limited_capacity_saves_for_dearest_hour(self):
        plan={**self.plan,'battery_capacity_kwh':4,'target_soc':70,'max_charge_w':1000}
        rows=economic_dispatch(self.slots([2,3.5,7],[1,1,1]),plan,70)
        self.assertGreater(rows[1]['projected_soc'],20)
        self.assertEqual(rows[2]['action'],'use_battery')
        self.assertLess(rows[2]['projected_soc'],rows[1]['projected_soc'])

    def test_energy_conservation_and_partial_hour_power_limits(self):
        slots=self.slots([1,7,2,8],[1,4,1,4]);slots[0]['fraction']=.1
        rows=economic_dispatch(slots,self.plan,20);before=32*.2;eta=.9**.5
        for slot,row in zip(slots,rows):
            after=row['projected_soc']/100*32
            delta=after-before
            self.assertLessEqual(delta,max(0,4500/1000*slot['fraction']*eta)+.04)
            self.assertLessEqual(-delta,4500/1000*slot['fraction']/eta+.04)
            self.assertLessEqual(-row['battery_power_w'],4500+1)
            before=after

    def test_large_solar_forecast_avoids_unneeded_night_charge(self):
        rows=economic_dispatch(self.slots([1,4,7],[1,1,1],[0,10,0]),self.plan,50)
        self.assertNotEqual(rows[0]['action'],'grid_charge')
        self.assertEqual(rows[1]['action'],'solar_charge')

    def test_missing_price_gap_is_not_assumed_free(self):
        slots=self.slots([2,7,1]);slots[2]['time']+=timedelta(hours=1)
        self.assertEqual(len(economic_dispatch(slots,self.plan,20)),2)

    def test_forecast_respects_actual_bms_and_current_ceiling(self):
        import json
        sample={'telemetry_json':json.dumps({'raw_registers':{'587':5200,'212':120,'213':190}})}
        plan=power_limited_plan({**self.plan,'max_charge_w':9000,'max_discharge_w':9000},sample)
        self.assertEqual(plan['max_charge_w'],6240)
        self.assertEqual(plan['max_discharge_w'],9000)
        sample['telemetry_json']=json.dumps({'raw_registers':{'587':5200,'212':190,'213':80}})
        self.assertEqual(power_limited_plan(plan,sample)['max_discharge_w'],4160)

class WeatherFallbackTests(unittest.TestCase):
    def test_stale_failed_missing_forecasts_use_zero_solar(self):
        from shadow_planning import planning_weather
        from datetime import datetime,timezone
        now=datetime(2026,11,12,4,tzinfo=timezone.utc)
        weather={'hourly':{'time':['2026-11-12T12:00:00+00:00'],'global_tilted_irradiance':[500]}}
        for updated,error in [(now.timestamp()-7201,None),(now.timestamp(),'timeout'),(0,None)]:
            safe,info=planning_weather(weather,updated,error,now)
            self.assertEqual(safe,{});self.assertEqual(info['weather_mode'],'zero_solar')
        safe,info=planning_weather(weather,now.timestamp(),None,now)
        self.assertEqual(safe,weather);self.assertEqual(info['weather_mode'],'forecast')
        safe,info=planning_weather({},now.timestamp(),None,now)
        self.assertEqual(safe,{})
        self.assertEqual(info['weather_mode'],'zero_solar')
