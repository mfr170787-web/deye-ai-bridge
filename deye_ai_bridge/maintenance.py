"""Pinned deployment core. This file is not a remotely updatable application file.

Remote calls may preview bytes without saving them. Installation requires a local,
expiring, one-use approval of the canonical package hash and current code revision.
"""
import ast
import difflib
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path

FILES = ('app.py', 'automatic_control.py', 'shadow_planning.py', 'modbus_telemetry.py',
         'energy_accounting.py', 'monthly_register.py', 'charge_test.py',
         'charge20_test.py', 'test_suite.py', 'private_reads.py', 'index.html', 'tesla_icon.b64')
MAX_BYTES = 800000
MAX_FILE_BYTES = 220000
GRANT_SECONDS = 900

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')

def sha(value):
    return hashlib.sha256(value).hexdigest()

def digest(value):
    return sha(canonical(value))

def valid_hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None

def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp-' + secrets.token_hex(6))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if temporary.exists(): temporary.unlink()

class Maintenance:
    def __init__(self, data_dir, base_dir, clock=time.time):
        self.data = Path(data_dir).resolve()
        self.base = Path(base_dir).resolve()
        self.private = self.data / '.maintenance'
        self.code = self.data / 'bridge_code'
        self.clock = clock
        self.lock = threading.RLock()
        self.restart = threading.Event()
        self.pending_restart = None
        self.restart_after = 0
        if self.private.is_symlink() or self.code.is_symlink():
            raise ValueError('Vedligeholdelsesmapper må ikke være symbolske links')
        self.private.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.private.chmod(0o700)
        self.code.mkdir(mode=0o755, exist_ok=True)
        self.code.chmod(0o755)
        (self.data / 'bridge_maintenance.operation').touch(exist_ok=True)
        (self.data / 'bridge_maintenance.operation').chmod(0o644)
        key = self.private / 'approval.key'
        if key.is_symlink(): raise ValueError('Ugyldig godkendelsesnøglefil')
        if not key.exists():
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(secrets.token_urlsafe(32))
        key.chmod(0o600)
        self.secret = key.read_text().strip()

    def read_private(self, name, default=None):
        path = self.private / name
        return json.loads(path.read_text()) if path.exists() else default

    def active(self):
        return self.read_private('active.json', {'release': 'base'})['release']

    def release_dir(self, release):
        if release == 'base': return self.base
        if not valid_hash(release): raise ValueError('Ugyldig release')
        path = self.code / release
        if path.is_symlink() or not path.is_dir(): raise ValueError('Release mangler')
        return path

    def revision(self, directory=None):
        root = directory or self.release_dir(self.active())
        result = {}
        for name in FILES:
            path = root / name
            if path.is_symlink(): raise ValueError('Symlink i kode er ikke tilladt')
            if path.is_file(): result[name] = sha(path.read_bytes())
        return digest(result)

    def inactive(self):
        path = self.data / 'automatic_control.json'
        if not path.exists():
            raise ValueError('Automatikstatus mangler; afvisning indtil status kan kontrolleres')
        state = json.loads(path.read_text())
        if (state.get('status') != 'disabled' or state.get('restored') is not True
                or state.get('pending') is not None or state.get('auto_resume')
                or state.get('tesla_only_active') or state.get('error')):
            raise ValueError('Automatikken skal være deaktiveret og gendannelsen verificeret uden fejl')
        for filename, allowed in [('charge_test.json', ('idle','restored')),
                                  ('charge20_test.json', ('idle','complete','failed')),
                                  ('test_suite.json', ('idle','complete','failed'))]:
            legacy = self.data/filename
            if legacy.exists():
                test = json.loads(legacy.read_text())
                if test.get('status') not in allowed:
                    raise ValueError('Et ældre testforløb er ikke afsluttet: '+filename)

    def validate(self, package):
        if type(package) is not dict or set(package) != {'format', 'action', 'base_revision', 'files', 'target_release', 'restart', 'rollback_on_failure'}:
            raise ValueError('Ukendt pakkeskema')
        if type(package['format']) is not int or package['format'] != 1:
            raise ValueError('Ukendt pakkeformat')
        if package['action'] not in ('install', 'rollback'):
            raise ValueError('Ukendt handling')
        if not valid_hash(package['base_revision']) or package['restart'] is not True or package['rollback_on_failure'] is not True:
            raise ValueError('Pakke skal angive kodegrundlag, procesgenstart og tilbageførsel ved opstartsfejl')
        if len(canonical(package)) > MAX_BYTES: raise ValueError('Pakken er for stor')
        files = package['files']
        if type(files) is not dict: raise ValueError('Ugyldig filliste')
        for name, content in files.items():
            if name not in FILES or not isinstance(content, str):
                raise ValueError('Fil eller filtype er ikke tilladt')
            if len(content.encode('utf-8')) > MAX_FILE_BYTES:
                raise ValueError('Kodefilen er for stor')
            if name.endswith('.py'): ast.parse(content, filename=name)
        if package['action'] == 'install':
            if not files or package['target_release'] is not None:
                raise ValueError('Installationspakken kræver filer og intet rollbackmål')
        else:
            if files or not (package['target_release'] == 'base' or valid_hash(package['target_release'])):
                raise ValueError('Ugyldig tilbageførsel')
            self.release_dir(package['target_release'])

    def preview(self, package):
        # No file writes, no subprocesses, no grant, no changes to the active code.
        self.validate(package)
        current = self.revision()
        if current != package['base_revision']: raise ValueError('Kodegrundlaget har ændret sig')
        root = self.release_dir(self.active())
        diffs = {}
        target = self.release_dir(package['target_release']) if package['action'] == 'rollback' else None
        changes = package['files'] if target is None else {name: (target/name).read_text() for name in FILES if (target/name).is_file()}
        for name, content in changes.items():
            old = (root/name).read_text() if (root/name).exists() else ''
            if old != content:
                diffs[name] = ''.join(difflib.unified_diff(old.splitlines(True), content.splitlines(True), fromfile='før/'+name, tofile='efter/'+name))
        if len(canonical(diffs)) > 850000:
            raise ValueError('Den samlede diff er for stor til sikker gennemgang i ét svar')
        return {'package_sha256': digest(package), 'base_revision': current, 'action': package['action'],
                'restart': True, 'rollback_on_failure': True, 'diffs': diffs,
                'consequence': 'Skriver godkendt Bridge-kode og backup. Genstarter kun Bridge-processen. Automatikken forbliver deaktiveret. Ved opstartsfejl genstartes den tidligere kode.'}

    def authenticate(self, supplied):
        if not isinstance(supplied, str) or not hmac.compare_digest(supplied, self.secret):
            raise PermissionError('Lokal godkendelsesnøgle kræves. Den må ikke sendes i chatten.')

    def grant(self, package, secret):
        self.authenticate(secret)
        with self.lock:
            if self.pending_restart is not None: raise ValueError('En installation afventer genstart')
            self.inactive()
            preview = self.preview(package)
            atomic_json(self.private/'grant.json', {'sha256': preview['package_sha256'],
                         'base_revision': preview['base_revision'], 'expires': self.clock()+GRANT_SECONDS})
            return {'approved_sha256': preview['package_sha256'], 'expires_in_seconds': GRANT_SECONDS,
                    'note': 'Godkendelsen gælder kun denne pakke og kan bruges én gang.'}

    @contextmanager
    def operation(self):
        with self.lock, (self.data/'bridge_maintenance.operation').open('r') as operation:
            fcntl.flock(operation, fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(operation, fcntl.LOCK_UN)

    def backup_data(self, directory):
        directory.mkdir(mode=0o700)
        for name in ('shadow.db', 'monthly_register.db', 'clock_diagnostics.db'):
            source = self.data/name
            if source.is_file() and not source.is_symlink():
                with sqlite3.connect(source.as_uri()+'?mode=ro', uri=True) as src, sqlite3.connect(directory/name) as dst:
                    src.backup(dst)
        for source in self.data.glob('*.json'):
            if source.is_file() and not source.is_symlink():
                shutil.copyfile(source, directory/source.name)

    def apply(self, package):
        with self.operation():
            if self.pending_restart is not None: raise ValueError('En installation afventer allerede genstart')
            self.inactive()
            self.validate(package)
            current = self.revision()
            approval = self.read_private('grant.json', {})
            package_hash = digest(package)
            if (approval.get('sha256') != package_hash or approval.get('base_revision') != current
                    or package['base_revision'] != current or approval.get('expires', 0) <= self.clock()):
                raise PermissionError('Ingen gyldig lokal engangsgodkendelse til denne pakke')
            # Burn approval before any installation writes. An interrupted attempt needs a new approval.
            (self.private/'grant.json').unlink()
            previous = self.active()
            operation_id = secrets.token_hex(12)
            backup = self.private/('backup-'+operation_id)
            self.backup_data(backup)
            marker = self.data/'bridge_maintenance.blocked'
            marker.write_text(operation_id)
            marker.chmod(0o644)
            try:
                if package['action'] == 'install':
                    target = package_hash
                    release = self.code/target
                    if release.exists(): raise ValueError('Release findes allerede; brug eksplicit tilbageførsel')
                    release.mkdir(mode=0o755)
                    for name in FILES:
                        source = self.release_dir(previous)/name
                        if source.is_file(): shutil.copyfile(source, release/name)
                    for name, content in package['files'].items(): (release/name).write_text(content)
                    for item in release.iterdir(): item.chmod(0o444)
                    release.chmod(0o555)
                else:
                    target = package['target_release']
                pending = dict(id=operation_id, package_sha256=package_hash, previous=previous,
                               target=target, backup=backup.name, status='restart_pending',
                               rollback_on_failure=True)
                # Journal before switching: a crash will recover previous code, never silently activate pending code.
                atomic_json(self.private/'operation.json', pending)
                atomic_json(self.private/'active.json', {'release': target})
                self.pending_restart = pending
                self.restart_after = time.monotonic()+1.0
                self.restart.set()
                return dict(status='restart_pending', operation_id=operation_id, backup=backup.name)
            except Exception:
                if marker.exists(): marker.unlink()
                raise

    def recover_interrupted(self):
        operation = self.read_private('operation.json', {})
        if operation.get('status') in ('restart_pending', 'starting'):
            atomic_json(self.private/'active.json', {'release': operation['previous']})
            operation['status'] = 'interrupted_rolled_back'
            atomic_json(self.private/'operation.json', operation)
        marker = self.data/'bridge_maintenance.blocked'
        if marker.exists(): marker.unlink()
        # Approvals do not survive a runtime restart.
        approval = self.private/'grant.json'
        if approval.exists(): approval.unlink()

    def finish_restart(self, success):
        with self.lock:
            operation = self.pending_restart
            if operation is None: raise ValueError('Ingen ventende genstart')
            if not success:
                atomic_json(self.private/'active.json', {'release': operation['previous']})
            operation['status'] = 'installed' if success else 'startup_failed_rolled_back'
            atomic_json(self.private/'operation.json', operation)
            self.pending_restart = None
            self.restart.clear()
            marker = self.data/'bridge_maintenance.blocked'
            if marker.exists(): marker.unlink()

    def status(self):
        with self.lock:
            approval = self.read_private('grant.json', {})
            version = None
            tree = ast.parse((self.release_dir(self.active())/'app.py').read_text())
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(isinstance(target,ast.Name) and target.id=='APP_VERSION' for target in node.targets):
                    if isinstance(node.value,ast.Constant) and isinstance(node.value.value,str):version=node.value.value
            return {'core_version': '1.0.0', 'app_version': version, 'active_release': self.active(), 'revision': self.revision(),
                    'approval_required': True, 'approved_sha256': approval.get('sha256') if approval.get('expires',0)>self.clock() else None,
                    'operation': self.read_private('operation.json'),
                    'releases': ['base'] + sorted(p.name for p in self.code.iterdir() if p.is_dir() and valid_hash(p.name)),
                    'allowed_files': list(FILES)}

    def archive_events(self):
        path = self.data/'automatic_control.json'
        if not path.exists(): return
        state = json.loads(path.read_text())
        with sqlite3.connect(self.private/'events.db') as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE, at TEXT, category TEXT, content TEXT)')
            for category, entries in [('event', state.get('events', [])), ('observation', state.get('observations', []))]:
                for entry in entries:
                    if type(entry) is not dict or not isinstance(entry.get('at'), str): continue
                    fields = ('at','kind','address','values','attempt','reason','command','observed') if category=='event' else ('at','voltage_v','soc','battery_w','clock_offset_seconds','action')
                    safe = {key: entry[key] for key in fields if key in entry}
                    encoded = canonical(safe)
                    if len(encoded)>6000: continue
                    conn.execute('INSERT OR IGNORE INTO events(fingerprint, at, category, content) VALUES (?, ?, ?, ?)',
                                 (sha(category.encode()+encoded), entry['at'], category, encoded.decode()))
            cutoff = (datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
            conn.execute('DELETE FROM events WHERE julianday(at)<julianday(?)', (cutoff,))

    def read_events(self, after_id=0, window_end=None):
        if type(after_id) is not int or after_id<0: raise ValueError('Ugyldig sideposition')
        end = datetime.now(timezone.utc) if window_end is None else datetime.fromisoformat(window_end)
        if end.tzinfo is None: raise ValueError('Tidszone kræves')
        start = end-timedelta(hours=24)
        path = self.private/'events.db'
        if not path.exists(): return {'events': [], 'has_more': False, 'note': 'Journalen er endnu ikke startet.'}
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('SELECT id, at, category, content FROM events WHERE id>? AND julianday(at)>=julianday(?) AND julianday(at)<=julianday(?) ORDER BY id LIMIT 101', (after_id,start.isoformat(),end.isoformat())).fetchall()
            bounds = conn.execute('SELECT min(at), max(at) FROM events').fetchone()
        return dict(events=[dict(id=row['id'], at=row['at'], category=row['category'], content=json.loads(row['content'])) for row in rows[:100]],
                    has_more=len(rows)>100, next_after_id=rows[99]['id'] if len(rows)>100 else (rows[-1]['id'] if rows else after_id),
                    window_start=start.isoformat(), window_end=end.isoformat(), first_at=bounds[0], last_at=bounds[1],
                    note='Journalen opsamler fra installationen og de gamle poster, der stadig findes. Manglende historik genskabes ikke. Op til 30 dages opbevaring.')

    def event_worker(self):
        while True:
            try: self.archive_events()
            except Exception:
                # No secrets or arbitrary stored strings in logs.
                print('Vedligeholdelsesjournal kunne ikke opsamles.', flush=True)
            time.sleep(5)
