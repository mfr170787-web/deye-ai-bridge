import sys,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).parent.parent/'deye_ai_bridge'))
from update_flow import UpdateFlow
class Tests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.t=0;self.calls=[];self.rollback=False
  self.s=dict(status='active',restored=False,live_sample_age_seconds=0,live_sample={'created':'0'})
  self.m=SimpleNamespace(private=Path(t.name),lock=threading.RLock(),secret='test',revision=lambda:'base',grant=lambda *a:self.calls.append('grant'),apply=lambda *a:self.calls.append('install'),status=lambda:dict(active_release='sha',operation=dict(package_sha256='sha',target='sha',status='startup_failed_rolled_back' if self.rollback else 'installed')))
  self.l=SimpleNamespace(manager=self.m);self.f=UpdateFlow(self.l,self.request,lambda:self.t,self.wait);self.f.value=dict(package_sha256='sha',was_active=True,resume_cancelled=False,phase="accepted")
 def wait(self,n):self.t+=n;self.s['live_sample']={'created':str(self.t)}
 def request(self,action=None):
  if action:self.calls.append(action);self.s.update(status='disabled' if action=='disable' else 'active',restored=action=='disable')
  return dict(self.s)
 def run_flow(self):self.f.run({'base_revision':'base'})
 def test_order(self):
  self.run_flow();self.assertEqual(self.calls,['disable','grant','install','enable']);self.assertEqual(self.f.status()['phase'],'complete');self.assertGreaterEqual(self.t,125)
 def test_disabled(self):
  self.s.update(status='disabled',restored=True);self.f.value['was_active']=False;self.run_flow();self.assertEqual(self.calls,['grant','install'])
 def test_restore_failure(self):
  self.f.request=lambda action=None:dict(status='fault',restored=False);self.run_flow();self.assertEqual(self.calls,[]);self.assertEqual(self.f.status()['phase'],'failed')
 def test_rollback(self):
  self.rollback=True;self.run_flow();self.assertNotIn('enable',self.calls)
 def test_stale(self):
  self.s['live_sample_age_seconds']=121;self.run_flow();self.assertNotIn('enable',self.calls)
 def test_cancel(self):
  self.f.cancel_resume();self.run_flow();self.assertNotIn('enable',self.calls)
 def test_interrupted(self):
  self.f.record('resuming');f=UpdateFlow(self.l,self.request);self.assertEqual(f.status()['phase'],'interrupted');self.assertEqual(self.calls,[])
 def test_lost_stop_response(self):
  def request(action=None):
   if action:self.calls.append(action);raise TimeoutError('lost')
   return self.s
  self.f.request=request;self.run_flow();self.assertEqual(self.calls,['disable'])
 def test_lost_enable_response_stops(self):
  def request(action=None):
   if action=='enable':self.calls.append(action);raise TimeoutError('lost')
   return self.request(action)
  self.f.request=request;self.run_flow();self.assertEqual(self.calls[-2:],['enable','disable'])
 def test_stale_after_resume_stops(self):
  def request(action=None):
   s=self.request(action)
   if self.t>=4:s['live_sample_age_seconds']=200
   return s
  self.f.request=request;self.run_flow();self.assertEqual(self.calls[-1],'disable');self.assertEqual(self.f.status()['phase'],'failed')
 def test_same_sample_rejected(self):
  self.f.wait=lambda n:setattr(self,'t',self.t+n);self.run_flow();self.assertNotIn('enable',self.calls);self.assertEqual(self.f.status()['phase'],'failed')
if __name__=='__main__':unittest.main()
