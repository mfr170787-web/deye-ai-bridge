import ast,json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import Mock
class LocalTeslaTests(unittest.TestCase):
    def test_local_selection_reading_and_no_vehicle_commands(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text())
        names={'tesla_candidates','tesla_selection','tesla_devices','select_tesla','tesla_state'}
        nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names or isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in {'TESLA_ENTITIES','TESLA_FIELDS','TESLA_SELECTION','TESLA_SELECTION_LOCK'} for t in n.targets)]
        with tempfile.TemporaryDirectory() as d:
            ha=Mock();ns=dict(DATA_DIR=Path(d),threading=threading,json=json,ha_request=ha)
            exec(compile(ast.Module(body=nodes,type_ignores=[]),'tesla','exec'),ns)
            ha.return_value=[{'entity_id':domain+'.demo_'+suffix,'state':'50','attributes':{}} for domain,suffix in ns['TESLA_FIELDS'].values()]
            self.assertFalse(ns['tesla_state']()['available'])
            with self.assertRaises(ValueError):ns['select_tesla']({'id':'sensor.other'})
            ns['select_tesla']({'id':'sensor.demo_battery_level'})
            self.assertTrue(ns['tesla_state']()['available'])
            self.assertEqual(ns['tesla_state']()['tesla_soc'],50)
            self.assertEqual(json.loads(Path(d,'tesla_selection.json').read_text())['id'],'sensor.demo_battery_level')
            self.assertTrue(all(c.args==('/states',) for c in ha.call_args_list))
