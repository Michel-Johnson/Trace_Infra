"""Real Caddy + real API, on private ephemeral ports with disposable credentials."""
import base64
import copy
import importlib.util
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid

ROOT=Path(os.environ.get('TRACE_HUNTER_TEST_ROOT',Path(__file__).resolve().parents[1]))
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'apps/api')]
CADDY=Path(os.environ.get('CADDY_BINARY',Path.home()/'apps/trace-hunter-tools/caddy'))
CADDYFILE=Path(os.environ.get('CADDYFILE_UNDER_TEST',ROOT/'deploy/Caddyfile'))


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


@unittest.skipUnless(CADDY.is_file(),'real Caddy executable is unavailable')
class CaddyAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import uvicorn
        from trace_hunter.storage import Store
        from trace_hunter_api.app import create_app
        cls.temp=tempfile.TemporaryDirectory();cls.addClassCleanup(cls.temp.cleanup);folder=Path(cls.temp.name)
        cls.store=Store(folder/'auth.sqlite');cls.addClassCleanup(cls.store.close)
        cls.trace=json.loads((ROOT/'examples/minimal.trace.json').read_text());cls.store.import_trace(cls.trace)
        app=create_app(cls.store);cls.app=app;cls.tasks=app.state.extensions.tasks;cls.evaluations=app.state.evaluations
        cls.api_port=port();cls.web_port=port();cls.base=f'http://127.0.0.1:{cls.web_port}'
        print(f'Isolated Caddy boundary test: {cls.base}; API 127.0.0.1:{cls.api_port}',flush=True)
        cls.api=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=cls.api_port,log_level='error',access_log=False,proxy_headers=False))
        cls.thread=threading.Thread(target=cls.api.run,daemon=True);cls.thread.start()
        def stop_api():
            cls.api.should_exit=True;cls.thread.join(timeout=5)
        cls.addClassCleanup(stop_api)
        for _ in range(100):
            if cls.api.started:break
            time.sleep(.02)
        if not cls.api.started:raise RuntimeError('API did not start')
        manifest=copy.deepcopy(json.loads((ROOT/'plugins/extensions/call-activity/manifest.json').read_text()))
        manifest['plugin_id']='test.caddy-agent';manifest['contributes']=[c for c in manifest['contributes'] if c['id']=='classify']
        manifest['contributes'][0]['implementation']['host']='remote_agent';app.state.extensions.register(manifest)
        cls.username='test-'+uuid.uuid4().hex;cls.password=uuid.uuid4().hex
        encoded=base64.b64encode((cls.username+':'+cls.password).encode()).decode();cls.basic='Basic '+encoded
        hashed=subprocess.run([str(CADDY),'hash-password','--algorithm','bcrypt','--bcrypt-cost','4'],input=(cls.password+'\n').encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True).stdout.decode().strip()
        auth=folder/'access.caddy';auth.write_text('basic_auth {\n    '+cls.username+' '+hashed+'\n}\n');auth.chmod(0o600)
        (folder/'index.html').write_text('private-trace-test')
        cls.env={**os.environ,'SITE_ADDRESS':cls.base,'WEB_HOST':'127.0.0.1','WEB_ROOT':str(folder),'API_UPSTREAM':f'127.0.0.1:{cls.api_port}','TRACE_HUNTER_AUTH_CONFIG':str(auth)}
        cls.log=(folder/'caddy.log').open('wb');cls.addClassCleanup(cls.log.close)
        cls.process=subprocess.Popen([str(CADDY),'run','--config',str(CADDYFILE),'--adapter','caddyfile'],env=cls.env,stdout=cls.log,stderr=cls.log)
        def stop_caddy():
            cls.process.terminate();cls.process.wait(timeout=5)
        cls.addClassCleanup(stop_caddy)
        cls.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(100):
            if cls.process.poll() is not None:raise RuntimeError('Caddy failed to start')
            try:
                if cls.fetch('/')[0]==401:break
            except OSError:pass
            time.sleep(.02)
        else:raise RuntimeError('Caddy access control did not start')

    @classmethod
    def fetch(cls,path,method='GET',authorization=None,body=None):
        headers={'Authorization':authorization} if authorization else {}
        raw=json.dumps(body).encode() if body is not None else None
        if raw is not None:headers['Content-Type']='application/json'
        request=urllib.request.Request(cls.base+path,data=raw,headers=headers,method=method)
        try:
            with cls.opener.open(request,timeout=5) as response:return response.status,response.read()
        except urllib.error.HTTPError as error:return error.code,error.read()

    def create(self):
        return self.tasks.create({'plugin_id':'test.caddy-agent','plugin_version':'1.0.0','contribution_id':'classify',
            'scope':'task','config':{},'collection_id':None,'run_ids':[self.trace['run']['id']],'request_key':uuid.uuid4().hex})['jobs'][0]

    def service_credential(self):
        status,raw=self.fetch('/api/v1/projects/default/principals','POST',self.basic,
                              {'name':'gateway-service','scopes':['traces:read']})
        self.assertEqual(status,201,raw)
        principal=json.loads(raw)
        status,raw=self.fetch('/api/v1/principals/'+principal['principal_id']+'/credentials','POST',self.basic,
                              {'ttl_seconds':3600})
        self.assertEqual(status,201,raw)
        return json.loads(raw)

    def test_native_task_credentials_cross_gateway_without_project_or_operator_access(self):
        from trace_hunter.access import Principal
        from test_invocations import definition
        from test_runtimes import advertised, config, Reader
        operation=self.app.state.invocations.register_operation('default',definition())['operation']['ref']
        revision=self.store.revisions.get('default',self.trace['run']['id'],1)
        ref={'kind':'trace_revision','id':revision['run_id'],'revision':1,'digest':revision['content']['digest']}
        invocation=self.app.state.invocations.create('default',{'operation':operation,'inputs':[{'role':'source','ref':ref}],
            'config':{'threshold':0}},request_key='native-task')['invocation']
        runtime=self.app.state.runtimes.append('default','native-reader',config(),expected_previous=0,request_key='native-runtime')['binding']['ref']
        self.app.state.runtimes.reader=Reader(advertised(operation))
        self.app.state.runtimes.probe('default','native-reader',1,actor=Principal.operator())
        principal=self.app.state.identities.create_principal('default','Native executor',
            ['invocations:execute','operations:read','runtimes:read','traces:read'])
        token=self.app.state.identities.grant_credential(principal['principal_id'],3600)['token']
        actor=self.app.state.identities.authenticate(token)
        claimed=self.app.state.execution.claim('default',invocation['invocation_id'],request_key='native-claim',
            worker_id='native-reader',lease_seconds=300,actor=actor)['attempt']
        path='/api/v1/projects/default/invocations/'+invocation['invocation_id']+'/task-grants'
        status,raw=self.fetch(path,'POST','Bearer '+token,{'attempt':1,'lease_id':claimed['lease_id'],'runtime':runtime})
        self.assertEqual(status,201,raw);grant=json.loads(raw);bearer='Bearer '+grant['token']
        self.assertEqual(self.fetch('/api/v1/task',authorization=bearer)[0],200)
        status,raw=self.fetch('/api/v1/task/inputs/0/records?limit=1',authorization=bearer)
        self.assertEqual(status,200,raw);self.assertEqual(json.loads(raw)['record_count'],len(self.trace['spans']))
        for auth in (None,self.basic,'Bearer '+token):
            self.assertEqual(self.fetch('/api/v1/task',authorization=auth)[0],401)
        for path_denied in ('/api/v1/projects/default','/api/runs','/'):
            self.assertEqual(self.fetch(path_denied,authorization=bearer)[0],401)
        self.assertEqual(self.fetch(path+'/'+grant['grant']['grant_id']+'/revoke','POST','Bearer '+token)[0],200)
        self.assertEqual(self.fetch('/api/v1/task',authorization=bearer)[0],401)
        from trace_hunter.runtimes.task_access import EXECUTION_PROTOCOL
        self.app.state.runtimes.reader.document['protocols'].append(EXECUTION_PROTOCOL)
        self.app.state.runtimes.probe('default','native-reader',1,actor=Principal.operator())
        status,raw=self.fetch(path,'POST','Bearer '+token,{'attempt':1,'lease_id':claimed['lease_id'],'runtime':runtime,'allow_execution':True})
        self.assertEqual(status,201,raw);grant=json.loads(raw);bearer='Bearer '+grant['token']
        self.assertEqual(self.fetch('/api/v1/task/heartbeat','POST',bearer,{})[0],200)
        status,raw=self.fetch('/api/v1/task/actions','POST',bearer,{'action_key':'plan-only','operation':'fixture.action','request':{}})
        self.assertEqual(status,201,raw);action=json.loads(raw)['action']
        action_path='/api/v1/task/actions/'+action['action_id']
        self.assertEqual(self.fetch(action_path,authorization=bearer)[0],200)
        self.assertEqual(self.fetch(action_path+'/abandon','POST',bearer,{'expected_version':1})[0],200)
        output={'outputs':[{'role':'report','content':{'encoding':'base64','media_type':'text/plain','data':'T0s='},'metadata':{}}]}
        status,raw=self.fetch('/api/v1/task/complete','POST',bearer,output)
        self.assertEqual(status,201,raw);receipt=json.loads(raw)
        self.assertEqual(receipt['delegation']['grant_id'],grant['grant']['grant_id'])
        self.assertEqual(self.fetch('/api/v1/task/complete','POST',bearer,output)[0],200)
        self.assertEqual(self.fetch('/api/v1/task/inputs',authorization=bearer)[0],401)
        status,raw=self.fetch('/api/v1/task/state',authorization=bearer)
        self.assertEqual(status,200);self.assertTrue(json.loads(raw)['should_stop'])
        self.assertEqual(self.fetch('/api/v1/task/heartbeat','POST',bearer,{})[0],409)

    def test_service_v1_bearer_channel_authenticates_and_revokes_without_basic(self):
        grant=self.service_credential();bearer='Bearer '+grant['token']
        self.assertEqual(self.fetch('/api/v1/projects/default')[0],401)
        self.assertEqual(self.fetch('/api/v1/projects/default',authorization=bearer)[0],200)
        status,raw=self.fetch('/api/v1/projects/default',authorization='Bearer invalid')
        self.assertEqual(status,401);self.assertIn(b'Invalid or expired credential',raw)
        self.assertEqual(self.fetch('/api/v1/projects/other-project',authorization=bearer)[0],403)
        self.assertEqual(self.fetch('/api/v1/projects',authorization=bearer)[0],403)
        self.assertEqual(self.fetch('/api/v1/credentials/'+grant['credential']['credential_id']+'/revoke','POST',self.basic,{})[0],200)
        self.assertEqual(self.fetch('/api/v1/projects/default',authorization=bearer)[0],401)

    def test_service_bearer_does_not_open_legacy_gateway_endpoints(self):
        bearer='Bearer '+self.service_credential()['token']
        for path in ('/','/api/runs','/api/extensions','/api/plugin-runs'):
            self.assertEqual(self.fetch(path,authorization=bearer)[0],401)

    def test_duplicate_authorization_headers_cannot_mix_bearer_and_operator(self):
        bearer='Bearer '+self.service_credential()['token']
        for values in (('Basic invalid',bearer),(bearer,'Basic invalid')):
            connection=http.client.HTTPConnection('127.0.0.1',self.web_port,timeout=5)
            try:
                connection.putrequest('GET','/api/v1/projects')
                for value in values:connection.putheader('Authorization',value)
                connection.endheaders();response=connection.getresponse()
                self.assertEqual(response.status,401);response.read()
            finally:connection.close()

    def test_gateway_operator_uses_socket_peer_not_forwarded_headers(self):
        request=urllib.request.Request(self.base+'/api/v1/projects',headers={
            'Authorization':self.basic,'X-Forwarded-For':'203.0.113.7','Forwarded':'for=203.0.113.7',
            'X-Trace-Hunter-Operator':'false'})
        with self.opener.open(request,timeout=5) as response:self.assertEqual(response.status,200)

    def test_browser_and_operator_paths_require_basic_even_with_bearer(self):
        paths=[('/', 'GET'),('/assets/private.js','GET'),('/api/health','GET'),('/api/runs','GET'),('/api/import','POST')]
        for family in ('plugin','evaluation'):
            root='/api/'+family+'-jobs/job-example'
            paths.extend([(root,'GET'),(root+'/grant','POST'),(root+'/cancel','POST'),(root+'/retry','POST'),(root+'/records/span/id','GET'),(root+'/input','POST')])
        for path,method in paths:
            for authorization in (None,'Bearer arbitrary-token'):
                with self.subTest(path=path,authorization=bool(authorization)):
                    self.assertEqual(self.fetch(path,method,authorization,{} if method=='POST' else None)[0],401)
        self.assertEqual(self.fetch('/',authorization='Basic '+base64.b64encode(b'invalid:invalid').decode())[0],401)
        self.assertEqual(self.fetch('/',authorization=self.basic),(200,b'private-trace-test'))
        self.assertEqual(self.fetch('/api/health',authorization=self.basic)[0],200)

    def test_worker_token_channel_preserves_real_claim_lease_and_submit(self):
        job=self.create();root='/api/plugin-jobs/'+job['id']
        self.assertEqual(self.fetch(root+'/claim','POST',body={'worker':'test'})[0],401)
        self.assertEqual(self.fetch(root+'/claim','POST','Bearer invalid',{'worker':'test'})[0],403)
        status,raw=self.fetch(root+'/grant','POST',self.basic)
        self.assertEqual(status,200);dispatch=json.loads(raw)['dispatch_token']
        status,raw=self.fetch(root+'/claim','POST','Bearer '+dispatch,{'worker':'caddy-test'})
        self.assertEqual(status,200);lease=json.loads(raw);bearer='Bearer '+lease['lease_token'];suffix='?attempt='+str(lease['attempt'])
        self.assertEqual(self.fetch(root+'/input'+suffix,authorization='Bearer invalid')[0],403)
        status,raw=self.fetch(root+'/input'+suffix,authorization=bearer);self.assertEqual(status,200);context=json.loads(raw)
        self.assertEqual(self.fetch(root+'/heartbeat'+suffix,'POST',bearer)[0],200)
        self.assertEqual(self.fetch(root+'/query'+suffix,'POST',bearer,{'kind':'spans','limit':1})[0],200)
        span=context['input']['spans'][0]['id']
        self.assertEqual(self.fetch(root+'/record-read'+suffix+'&kind=span&record_id='+span,authorization=bearer)[0],200)
        self.assertEqual(self.fetch(root+'/records/span/'+span,authorization=bearer)[0],401)
        spec=importlib.util.spec_from_file_location('auth_classifier',ROOT/'plugins/extensions/call-activity/classify.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertEqual(self.fetch(root+'/results'+suffix,'POST',bearer,module.evaluate(context))[0],200)
        self.assertEqual(self.fetch(root,authorization=self.basic)[0],200)

    def test_legacy_evaluation_agent_uses_the_same_private_token_boundary(self):
        manifest=copy.deepcopy(self.evaluations.plugins()[0]['manifest'])
        manifest['plugin_id']='test.caddy-evaluator';self.evaluations.register(manifest)
        job=self.evaluations.create({'plugin_id':manifest['plugin_id'],'plugin_version':manifest['version'],
            'scope':'task','config':manifest['default_config'],'collection_id':None,
            'run_ids':[self.trace['run']['id']],'request_key':uuid.uuid4().hex})['jobs'][0]
        root='/api/evaluation-jobs/'+job['id']
        self.assertEqual(self.fetch(root+'/grant','POST','Bearer invalid')[0],401)
        status,raw=self.fetch(root+'/grant','POST',self.basic);self.assertEqual(status,200)
        status,raw=self.fetch(root+'/claim','POST','Bearer '+json.loads(raw)['dispatch_token'],{'worker':'legacy-test'})
        self.assertEqual(status,200);lease=json.loads(raw);bearer='Bearer '+lease['lease_token'];suffix='?attempt='+str(lease['attempt'])
        self.assertEqual(self.fetch(root+'/input'+suffix,authorization=bearer)[0],200)
        self.assertEqual(self.fetch(root+'/heartbeat'+suffix,'POST',bearer)[0],200)
        self.assertEqual(self.fetch(root+'/fail'+suffix,'POST',bearer,{'error':'disposable legacy test'})[0],200)

    def test_exact_worker_paths_and_failed_token_have_no_operator_bypass(self):
        job=self.create();root='/api/plugin-jobs/'+job['id']
        for action,method in [('input','GET'),('record-read','GET'),('heartbeat','POST'),('query','POST'),('results','POST'),('fail','POST')]:
            body={'kind':'spans'} if action=='query' else {'error':'test failure'} if action=='fail' else {}
            with self.subTest(action=action):
                status,_=self.fetch(root+'/'+action+'?attempt=1&kind=span&record_id=missing',method,'Bearer invalid',body if method=='POST' else None)
                self.assertIn(status,(403,422))
        for suffix in ('/input/../grant','/input%2f..%2fgrant','/grant?next=input','/record-read/extra'):
            self.assertEqual(self.fetch(root+suffix,'POST','Bearer invalid',{})[0],401)
        status,raw=self.fetch(root+'/grant','POST',self.basic);self.assertEqual(status,200)
        status,raw=self.fetch(root+'/claim','POST','Bearer '+json.loads(raw)['dispatch_token'],{'worker':'failure-test'});self.assertEqual(status,200)
        lease=json.loads(raw)
        self.assertEqual(self.fetch(root+'/fail?attempt='+str(lease['attempt']),'POST','Bearer '+lease['lease_token'],{'error':'disposable test failure'})[0],200)


if __name__=='__main__':unittest.main()
