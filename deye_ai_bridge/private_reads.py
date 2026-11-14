"""Bounded, read-only access. No imports from the running control application."""
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOURCE_FILES = ('app.py', 'automatic_control.py', 'shadow_planning.py',
                'modbus_telemetry.py', 'energy_accounting.py', 'monthly_register.py',
                'charge_test.py', 'charge20_test.py', 'test_suite.py', 'index.html')
FIELDS = ('id', 'created', 'soc', 'battery_w', 'grid_w', 'grid_l1_w', 'grid_l2_w',
          'grid_l3_w', 'load_w', 'pv_w', 'pv1_w', 'pv2_w', 'price_dkk',
          'battery_charge_today', 'battery_discharge_today', 'grid_import_today',
          'grid_export_today', 'load_today', 'pv_today', 'tesla_soc',
          'tesla_charger_power_kw')

def read_history(db_path, after_id=0, window_end=None):
    if type(after_id) is not int or after_id < 0:
        raise ValueError('after_id skal være et positivt heltal eller nul')
    end = datetime.now(timezone.utc) if window_end is None else datetime.fromisoformat(window_end)
    if end.tzinfo is None:
        raise ValueError('window_end kræver tidszone')
    end = end.astimezone(timezone.utc)
    start = end - timedelta(hours=24)
    # mode=ro fails when absent and never creates a database.
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.row_factory = sqlite3.Row
        existing = {row[1] for row in conn.execute('PRAGMA table_info(samples)')}
        if not {'id', 'created'} <= existing:
            raise ValueError('Ukendt databaseskema')
        columns = ','.join(f for f in FIELDS if f in existing)
        rows = conn.execute(
            f'SELECT {columns} FROM samples WHERE id>? AND julianday(created)>=julianday(?) '
            'AND julianday(created)<=julianday(?) ORDER BY id LIMIT 201',
            (after_id, start.isoformat(), end.isoformat())).fetchall()
        page = [dict(row) for row in rows[:200]]
        return dict(window_start=start.isoformat(), window_end=end.isoformat(),
                    samples=page, has_more=len(rows)>200,
                    next_after_id=page[-1]['id'] if page else after_id,
                    note='Gemte målinger; manglende perioder kan ikke genskabes.')
    finally:
        conn.close()

def read_source(app_dir, name=None):
    root = Path(app_dir).resolve()
    def file_path(filename):
        path = root / filename
        if path.is_symlink() or path.resolve().parent != root:
            raise ValueError('Ugyldig kodefil')
        return path
    if name is None:
        return {'files': [{'name': filename, 'sha256': hashlib.sha256(file_path(filename).read_bytes()).hexdigest()}
                          for filename in SOURCE_FILES if file_path(filename).is_file()]}
    if name not in SOURCE_FILES:
        raise ValueError('Kodefilen er ikke tilladt')
    with file_path(name).open('rb') as stream:
        payload = stream.read(200001)
    if len(payload)>200000:
        raise ValueError('Kodefilen er for stor')
    return dict(name=name, sha256=hashlib.sha256(payload).hexdigest(), content=payload.decode('utf-8'))
