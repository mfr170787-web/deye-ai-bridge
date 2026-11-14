"""Pinned ingress front door: maintenance remains available without the child."""
import json
import urllib.request
import urllib.error
from urllib.parse import urlparse,parse_qs
from http.server import BaseHTTPRequestHandler
from local_updates import PREFIX
from maintenance import MAX_BYTES


def frontdoor_handler(local, template):
    class Handler(BaseHTTPRequestHandler):
        def reply(self,value,status=200,mime='application/json; charset=utf-8'):
            payload=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(len(payload)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
            self.end_headers();self.wfile.write(payload)

        def body(self):
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=MAX_BYTES+2048:raise ValueError('Ugyldig pakkestørrelse')
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('JSON kræves')
            return json.loads(self.rfile.read(size))

        def do_GET(self):
            try:
                parsed=urlparse(self.path);path=parsed.path.rstrip('/')
                if path in (PREFIX+'/ui','/api/private/maintenance/ui'):
                    return self.reply(local.page(template.read_text()),mime='text/html; charset=utf-8')
                if path in (PREFIX+'/status','/api/private/maintenance/status') and not parsed.query:
                    return self.reply(local.status())
                if path==PREFIX+'/list' and not parsed.query:return self.reply(local.listing())
                if path==PREFIX+'/proposal':
                    q=parse_qs(parsed.query,keep_blank_values=True)
                    if set(q)!={'id'} or len(q['id'])!=1:raise ValueError('Ugyldigt forslags-id')
                    item=local.proposal(q['id'][0])
                    return self.reply({**item,'proposal_id':q['id'][0]})
                if path.startswith(PREFIX+'/local-'):
                    return self.reply({'error':'Lokal godkendelse kræver POST'},405)
                return self.proxy()
            except PermissionError as exc:self.reply({'error':str(exc)},403)
            except Exception as exc:self.reply({'error':str(exc)},400)

        def do_POST(self):
            try:
                parsed=urlparse(self.path);path=parsed.path.rstrip('/')
                if path==PREFIX+'/submit' and not parsed.query:return self.reply(local.submit(self.body()))
                actions={PREFIX+'/local-flow':local.flow.start,PREFIX+'/local-preview':local.preview,PREFIX+'/local-install':local.install,
                         PREFIX+'/local-fault-ack':local.acknowledge_fault}
                if path in actions and not parsed.query:
                    # Browser nonces are private to a loaded local page; never
                    # returned by status, preview_update or proposal MCP tools.
                    if self.headers.get('Sec-Fetch-Site')=='cross-site':raise PermissionError('Tværgående godkendelse afvises')
                    nonce=self.headers.get('X-Deye-Local-Page','')
                    return self.reply(actions[path](self.body(),nonce))
                if path==PREFIX+'/approve':
                    return self.reply({'error':'Brug Opdater på den nye lokale side. Ingen kode kræves.'},409)
                return self.proxy()
            except PermissionError as exc:self.reply({'error':str(exc)},403)
            except Exception as exc:self.reply({'error':str(exc)},400)

        def proxy(self):
            path=urlparse(self.path).path
            # Keep legacy pinned-core reads/approvals working without involving
            # the child. All other app requests use one fixed loopback target.
            flow=local.flow.status()
            if self.command=='POST' and flow and flow.get('phase') not in ('complete','failed','interrupted'):
                # Manual stop remains available and cancels resume. Other child
                # writes cannot race with the reviewed update transaction.
                if path=='/api/automation':
                    body=self.body()
                    if body.get('action')!='disable':
                        return self.reply({'error':'Opdatering i gang; kun manuelt stop er tilladt.'},409)
                    local.flow.cancel_resume()
                    data=json.dumps(body).encode()
                    req=urllib.request.Request('http://127.0.0.1:8098/api/automation',data=data,
                        headers={'Content-Type':'application/json','X-Deye-Control':self.headers.get('X-Deye-Control','')})
                    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(req,timeout=90) as response:return self.reply(json.load(response))
                return self.reply({'error':'Opdatering i gang; vent på afsluttet kontrol.'},409)
            port=8100 if path.startswith('/api/private/maintenance/') else 8098
            suffix=self.path.removeprefix('/api/private/maintenance') if port==8100 else self.path
            length=int(self.headers.get('Content-Length','0'))
            if not 0<=length<=MAX_BYTES+2048:raise ValueError('Anmodning for stor')
            data=self.rfile.read(length) if self.command=='POST' else None
            req=urllib.request.Request('http://127.0.0.1:'+str(port)+suffix,data=data,method=self.command,
                headers={key:value for key,value in self.headers.items() if key.lower() not in
                    ('host','connection','content-length','x-deye-local-page')})
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                try:response=opener.open(req,timeout=65)
                except urllib.error.HTTPError as exc:response=exc
                with response:
                    payload=response.read(12000001)
                    if len(payload)>12000000:raise ValueError('Svar for stort')
                    self.send_response(response.code)
                    for key in ('Content-Type','Location','Content-Disposition','Cache-Control'):
                        if response.headers.get(key):self.send_header(key,response.headers[key])
                    self.send_header('Content-Length',str(len(payload)))
                    self.end_headers();self.wfile.write(payload)
            except (OSError,urllib.error.URLError):
                if 'text/html' in self.headers.get('Accept',''):
                    return self.reply(b'<!doctype html><meta charset="utf-8"><h1>Bridge-processen svarer ikke</h1><p>Vedligeholdelse virker stadig.</p><a href="api/private/updates/ui">Aabn vedligeholdelse og manuel upload</a>',503,'text/html; charset=utf-8')
                return self.reply({'error':'Bridge-processen svarer ikke. Vedligeholdelse og manuel upload er stadig tilgængelig.'},503)

        def log_message(self,*args):pass
    return Handler
