import ast
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
import re


class ControlAlertsTests(unittest.TestCase):
    def test_only_terminal_fault_notifies_once(self):
        source=Path(__file__).resolve().parents[1]/'app.py'
        tree=ast.parse(source.read_text())
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='maybe_notify_control_fault')
        with tempfile.TemporaryDirectory() as directory:
            request=Mock()
            scope=dict(DATA_DIR=Path(directory),json=json,time=time,re=re,datetime=datetime,timezone=timezone,
                       energy_plan=lambda:{'tesla_notify_service':'mobile_app_test'},ha_request=request,addon_slug=lambda:'bridge')
            exec(compile(ast.Module(body=[function],type_ignores=[]),str(source),'exec'),scope)
            state=dict(started='test',stop_reason='fault: Modbus read, ConnectionError',error='connection lost')
            for status,resume in [('restoring',True),('verifying_restore',True),('restore_failed',True),('fault',True),('active',False)]:
                state.update(status=status,auto_resume=resume)
                (Path(directory)/'automatic_control.json').write_text(json.dumps(state))
                scope['maybe_notify_control_fault']()
            request.assert_not_called()
            state.update(status='fault',auto_resume=False,restored=True)
            (Path(directory)/'automatic_control.json').write_text(json.dumps(state))
            scope['maybe_notify_control_fault']()
            scope['maybe_notify_control_fault']()
            pushes=[c for c in request.call_args_list if c.args[0].startswith('/services/notify/')]
            self.assertEqual(len(pushes),1)
            self.assertIn('kunne ikke genoptages',pushes[0].args[2]['message'])
