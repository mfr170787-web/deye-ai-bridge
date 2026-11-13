import ast,json,math,tempfile,threading,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
class CostTests(unittest.TestCase):
    def test_unknown_then_grid_and_solar_weighted_cost(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text())
        nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='battery_cost']
        with tempfile.TemporaryDirectory() as d:
            ns=dict(DATA_DIR=Path(d),BATTERY_COST_LOCK=threading.Lock(),json=json,math=math,datetime=datetime)
            exec(compile(ast.Module(body=nodes,type_ignores=[]),'cost','exec'),ns)
            f=ns['battery_cost'];plan=dict(battery_capacity_kwh=32,roundtrip_efficiency_percent=90)
            t=datetime(2026,11,13,tzinfo=timezone.utc)
            def sample(i,**kw):return dict(created=(t+timedelta(seconds=60*i)).isoformat(),soc=20+i*.1,battery_w=-2000,pv_w=0,load_w=1000,grid_w=3000,price_dkk=2,**kw)
            r=f(sample(0),plan);self.assertEqual(r['known_cost_dkk_kwh'],None);self.assertEqual(r['unknown_kwh'],6.4)
            r=f(sample(1),plan);self.assertGreater(r['known_cost_dkk_kwh'],2)
            self.assertGreater(r['unknown_kwh'],0)
            before=Path(d,'battery_cost.json').read_text();f(sample(1),plan)
            self.assertEqual(before,Path(d,'battery_cost.json').read_text())
    def test_night_ev_blocks_only_reading_and_uses_fresh_data(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text());f=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='night_ev_charging')
        t=datetime(2026,11,13,2,tzinfo=timezone.utc)
        ns=dict(datetime=datetime,victron_state=lambda:{'fields':{'total_power':{'state':'7000','unit':'W','updated':t.isoformat()}}},validated_tesla_power=lambda s:0)
        exec(compile(ast.Module(body=[f],type_ignores=[]),'night','exec'),ns)
        self.assertTrue(ns['night_ev_charging']({},t))
        self.assertFalse(ns['night_ev_charging']({},t.replace(hour=12)))
