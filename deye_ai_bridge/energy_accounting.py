"""Integrate local-day power samples; never use resetting inverter counters."""
import math
from datetime import datetime, timedelta, timezone


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def positive_area(a, b, seconds):
    """kWh under the positive portion of a linear W trace (exclude exports)."""
    if a >= 0 and b >= 0:
        return (a+b)*.5*seconds/3600000
    if a <= 0 and b <= 0:
        return 0.0
    positive = max(a, b)
    return positive*.5*seconds*(positive/abs(b-a))/3600000


def daily_energy(samples, prices, now, validate_car):
    tz = now.tzinfo
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    first_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    buckets = {}
    cursor = first_utc
    while cursor < end_utc:
        buckets[cursor] = {'time': cursor.astimezone(tz).isoformat(), 'grid_import_kwh': 0.0,
                           'coverage_seconds': 0.0, 'priced_kwh': 0.0, 'cost_dkk': 0.0,
                           'price_dkk_kwh': None}
        cursor += timedelta(hours=1)
    price_map = {}
    for row in prices:
        try:
            stamp = datetime.fromisoformat(row['time'])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=tz)
            value = number(row['price_dkk_kwh'])
            if value is not None:
                price_map[stamp.astimezone(timezone.utc)] = value
        except (KeyError, TypeError, ValueError):
            pass
    # Historical samples retain the import tariff even after today's price feed expires.
    ordered = []
    for row in samples:
        try:
            stamp = datetime.fromisoformat(row['created']).astimezone(timezone.utc)
            ordered.append((stamp, row))
            hour = stamp.replace(minute=0, second=0, microsecond=0)
            p = number(row.get('price_dkk'))
            if p is not None:
                price_map.setdefault(hour, p)
        except (KeyError, TypeError, ValueError):
            pass
    ordered.sort(key=lambda x: x[0])
    total = car = house = unknown = load_seconds = car_seconds = 0.0
    for (ta, a), (tb, b) in zip(ordered, ordered[1:]):
        duration = (tb-ta).total_seconds()
        if not 0 < duration <= 120:
            continue
        begin, stop = max(ta, first_utc), min(tb, end_utc, now_utc)
        if stop <= begin:
            continue
        la, lb = number(a.get('load_w')), number(b.get('load_w'))
        ga, gb = number(a.get('grid_w')), number(b.get('grid_w'))
        ca, cb = validate_car(a), validate_car(b)
        while begin < stop:
            hour = begin.replace(minute=0, second=0, microsecond=0)
            cut = min(stop, hour+timedelta(hours=1))
            seconds = (cut-begin).total_seconds()
            fa, fb = (begin-ta).total_seconds()/duration, (cut-ta).total_seconds()/duration
            if ga is not None and gb is not None:
                amount = positive_area(ga+(gb-ga)*fa, ga+(gb-ga)*fb, seconds)
                bucket = buckets[hour]
                bucket['grid_import_kwh'] += amount
                bucket['coverage_seconds'] += seconds
            if la is not None and lb is not None:
                # Load is nonnegative; validated car power must fit inside the total.
                energy = (max(0,la+(lb-la)*fa)+max(0,la+(lb-la)*fb))*.5*seconds/3600000
                total += energy
                load_seconds += seconds
                if ca is not None and cb is not None:
                    ev = max(0,(ca+(cb-ca)*fa+ca+(cb-ca)*fb)*.5*seconds/3600)
                    ev = min(energy, ev)
                    car += ev
                    house += energy-ev
                    car_seconds += seconds
                else:
                    unknown += energy
            begin = cut
    for hour, bucket in buckets.items():
        bucket['price_dkk_kwh'] = price_map.get(hour)
        if bucket['price_dkk_kwh'] is not None:
            bucket['priced_kwh'] = bucket['grid_import_kwh']
            bucket['cost_dkk'] = bucket['grid_import_kwh']*bucket['price_dkk_kwh']
        else:
            bucket['cost_dkk'] = None
        expected = max(0,min(3600,(now_utc-hour).total_seconds()))
        bucket['coverage_percent'] = round(100*bucket['coverage_seconds']/expected,1) if expected else None
        bucket['status'] = 'future' if not expected else 'missing' if not bucket['coverage_seconds'] else 'partial' if bucket['coverage_seconds'] < expected-120 else 'measured'
    return {'date': start.date().isoformat(), 'total_kwh': round(total,3),
            'house_kwh': round(house,3), 'car_kwh': round(car,3),
            'unallocated_kwh': round(unknown,3),
            'load_coverage_seconds': load_seconds, 'car_coverage_seconds': car_seconds,
            'coverage_percent': round(100*load_seconds/max(1,(now_utc-first_utc).total_seconds()),1),
            'allocation_complete': unknown < .001,
            'hourly_import': list(buckets.values()),
            'grid_import_kwh': round(sum(x['grid_import_kwh'] for x in buckets.values()),3),
            'import_cost_dkk': round(sum(x['cost_dkk'] or 0 for x in buckets.values()),2),
            'unpriced_import_kwh': round(sum(x['grid_import_kwh']-x['priced_kwh'] for x in buckets.values()),3),
            'basis': 'Beregnet fra minutmålinger. Bilandel baseret på valideret Tesla-effekt; ukendt bilandel holdes uafklaret.'}
