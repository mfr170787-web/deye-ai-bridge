import ast
import io
import json
import sys
import unittest
from datetime import datetime as Date,timedelta,timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shadow_planning import reserve,limits,economic_dispatch,power_limited_plan

SOURCE=(Path(__file__).resolve().parents[1]/'app.py').read_text()
TREE=ast.parse(SOURCE)

class DispatchTests(unittest.TestCase):
    def setUp(self):
        h=next(x for x in TREE.body if isinstance(x,ast.ClassDef) and x.name=='Handler')
        self.auto=SimpleNamespace(enable=Mock(return_value={'status':'active'}),disable=Mock(return_value={'status':'verifying_restore'}))
        ns=dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler,json=json,urlparse=urlparse,AUTO_CONTROL=self.auto)
        exec(compile(ast.Module(body=[h],type_ignores=[]),'handler','exec'),ns);self.cls=ns['Handler']
    def post(self,body,header=True,path='/api/automation'):
        h=object.__new__(self.cls);payload=json.dumps(body).encode();h.headers={'Content-Length':str(len(payload))}
        if header:h.headers['X-Deye-Control']='1'
        h.path=path;h.rfile=io.BytesIO(payload);h.send_json=Mock();h.do_POST();return h.send_json.call_args.args
    def test_activation_header_ack_and_deactivation(self):
        self.assertEqual(self.post({'action':'enable'},False)[1],403);self.auto.enable.assert_not_called()
        self.post({'action':'enable','cloud_off':True});self.auto.enable.assert_called_once_with(True)
        self.post({'action':'disable'});self.auto.disable.assert_called_once_with()
    def test_retired_start_and_unknown_action(self):
        for path in ('/api/charge20','/api/test-suite','/api/charge-test'):
            self.assertEqual(self.post({'action':'start'},path=path)[1],400)
        self.assertEqual(self.post({'action':'write','address':212,'value':999})[1],400)
        self.auto.enable.assert_not_called();self.auto.disable.assert_not_called()

class ModelTests(unittest.TestCase):
    def model(self,prices,loads,pvs,soc):
        now=Date(2026,11,7,10,tzinfo=ZoneInfo('Europe/Copenhagen'))
        class Clock(Date):
            @classmethod
            def now(cls,tz=None):return now
        slots=[dict(time=now+timedelta(hours=i),price=p,load=loads[i],pv=pvs[i],fraction=1.) for i,p in enumerate(prices)]
        rows=[dict(time=x['time'].isoformat(),price_dkk_kwh=x['price']) for x in slots]
        cfg=dict(battery_capacity_kwh=32,roundtrip_efficiency_percent=90,target_soc=80,minimum_soc_morning=20,max_charge_w=4500,max_discharge_w=4500,reserve_margin_percent=15)
        ns=dict(datetime=Clock,ZoneInfo=ZoneInfo,forecast_slots=lambda *a:(slots,100),energy_plan=lambda:cfg,limits=limits,reserve=reserve,economic_dispatch=economic_dispatch,power_limited_plan=power_limited_plan)
        f=next(x for x in TREE.body if isinstance(x,ast.FunctionDef) and x.name=='optimization_plan')
        exec(compile(ast.Module(body=[f],type_ignores=[]),'model','exec'),ns)
        return ns['optimization_plan']({'soc':soc},rows,{})
    def test_reserve_preserved_for_most_expensive_hour(self):
        m=self.model([2,4,5,1],[5,5,5,5],[0,0,0,0],25)
        self.assertEqual(m['hours'][0]['action'],'grid_charge')
        self.assertEqual(m['hours'][2]['action'],'use_battery')
        self.assertGreater(m['hours'][0]['charge_target_soc'],25)
    def test_cheap_charging_and_forecast_solar_space(self):
        m=self.model([1,4,5,2],[5,5,5,5],[0,0,0,0],15)
        self.assertEqual(m['hours'][0]['action'],'grid_charge');self.assertLessEqual(abs(m['hours'][0]['battery_power_w']),4500)
        m=self.model([1,4,5,2],[1,1,1,1],[0,10,10,0],50)
        self.assertNotEqual(m['hours'][0]['action'],'grid_charge');self.assertIn('solar_charge',[r['action'] for r in m['hours'][1:3]])

if __name__=='__main__':unittest.main()


class ReadRetryTests(unittest.TestCase):
    def run_read(self, failures):
        once=Mock(side_effect=failures);sleep=Mock()
        ns=dict(read_holding_once=once,time=SimpleNamespace(sleep=sleep))
        f=next(x for x in TREE.body if isinstance(x,ast.FunctionDef) and x.name=='read_holding')
        exec(compile(ast.Module(body=[f],type_ignores=[]),'retry','exec'),ns)
        return ns['read_holding'],once,sleep

    def network_error(self):
        error=RuntimeError('Modbus read timeout');error.__cause__=TimeoutError('timeout');return error

    def test_temporary_network_failure_retries_read(self):
        read,once,sleep=self.run_read([self.network_error(),self.network_error(),[42]])
        self.assertEqual(read(108,1),[42]);self.assertEqual(once.call_count,3)

    def test_permanent_failure_is_bounded_and_protocol_is_not_retried(self):
        read,once,_=self.run_read([self.network_error()]*3)
        with self.assertRaises(RuntimeError):read(108,1)
        self.assertEqual(once.call_count,3)
        read,once,_=self.run_read([RuntimeError('Uventet Modbus-svar')])
        with self.assertRaises(RuntimeError):read(108,1)
        self.assertEqual(once.call_count,1)
