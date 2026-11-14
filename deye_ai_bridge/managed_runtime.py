"""Immutable parent process: approval gate + unprivileged Bridge child."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from maintenance import Maintenance, MAX_BYTES, atomic_json

DATA = Path('/data')
CORE = Path('/control')

def prepare_permissions(data):
    # Sticky root-owned parent protects root-owned approval/code directories from
    # rename/deletion by the application, while allowing its existing state writes.
    os.chown(data, 0, 0)
    data.chmod(0o1777)
    excluded = {'.maintenance', 'bridge_code', 'bridge_maintenance.operation', 'bridge_maintenance.blocked', 'options.json'}
    for entry in data.iterdir():
        if entry.name in excluded or entry.is_symlink(): continue
        paths = [entry] + list(entry.rglob('*')) if entry.is_dir() else [entry]
        for path in paths:
            if path.is_symlink(): continue
            os.chown(path, 1000, 1000)
    options = data/'options.json'
    if options.exists():
        os.chown(options, 0, 0); options.chmod(0o644)

def handler_class(manager):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, value, status=200, content_type='application/json; charset=utf-8'):
            payload = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
            self.end_headers(); self.wfile.write(payload)

        def do_GET(self):
            try:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == '/status' and not parsed.query: return self.reply(manager.status())
                if parsed.path == '/ui' and not parsed.query: return self.reply((CORE/'maintenance.html').read_bytes(), content_type='text/html; charset=utf-8')
                if parsed.path == '/logs':
                    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                    if set(params)-{'after_id','window_end'} or any(len(v)!=1 for v in params.values()):
                        raise ValueError('Ugyldige logparametre')
                    return self.reply(manager.read_events(int(params.get('after_id',['0'])[0]),params.get('window_end',[None])[0]))
                self.reply({'error': 'Ukendt læsekald'}, 404)
            except Exception as exc: self.reply({'error': str(exc)}, 400)

        def do_POST(self):
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BYTES+2048: raise ValueError('Ugyldig pakkestørrelse')
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('JSON kræves')
                body = json.loads(self.rfile.read(length))
                if self.path == '/preview': return self.reply(manager.preview(body))
                if self.path == '/apply': return self.reply(manager.apply(body))
                if self.path == '/grant':
                    if type(body) is not dict or set(body) != {'package', 'secret'}:
                        raise ValueError('Ukendt godkendelsesskema')
                    return self.reply(manager.grant(body['package'], body['secret']))
                self.reply({'error': 'Ukendt handling'}, 404)
            except PermissionError as exc: self.reply({'error': str(exc)}, 403)
            except Exception as exc: self.reply({'error': str(exc)}, 400)

        def log_message(self, *_):
            pass  # Never log request bodies, approval credentials or URL parameters.
    return Handler

def spawn_bridge(manager):
    directory = manager.release_dir(manager.active())
    env = dict(os.environ, DEYE_APP_DIR=str(directory), DEYE_MANAGED_RUNTIME='1', PYTHONDONTWRITEBYTECODE='1')
    return subprocess.Popen([sys.executable, str(directory/'app.py')], cwd=directory, env=env,
                            user=1000, group=1000, extra_groups=[], umask=0o077, start_new_session=True)

def main():
    if os.geteuid() != 0: raise SystemExit('Vedligeholdelseskernen skal starte som root')
    manager = Maintenance(DATA, Path('/app'))
    manager.recover_interrupted()
    prepare_permissions(DATA)
    print('Lokal godkendelsesnøgle til vedligeholdelse (kun til HA-siden; del den ikke i chat): '+manager.secret, flush=True)
    server = ThreadingHTTPServer(('127.0.0.1', 8100), handler_class(manager))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=manager.event_worker, daemon=True).start()
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def start():
        return spawn_bridge(manager)

    def stop(child):
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try: child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL); child.wait(timeout=5)

    def healthy(child):
        deadline = time.monotonic()+45
        while time.monotonic()<deadline and not stopping.is_set():
            if child.poll() is not None: return False
            try:
                with opener.open('http://127.0.0.1:8099/api/private/runtime-health', timeout=2) as response:
                    if json.load(response).get('ready') is True: return True
            except Exception: pass
            stopping.wait(0.5)
        return False

    child = start()
    try:
        while not stopping.wait(0.25):
            if manager.restart.is_set() and time.monotonic()>=manager.restart_after:
                with manager.lock:
                    operation = dict(manager.pending_restart)
                    operation['status'] = 'starting'
                    atomic_json(manager.private/'operation.json', operation)
                stop(child)
                child = start()
                success = healthy(child)
                if not success:
                    stop(child)
                manager.finish_restart(success)
                if not success:
                    child = start()
                    if not healthy(child):
                        print('Tidligere Bridge-kode kunne heller ikke starte. Manuel kontrol kræves.', flush=True)
                        break
            elif child.poll() is not None:
                # No unapproved automatic code switching or restart loop.
                print('Bridge-processen stoppede uventet; ingen ny release aktiveres.', flush=True)
                break
    finally:
        stop(child)
        server.shutdown()

if __name__ == '__main__': main()
