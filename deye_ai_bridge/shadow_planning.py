"""Pure, read-only forecasting helpers. No device commands or network calls."""
import math
from datetime import datetime, timedelta


def price_window(prices, now, hours=48):
    end = now + timedelta(hours=hours)
    unique = {}
    for row in prices:
        try:
            stamp = datetime.fromisoformat(row['time'])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=now.tzinfo)
            price = float(row['price_dkk_kwh'])
            if math.isfinite(price) and stamp < end and stamp + timedelta(hours=1) > now:
                unique[stamp] = {'time': stamp, 'price': price}
        except (ValueError, TypeError, KeyError):
            continue
    return [unique[k] for k in sorted(unique)]


def limits(prices, now):
    values = sorted(r['price'] for r in price_window(prices, now))
    if not values:
        return None, None
    return values[max(0, len(values)//4-1)], values[min(len(values)-1, len(values)*3//4)]


def reserve(slots, plan):
    """Cover the maximum accumulated house deficit before a refill opportunity.

    Only 70% of forecast PV is credited. Two successive solar-surplus hours
    count as an opportunity; an isolated forecast peak does not release reserve.
    Missing hourly coverage is not assumed to be free energy.
    """
    capacity = float(plan['battery_capacity_kwh'])
    eta = (float(plan['roundtrip_efficiency_percent'])/100)**0.5
    floor = float(plan['minimum_soc_morning'])
    margin = capacity * float(plan.get('reserve_margin_percent', 15))/100
    deficit = peak = 0.0
    source = None
    cheap = sorted(s['price'] for s in slots)
    cheap = cheap[max(0, len(cheap)//4-1)] if cheap else None
    for i, slot in enumerate(slots):
        if i and slot['time'] - slots[i-1]['time'] > timedelta(hours=1, seconds=1):
            break
        solar_surplus = (slot['pv']*.7 > slot['load'] and i+1 < len(slots)
                         and slots[i+1]['time']-slot['time'] == timedelta(hours=1)
                         and slots[i+1]['pv']*.7 > slots[i+1]['load'])
        cheaper = i > 0 and (slot['price'] < slots[0]['price']*.95 or slot['price'] <= cheap)
        if solar_surplus or cheaper:
            source = {'time': slot['time'].isoformat(), 'kind': 'solar' if solar_surplus else 'grid',
                      'price_dkk_kwh': slot['price']}
            break
        deficit += (slot['load']-slot['pv']*.7)*slot['fraction']
        peak = max(peak, deficit)
    required = max(capacity*floor/100, peak/eta+margin)
    return {'reserve_soc': round(min(100, required/capacity*100), 1),
            'reserve_kwh': round(min(capacity, required), 2),
            'house_need_kwh': round(peak, 2), 'safety_margin_kwh': round(margin, 2),
            'next_energy': source, 'forecast_complete': source is not None,
            'capacity_insufficient': required > capacity,
            'basis': '70 % af solprognosen; 15 % kapacitetsmargin som standard; minimums-SOC respekteres.'}


def charge_schedule(sample, prices, plan, now):
    dh, dm = map(int, plan['tesla_departure_time'].split(':'))
    ah, am = map(int, plan['tesla_arrival_time'].split(':'))
    departure = now.replace(hour=dh, minute=dm, second=0, microsecond=0)
    if departure <= now:
        departure += timedelta(days=1)
    arrival = departure.replace(hour=ah, minute=am)
    if arrival >= departure:
        arrival -= timedelta(days=1)
    start = now if sample.get('tesla_connected') else max(now, arrival)
    soc = sample.get('tesla_soc')
    valid = isinstance(soc, (int, float)) and math.isfinite(soc)
    target = min(80, plan['tesla_target_soc'])
    energy = max(0, (target-soc)*plan['tesla_battery_capacity_kwh']/100) if valid else None
    power = plan['tesla_charge_power_kw']
    duration = energy/(power*.9) if valid else 0
    candidates = []
    for row in price_window(prices, now):
        begin = max(start, row['time'])
        end = min(departure, row['time']+timedelta(hours=1))
        available = (end-begin).total_seconds()/3600
        if available > 0:
            candidates.append({**row, 'begin': begin, 'end': end, 'available': available})
    coverage = sum(r['available'] for r in candidates)
    complete = coverage >= max(0, (departure-start).total_seconds()/3600)-.001
    remaining = duration
    selected = []
    # Partial price coverage must never be advertised as the cheapest overnight plan.
    if complete and valid:
        for row in sorted(candidates, key=lambda r: (r['price'], r['begin'])):
            if remaining <= .000001:
                break
            used = min(remaining, row['available'])
            selected.append({'time': row['begin'].isoformat(),
                             'end': (row['begin']+timedelta(hours=used)).isoformat(),
                             'price_dkk_kwh': row['price'], 'hours': used})
            remaining -= used
    selected.sort(key=lambda r: r['time'])
    fallback = max(start, departure-timedelta(hours=duration))
    plug = datetime.fromisoformat(selected[0]['time']) if selected else fallback
    status = 'unavailable' if not valid else 'complete' if energy == 0 else 'waiting_prices' if not complete else 'insufficient_time' if remaining > .001 else 'ready'
    return {'soc': soc, 'target_soc': target, 'energy_needed_kwh': round(energy, 2) if valid else None,
            'estimated_hours': round(duration, 2), 'charge_power_kw': power,
            'departure': departure.isoformat(), 'arrival': arrival.isoformat(),
            'plug_in_by': plug.isoformat(), 'price_hours_available': len(candidates),
            'prices_complete': complete, 'status': status, 'selected_hours': selected}
