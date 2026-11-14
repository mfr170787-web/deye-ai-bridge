import copy
import hashlib
import json
import re
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent.parent/'deye_ai_bridge'))
from maintenance import Maintenance,atomic_json,digest
from local_updates import LocalUpdates,PREFIX
from update_frontdoor import frontdoor_handler
from image_recovery import ADDRESSES,STALE_FAULT


class LocalUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.data=root/'data';self.data.mkdir();self.base=root/'base';self.base.mkdir()
        (self.base/'app.py').write_text('APP_VERSION="old"\n');(self.base/'index.html').write_text('old')
        self.manager=Maintenance(self.data,self.base)
        self.state=dict(status='disabled',restored=True,error=None,pending=None,auto_resume=False)
        atomic_json(self.data/'automatic_control.json',self.state)
        self.time=[1000.];self.local=LocalUpdates(self.manager,lambda:self.time[0])
        self.package=dict(format=1,action='install',base_revision=self.manager.revision(),
            files={'app.py':'APP_VERSION="new"\n'},target_release=None,restart=True,rollback_on_failure=True)
        self.nonce=self.local.page('__LOCAL_PAGE_NONCE__').decode()

    def tearDown(self):
        for p in self.data.rglob('*'):
            if p.is_dir():p.chmod(0o700)

    def review(self,package=None):return self.local.preview({'package':package or self.package},self.nonce)
    def install(self,review,package=None):
        return self.local.install(dict(package=package or self.package,review_ticket=review['review_ticket'],acknowledge=True),self.nonce)

    def test_stage_and_preview_never_install_or_issue_a_grant(self):
        result=self.local.submit({'package':self.package,'summary':'Test update'})
        self.assertFalse(result['installed']);self.assertFalse(result['restart_performed'])
        self.assertEqual(self.local.listing()['proposals'][0]['proposal_id'],digest(self.package))
        review=self.review();self.assertNotIn(self.manager.secret,json.dumps(review))
        self.assertFalse((self.manager.private/'grant.json').exists());self.assertEqual(self.manager.active(),'base')

    def test_exact_local_click_installs_without_password_and_consumes_ticket(self):
        review=self.review();result=self.install(review)
        self.assertEqual(result['package_sha256'],digest(self.package))
        self.assertFalse((self.manager.private/'grant.json').exists())
        self.assertEqual(self.manager.active(),digest(self.package))
        self.manager.finish_restart(True)
        self.assertEqual(self.local.status()['app_version'],'new')
        with self.assertRaises(PermissionError):self.install(review)

    def test_missing_or_expired_page_and_ticket_are_rejected(self):
        with self.assertRaises(PermissionError):self.local.preview({'package':self.package},'')
        review=self.review();self.time[0]+=901
        with self.assertRaises(PermissionError):self.install(review)
        self.time[0]+=1000
        with self.assertRaises(PermissionError):self.review()
        self.assertEqual(self.manager.active(),'base')

    def test_ticket_does_not_work_for_another_page_or_modified_package(self):
        review=self.review();nonce2=self.local.page('__LOCAL_PAGE_NONCE__').decode()
        with self.assertRaises(PermissionError):
            self.local.install(dict(package=self.package,review_ticket=review['review_ticket'],acknowledge=True),nonce2)
        review=self.review();modified=copy.deepcopy(self.package);modified['files']['app.py']='APP_VERSION="evil"'
        with self.assertRaises(PermissionError):self.install(review,modified)
        self.assertEqual(self.manager.active(),'base')

    def test_active_or_faulted_automation_blocks_install_but_not_preview(self):
        for changes in ({'status':'active'},{'status':'fault','error':'fault'},{'restored':False},{'pending':{'108':20}}):
            atomic_json(self.data/'automatic_control.json',{**self.state,**changes})
            review=self.review()
            with self.assertRaises(ValueError):self.install(review)
            self.assertEqual(self.manager.active(),'base')

    def test_stale_review_cannot_install(self):
        review=self.review();(self.base/'index.html').write_text('changed')
        with self.assertRaises(PermissionError):self.install(review)

    def test_manual_package_path_needs_no_staged_file(self):
        self.assertEqual(self.local.listing()['proposals'],[])
        self.install(self.review());self.manager.finish_restart(False)
        self.assertEqual(self.manager.active(),'base')
        self.assertEqual(self.local.status()['operation']['status'],'startup_failed_rolled_back')

    def test_invalid_files_and_unacknowledged_actions_are_rejected(self):
        p=copy.deepcopy(self.package);p['files']={'../maintenance.py':'bad'}
        with self.assertRaises(ValueError):self.review(p)
        review=self.review()
        with self.assertRaises(ValueError):self.local.install(dict(package=self.package,review_ticket=review['review_ticket'],acknowledge=False),self.nonce)

    def test_root_ui_and_upload_work_without_running_child(self):
        template=Path(__file__).parent.parent/'deye_ai_bridge/local_updates.html'
        server=ThreadingHTTPServer(('127.0.0.1',0),frontdoor_handler(self.local,template))
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url='http://127.0.0.1:'+str(server.server_address[1])+PREFIX
        with urllib.request.urlopen(url+'/ui') as response:html=response.read().decode()
        self.assertIn('type="file"',html);self.assertNotIn('type="password"',html)
        self.assertNotIn(self.manager.secret,html)
        nonce=re.search("const PAGE_NONCE='([^']+)'",html).group(1)
        def post(path,body,headers=None):
            req=urllib.request.Request(url+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json',**(headers or {})})
            with urllib.request.urlopen(req) as response:return json.load(response)
        with self.assertRaises(urllib.error.HTTPError) as e:post('/local-preview',{'package':self.package})
        self.assertEqual(e.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError) as e:post('/local-preview',{'package':self.package},{'X-Deye-Local-Page':nonce,'Sec-Fetch-Site':'cross-site'})
        self.assertEqual(e.exception.code,403)
        review=post('/local-preview',{'package':self.package},{'X-Deye-Local-Page':nonce})
        result=post('/local-install',dict(package=self.package,review_ticket=review['review_ticket'],acknowledge=True),{'X-Deye-Local-Page':nonce})
        self.assertEqual(result['status'],'restart_pending')
        self.assertEqual(self.manager.active(),digest(self.package))

    def test_fault_acknowledgement_requires_fresh_matching_baseline(self):
        baseline={a:10 for a in ADDRESSES}
        state=dict(status='fault',restored=True,error=STALE_FAULT,pending=None,auto_resume=False,
                   endpoint=['test-host',502,1],baseline=baseline,events=[])
        path=self.data/'automatic_control.json';atomic_json(path,state)
        atomic_json(self.data/'options.json',dict(deye_host='test-host',deye_port=502,slave_id=1))
        raw=path.read_bytes();body=dict(state_sha256=hashlib.sha256(raw).hexdigest(),acknowledge=True)
        def wrong(e,t,s,c):return {str(a):99 for a in range(s,s+c)}
        with self.assertRaisesRegex(ValueError,'afviger'):
            self.local.acknowledge_fault(body,self.nonce,wrong)
        self.assertEqual(path.read_bytes(),raw)
        calls=[]
        def right(e,t,s,c):calls.append((s,c));return {str(a):10 for a in range(s,s+c)}
        with patch('os.chown'):
            result=self.local.acknowledge_fault(body,self.nonce,right)
        self.assertEqual(len(calls),12);self.assertEqual(result['inverter_writes'],0)
        final=json.loads(path.read_text());self.assertEqual(final['status'],'disabled')
        self.assertEqual(final['events'][-1]['previous_error'],STALE_FAULT)
        self.assertEqual(len(list(self.manager.private.glob('fault-ack-*.json'))),1)
        with self.assertRaises(ValueError):self.local.acknowledge_fault(body,self.nonce,right)

    def test_flow_requires_exact_local_review_and_cloud_confirmation(self):
        self.local.flow.request=lambda action=None:dict(status='active',restored=False)
        review=self.review()
        body=dict(package=self.package,review_ticket=review['review_ticket'],acknowledge=True,cloud_off=True)
        with self.assertRaises(PermissionError):self.local.flow.start(body,'invalid')
        with self.assertRaises(ValueError):self.local.flow.start({**body,'cloud_off':False},self.nonce)
        with patch('update_flow.threading.Thread') as worker:
            result=self.local.flow.start(body,self.nonce)
            self.assertEqual(result['phase'],'accepted')
            worker.return_value.start.assert_called_once()
        self.assertEqual(self.manager.active(),'base')
        self.assertFalse((self.manager.private/'grant.json').exists())
        with self.assertRaises(ValueError):self.local.flow.start(body,self.nonce)

    def test_flow_rejects_changed_package_without_any_installation(self):
        review=self.review();changed=copy.deepcopy(self.package);changed['files']['app.py']='changed'
        body=dict(package=changed,review_ticket=review['review_ticket'],acknowledge=True,cloud_off=True)
        with self.assertRaises(PermissionError):self.local.flow.start(body,self.nonce)
        self.assertEqual(self.manager.active(),'base')


if __name__=='__main__':unittest.main()
