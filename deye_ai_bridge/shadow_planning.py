"""Pure, read-only forecasting helpers. No device commands or network calls."""
import math
import json
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
    # Weekend availability extends through the whole day, including Friday night.
    weekend = now.weekday() in (5,6) or departure.weekday() in (5,6)
    if weekend:
        day = now if now.weekday() in (5,6) else departure
        departure = (day+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        arrival = day.replace(hour=0,minute=0,second=0,microsecond=0) if now.weekday() in (5,6) else now.replace(hour=ah,minute=am,second=0,microsecond=0)
    else:
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


def economic_dispatch(slots, plan, initial_soc):
    """Minimize import cost across all contiguous known hours, without exports.

    Battery energy is discretized at 0.25 kWh (including exact bounds and initial
    energy). Every transition conserves energy including charge/discharge loss.
    Unknown price gaps end the horizon; they are never treated as cheap hours.
    """
    if not slots:
        return []
    horizon=[]
    for slot in slots:
        if horizon and slot['time']-horizon[-1]['time'] != timedelta(hours=1):
            break
        horizon.append(slot)
    capacity=float(plan['battery_capacity_kwh'])
    eta=(float(plan['roundtrip_efficiency_percent'])/100)**.5
    floor=capacity*float(plan['minimum_soc_morning'])/100
    ceiling=capacity*float(plan['target_soc'])/100
    initial=max(0., min(capacity, float(initial_soc)*capacity/100))
    # Initial energy above the configured target may still be used, never topped up.
    upper=max(ceiling,initial)
    lower=min(floor,initial)
    levels=sorted(set([round(lower,8),round(floor,8),round(ceiling,8),round(initial,8)] +
                      [round(i*.25,8) for i in range(math.ceil(lower/.25),math.floor(upper/.25)+1)]))
    initial_index=min(range(len(levels)),key=lambda j:abs(levels[j]-initial))
    # Load margin protects against forecast error; PV credit stays at 70 percent.
    margin=1+float(plan.get('reserve_margin_percent',15))/100
    costs={initial_index:0.}
    parents=[]
    for slot in horizon:
        fraction=float(slot['fraction'])
        deficit=(float(slot['load'])*margin-float(slot['pv'])*.7)*fraction
        charge_limit=float(plan['max_charge_w'])/1000*fraction*eta
        discharge_limit=min(float(plan['max_discharge_w'])/1000*fraction,max(0.,deficit))/eta
        next_costs={};back={}
        for i,cost in costs.items():
            before=levels[i]
            for j,after in enumerate(levels):
                change=after-before
                if change>charge_limit+1e-8 or -change>discharge_limit+1e-8:
                    continue
                if change>1e-8 and after>ceiling+1e-8:
                    continue
                if change < -1e-8 and after<floor-1e-8:
                    continue
                ac=change/eta if change>=0 else change*eta
                imported=max(0.,deficit+ac)
                # Small throughput cost avoids pointless cycling at equal prices.
                total=cost+imported*float(slot['price'])+abs(change)*.02
                if total<next_costs.get(j,float('inf'))-1e-9:
                    next_costs[j]=total;back[j]=i
        parents.append(back);costs=next_costs
    end=min(costs,key=lambda j:(costs[j]-.03*levels[j],levels[j]))
    path=[end]
    for back in reversed(parents):
        end=back[end];path.append(end)
    path.reverse()
    rows=[]
    for k,slot in enumerate(horizon):
        before,after=levels[path[k]],levels[path[k+1]]
        change=after-before;fraction=float(slot['fraction'])
        deficit=(float(slot['load'])*margin-float(slot['pv'])*.7)*fraction
        ac=change/eta if change>=0 else change*eta
        grid_charge=change>1e-7 and deficit+ac>max(0.,deficit)+1e-7
        action=('grid_charge' if grid_charge else 'solar_charge') if change>1e-7 else ('use_battery' if change< -1e-7 else 'hold')
        reason={'grid_charge':'Lad i de billigste rentable timer i hele den kendte prisperiode til kommende husforbrug.',
                'solar_charge':'Gem forventet soloverskud til senere husforbrug.',
                'use_battery':'Brug batteriet nu ifølge samlet prisplan; energi til dyrere timer er afsat.',
                'hold':'Gem batteriet til de timer, hvor det sparer mest.'}[action]
        # Reserve the planned consumption until the next scheduled refill.
        need=0.
        for m in range(k+1,len(horizon)):
            delta=levels[path[m+1]]-levels[path[m]]
            if delta>1e-7:break
            need+=max(0.,-delta)
        protected=min(upper,floor+need)/capacity*100
        target=after/capacity*100
        rows.append({'time':slot['time'].isoformat(),'price_dkk_kwh':round(slot['price'],4),
                     'pv_forecast_kwh':round(slot['pv']*fraction,2),
                     'load_forecast_kwh':round(slot['load']*fraction,2),
                     'action':action,'charge_target_soc':round(target,1),
                     'discharge_floor_soc':round(protected,1),
                     'battery_power_w':round(-ac/max(fraction,.001)*1000),
                     'projected_soc':round(target,1),'reserve_soc':round(protected,1),
                     'reason':reason})
    return rows


def power_limited_plan(plan, sample):
    """Forecast with the same current ceilings as the actual controller."""
    limited=dict(plan)
    try:
        raw=json.loads(sample.get('telemetry_json') or '{}').get('raw_registers',{})
        voltage=float(raw.get('587',5000))/100
        if not 40<=voltage<=60:voltage=50.
        charge=float(raw.get('212',190));discharge=float(raw.get('213',190))
        if not 0<=charge<=500:charge=0
        if not 0<=discharge<=500:discharge=0
    except (ValueError,TypeError,AttributeError):
        voltage,charge,discharge=50.,190.,190.
    limited['max_charge_w']=min(float(plan['max_charge_w']),min(190,charge)*voltage)
    limited['max_discharge_w']=min(float(plan['max_discharge_w']),min(190,discharge)*voltage)
    return limited


def planning_weather(weather, updated, error, now):
    """Never credit stale/failed solar forecasts. Prices and SOC stay independent."""
    try:
        age=now.timestamp()-float(updated or 0)
        hourly=(weather or {}).get('hourly',{})
        useful=False
        for at,value in zip(hourly.get('time',[]),hourly.get('global_tilted_irradiance',[])):
            dt=datetime.fromisoformat(at)
            if dt.tzinfo is None:dt=dt.replace(tzinfo=now.tzinfo)
            if dt>=now.replace(minute=0,second=0,microsecond=0) and value is not None and math.isfinite(float(value)) and float(value)>=0:
                useful=True;break
        valid=not error and 0<=age<=7200 and useful
    except (ValueError,TypeError,AttributeError):valid=False
    if valid:return weather,{'weather_mode':'forecast','weather_notice':''}
    return {},{'weather_mode':'zero_solar','weather_notice':'Solprognosen er utilgængelig eller for gammel. Planen regner med 0 kWh sol og genberegnes fra faktisk SOC og aktuelle priser.'}
