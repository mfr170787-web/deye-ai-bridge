import ast
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

class VictronTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text())
        nodes=[n for n in tree.body if (isinstance(n,ast.FunctionDef) and n.name.startswith('victron_')) or (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id.startswith('VICTRON_') for t in n.targets))]
        self.ha=Mock()
        self.ns={'threading':threading,'ha_request':self.ha,'DATA_DIR':Path(self.tmp.name),'json':json}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'victron','exec'),self.ns)
        self.states=[{'entity_id':domain+'.victron_demo_'+suffix,'state':'off' if key=='charging' else '14','attributes':{}} for key,(domain,suffix) in self.ns['VICTRON_FIELDS'].items()]
        self.ha.return_value=self.states
    def select(self):
        self.ns['victron_action']({'action':'select','id':'switch.victron_demo_charging'})
    def test_read_does_not_write_or_select(self):
        state=self.ns['victron_state']()
        self.assertEqual(len(state['candidates']),1);self.assertIsNone(state['selected'])
        self.ha.assert_called_once_with('/states')
    def test_selection_is_local_and_no_command(self):
        self.select();self.ha.assert_called_once_with('/states')
        self.assertEqual(len(self.ns['victron_state']()['fields']),12)
    def test_exact_manual_service_and_validation(self):
        with self.assertRaises(ValueError):self.ns['victron_action']({'action':'on'})
        self.select()
        def ha(path,*args):
            if path=='/states':return self.states
            return {'state':'off'}
        self.ha.side_effect=ha
        self.ns['victron_action']({'action':'on'})
        self.assertEqual(self.ha.call_args.args,('/services/switch/turn_on','POST',{'entity_id':'switch.victron_demo_charging'}))
        for value in (0,33,True,14.5,'16'):
            with self.assertRaises(ValueError):self.ns['victron_action']({'action':'current','value':value})
        self.ha.side_effect=lambda path,*args:self.states if path=='/states' else {'state':'unavailable'}
        with self.assertRaises(ValueError):self.ns['victron_action']({'action':'off'})
    def test_reject_arbitrary_entities(self):
        with self.assertRaises(ValueError):self.ns['victron_action']({'action':'select','id':'switch.other'})
