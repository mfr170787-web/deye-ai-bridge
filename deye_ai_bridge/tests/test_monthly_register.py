import io
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from monthly_register import MonthlyRegister, bounds, excel

class RegisterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.r=MonthlyRegister(Path(self.tmp.name)/'register.db')
    def point(self,t,counter):
        return {'created':t.isoformat(),'telemetry_json':json.dumps({'totals_kwh':{'grid_import':counter}}),'price_dkk':2.}
    def test_hour_boundary_conservation_price_components_and_restart(self):
        t=datetime(2026,11,10,6,59,tzinfo=timezone.utc)
        self.r.backfill([self.point(t,100),self.point(t+timedelta(minutes=2),100.2)])
        p={'time':t.replace(minute=0).isoformat(),'source':'Andel FlexEnergi + N1 og afgifter','price_dkk_kwh':2.,'energy_price_dkk_kwh':1.,'grid_tariff_dkk_kwh':.5}
        self.r.ingest(self.point(t+timedelta(minutes=2),100.2),[p])
        result=self.r.report('2026-11');used=[x for x in result['hours'] if x['coverage_seconds']]
        self.assertAlmostEqual(result['kwh'],.2);self.assertEqual(len(used),2)
        self.assertAlmostEqual(used[0]['energy_cost'],.1)
        self.assertIsNone(used[1]['energy_price'])
        self.assertAlmostEqual(MonthlyRegister(self.r.path).report('2026-11')['kwh'],.2)
        self.assertTrue(all('Delvis' in x['status'] for x in used))
    def test_gaps_resets_and_missing_data_are_not_zero_cost_complete_hours(self):
        t=datetime(2026,11,10,8,tzinfo=timezone.utc)
        self.r.backfill([self.point(t,100),self.point(t+timedelta(minutes=10),101),self.point(t+timedelta(minutes=11),0)])
        report=self.r.report('2026-11')
        self.assertEqual(report['kwh'],0);self.assertGreater(report['missing_hours'],0)
        self.assertTrue(all(x['cost'] is None for x in report['hours']))
    def test_dst_hours_and_distinct_offsets(self):
        self.assertEqual(len(self.r.report('2026-10')['hours']),745)
        self.assertEqual(len(self.r.report('2026-03')['hours']),743)
        rows=[r for r in self.r.report('2026-10')['hours'] if r['time'].startswith('2026-10-25T02:')]
        self.assertEqual(len(rows),2);self.assertNotEqual(rows[0]['time'],rows[1]['time'])
    def test_archives_are_automatic_and_immutable(self):
        t=datetime(2026,9,30,10,tzinfo=timezone.utc)
        self.r.backfill([self.point(t,100),self.point(t+timedelta(minutes=1),100.1)])
        self.r.finalize(datetime(2026,10,2,12,tzinfo=timezone.utc))
        saved=self.r.download('2026-09');self.assertTrue((self.r.archive/'2026-09.json').exists())
        self.r.backfill([self.point(t+timedelta(minutes=2),100.2)])
        self.r.finalize();self.assertEqual(self.r.download('2026-09'),saved)
        self.assertTrue(self.r.months()[0]['archived'])
    def test_excel_structure_formulas_and_path_validation(self):
        report=self.r.report('2026-11')
        with zipfile.ZipFile(io.BytesIO(excel(report))) as z:
            for name in z.namelist():ET.fromstring(z.read(name))
            self.assertIn(b'B2*G2',z.read('xl/worksheets/sheet2.xml'))
            self.assertIn(b'SUM(Timer!I2:',z.read('xl/worksheets/sheet1.xml'))
        for month in ('../foo','2026-13','2026-1'):
            with self.assertRaises(ValueError):self.r.download(month)

if __name__=='__main__':unittest.main()
