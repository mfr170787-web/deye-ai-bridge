"""Bounded migration for the explicitly installed 0.9.5 HA image.

Runs before the application child exists. Only Modbus function 3 (read) is
implemented. Never arms control, retries a write, or silently accepts a fault.
"""
import json
import re
import secrets
import socket
import struct
from datetime import datetime, timezone
from maintenance import FILES, atomic_json, digest, sha

VERSION = '0.9.5'
OLD_REVISION = '7f8724599f658c12fed2479e2b9999a6941c2f592271c50d219abef0c1af68ee'
STALE_FAULT = 'fault: Målinger er over 120 sekunder gamle eller ugyldige.'
GROUPS = ((108, 2), (128, 1), (148, 6), (166, 12))
ADDRESSES = {str(a) for start, count in GROUPS for a in range(start, start+count)}


def exact(sock, size):
    data = b''
    while len(data) < size:
        part = sock.recv(size-len(data))
        if not part:
            raise ConnectionError('Modbus-forbindelsen blev lukket')
        data += part
    return data


def read_registers(endpoint, timeout, start, count):
    transaction = secrets.randbelow(65536)
    host, port, unit = endpoint
    pdu = struct.pack('>BHH', 3, start, count)
    packet = struct.pack('>HHHB', transaction, 0, len(pdu)+1, unit)+pdu
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(packet)
        tx, protocol, length, slave = struct.unpack('>HHHB', exact(sock, 7))
        if tx != transaction or protocol != 0 or slave != unit or length != 3+2*count:
            raise ValueError('Ugyldigt Modbus-hoved; gendannelse ikke bekræftet')
        body = exact(sock, length-1)
    if body[:2] != bytes((3, 2*count)):
        raise ValueError('Ugyldigt Modbus-læsesvar')
    return dict(zip((str(a) for a in range(start, start+count)), struct.unpack('>'+'H'*count, body[2:])))


def eligible(state):
    return (state.get('status') == 'fault' and state.get('restored') is True
            and state.get('error') == STALE_FAULT and state.get('pending') is None
            and not state.get('auto_resume') and not state.get('tesla_only_active')
            and not state.get('battery_timer_mode'))


def prepare_image_recovery(manager, reader=read_registers):
    """Return True only when a verified candidate awaits the parent's health check.

    A failed prerequisite is an explicit blocked result; the old code and fault
    are retained. Approval keys, databases and inverter registers are untouched.
    """
    record = manager.private / ('image-'+VERSION+'.json')
    if record.exists():
        return False  # Never repeat an attempted image migration automatically.
    if manager.revision() != OLD_REVISION:
        return False  # Do not overwrite unknown local releases.
    state_path = manager.data/'automatic_control.json'
    try:
        original = state_path.read_bytes()
        state = json.loads(original)
        if not eligible(state):
            raise ValueError('Kræver den kendte 120-sekunders fejl og verificeret gendannelse')
        cfg = json.loads((manager.data/'options.json').read_text())
        endpoint = [cfg['deye_host'], cfg['deye_port'], cfg['slave_id']]
        if (not isinstance(endpoint[0], str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,253}', endpoint[0])
                or type(endpoint[1]) is not int or not 1 <= endpoint[1] <= 65535
                or type(endpoint[2]) is not int or not 1 <= endpoint[2] <= 247
                or state.get('endpoint') != endpoint):
            raise ValueError('Inverterforbindelsen er ændret eller ugyldig')
        baseline = state.get('baseline')
        if (type(baseline) is not dict or set(baseline) != ADDRESSES
                or any(type(v) is not int or not 0 <= v <= 65535 for v in baseline.values())):
            raise ValueError('Oprindelige registerværdier mangler eller er ugyldige')
        timeout = cfg.get('timeout_seconds', 3)
        if type(timeout) is not int or not 1 <= timeout <= 15:
            raise ValueError('Ugyldig kommunikationstimeout')
        # Isolated core; no Bridge child runs yet. Three complete live reads.
        for _ in range(3):
            observed = {}
            for start, count in GROUPS:
                observed.update(reader(endpoint, timeout, start, count))
            if observed != baseline:
                raise ValueError('Inverterens registre afviger fra de oprindelige indstillinger')
        if state_path.read_bytes() != original:
            raise ValueError('Automatikstatus ændrede sig under kontrollen')
        # Reject active legacy tests using the existing gate, without modifying
        # the live state just to test it.
        for filename, allowed in [('charge_test.json', ('idle','restored')),
                                  ('charge20_test.json', ('idle','complete','failed')),
                                  ('test_suite.json', ('idle','complete','failed'))]:
            path = manager.data/filename
            if path.exists() and json.loads(path.read_text()).get('status') not in allowed:
                raise ValueError('En idriftsættelsestest er stadig aktiv')
    except Exception as exc:
        atomic_json(record, {'version': VERSION, 'status': 'blocked', 'reason': str(exc)})
        print('Bridge '+VERSION+' gendannelsesopdatering BLOKERET: '+str(exc), flush=True)
        return False

    # Build an immutable, content-addressed image release, preserving old
    # overlay code for rollback rather than re-pointing its mutable base.
    files = {name:(manager.base/name).read_text() for name in FILES if (manager.base/name).is_file()}
    release_id = digest({'image': VERSION, 'files': files})
    target = manager.code/release_id
    if target.exists():
        raise ValueError('Gendannelsesrelease findes allerede; manuel kontrol kræves')
    operation_id = secrets.token_hex(12)
    backup = manager.private/('backup-image-'+operation_id)
    manager.backup_data(backup)
    atomic_json(backup/'active-before.json', {'release': manager.active()})
    target.mkdir(mode=0o755)
    for name, content in files.items():
        path = target/name
        path.write_text(content)
        path.chmod(0o444)
    target.chmod(0o555)
    op = dict(id=operation_id, package_sha256=release_id, previous=manager.active(),
              target=release_id, backup=backup.name, status='starting',
              rollback_on_failure=True, source='explicit_ha_image_update_'+VERSION)
    atomic_json(manager.private/'operation.json', op)
    atomic_json(record, {'version': VERSION, 'status': 'starting', 'operation_id': operation_id,
                         'previous_error': state['error'], 'register_reads_verified': 3,
                         'state_before_sha256': sha(original)})
    state.setdefault('events', []).append(dict(at=datetime.now(timezone.utc).isoformat(),
        kind='restored_fault_acknowledged', previous_error=state['error'],
        reason='Explicit HA image '+VERSION+'; three baseline readbacks verified'))
    state.update(status='disabled', error=None, stop_reason='verified_image_recovery_'+VERSION,
                 command=None, signature=None, auto_resume=False, tesla_only_active=False,
                 battery_timer_mode=None, restored=True, pending=None)
    atomic_json(state_path, state)
    atomic_json(manager.private/'active.json', {'release': release_id})
    manager.pending_restart = op
    print('Bridge '+VERSION+': oprindelige registre verificeret tre gange. Automatik forbliver fra. Kontrollerer ny kode.', flush=True)
    return True
