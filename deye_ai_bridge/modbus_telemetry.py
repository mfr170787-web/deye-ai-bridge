"""Read-only interpretation of Deye V105-family registers, with raw evidence."""
from datetime import datetime


REGISTERS = {
    62: ('Inverterur år/måned (rå)', '', 1, False),
    63: ('Inverterur dag/time (rå)', '', 1, False),
    64: ('Inverterur minut/sekund (rå)', '', 1, False),
    108: ('Indstillet maksimal batteriladestrøm', 'A', 1, False),
    109: ('Indstillet maksimal batteriafladestrøm', 'A', 1, False),
    111: ('Batteristyring efter spænding/SOC (rå)', '', 1, False),
    115: ('Batteri Shutdown-SOC', '%', 1, False),
    116: ('Batteri Restart-SOC', '%', 1, False),
    117: ('Batteri LowBatt-SOC', '%', 1, False),
    127: ('Start-SOC for netopladning', '%', 1, False),
    128: ('Indstillet batteriladestrøm fra net', 'A', 1, False),
    130: ('Netopladning aktiveret (rå)', '', 1, False),
    143: ('Maksimal salgseffekt til net (LV)', 'W', 1, False),
    212: ('BMS rapporteret ladestrømgrænse', 'A', 1, False),
    213: ('BMS rapporteret afladestrømgrænse', 'A', 1, False),
    214: ('BMS rapporteret SOC', '%', 1, False),
    215: ('BMS rapporteret spænding (LV)', 'V', .01, False),
    216: ('BMS rapporteret strøm', 'A', 1, True),
    217: ('BMS temperatur med offset (rå)', '', 1, False),
    259: ('EV-ladetilstand (rå bitfelt, ikke verificeret)', '', 1, False),
    260: ('EV-effektgrænse (rå, skala ikke verificeret)', '', 1, False),
    1100: ('Remote mode (0 = deaktiveret)', '', 1, False),
    1101: ('Remote watchdog (65535 = deaktiveret)', 's', 1, False),
    1104: ('Remote kontrolside (0 AC, 1 batteri, 2 net)', '', 1, False),
    1105: ('Remote batteristyringsform (2 = effekt)', '', 1, False),
    1109: ('Remote batteri-effektsetpunkt', '%', .1, True),
    1110: ('Remote husbatteri-SOC-setpunkt', '%', .1, False),
}
for low, name in [(516,'Batteri opladet total'),(518,'Batteri afladet total'),
                  (522,'Netimport total'),(524,'Neteksport total'),(527,'Forbrug total')]:
    REGISTERS[low] = (name+' lavt ord (rå)', '', 1, False)
    REGISTERS[low+1] = (name+' højt ord (rå)', '', 1, False)
for i in range(6):
    REGISTERS[148+i] = (f'TOU periode {i+1} start HHMM', '', 1, False)
    REGISTERS[154+i] = (f'TOU periode {i+1} batteriafladeeffekt (LV)', 'W', 1, False)
    REGISTERS[160+i] = (f'TOU periode {i+1} batterimålspænding', 'V', .01, False)
    REGISTERS[166+i] = (f'TOU periode {i+1} SOC', '%', 1, False)
    REGISTERS[172+i] = (f'TOU periode {i+1} net/gen-opladning bitfelt', '', 1, False)


def decode(raw, clock_reference=None, errors=None):
    def get(address):
        value = raw.get(address)
        return value if isinstance(value,int) and 0 <= value <= 65535 else None
    def scalar(address, scale=1):
        v = get(address)
        return None if v in (None,65535) else round(v*scale,3)
    def counter(low):
        a,b=get(low),get(low+1)
        # A low word of 65535 is valid in a 32-bit counter.
        return None if a is None or b is None or (a==65535 and b==65535) else round(((b<<16)|a)*.1,1)
    clock,offset,clock_error=None,None,None
    try:
        a,b,c=(get(i) for i in (62,63,64))
        if None in (a,b,c) or 65535 in (a,b,c):
            raise ValueError('Urregistre mangler eller er ugyldige.')
        stamp=datetime(2000+(a>>8),a&255,b>>8,b&255,c>>8,c&255)
        clock=stamp.isoformat()
        if clock_reference is not None:
            offset=round((stamp-clock_reference.replace(tzinfo=None)).total_seconds(),1)
    except (TypeError,ValueError) as exc:
        clock_error=str(exc)
    voltage=scalar(587,.01)
    charge=scalar(212)
    discharge=scalar(213)
    configured_charge=scalar(108)
    configured_discharge=scalar(109)
    grid_charge=scalar(128)
    def power(*amps):
        if voltage is None or any(a is None for a in amps):
            return None
        return round(voltage*min(amps))
    wd=get(1101)
    return {
        'raw_registers': {str(k):v for k,v in sorted(raw.items())},
        'read_errors': errors or [],
        'clock': {'inverter_local':clock,'reference_local':clock_reference.isoformat() if clock_reference else None,
                  'offset_seconds':offset,'warning':offset is not None and abs(offset)>120,
                  'error':clock_error,'basis':'Inverterens ur fortolkes som dansk lokal tid; ingen tidskorrektion udføres.'},
        'remote': {'mode':scalar(1100),'watchdog_raw':wd,
                   'watchdog_disabled':None if wd is None else wd==65535,
                   'watchdog_seconds':None if wd==65535 else wd,
                   'control_side':scalar(1104),'battery_control_mode':scalar(1105),
                   'power_setpoint_percent':None if get(1109) is None else round((get(1109)-65536 if get(1109)>32767 else get(1109))*.1,1),
                   'soc_setpoint_percent':scalar(1110,.1),
                   'write_support_verified':False},
        'battery': {'configured_charge_a':configured_charge,'configured_discharge_a':configured_discharge,
                    'grid_charge_a':grid_charge,'bms_charge_limit_a':charge,'bms_discharge_limit_a':discharge,
                    'shutdown_soc':scalar(115),'restart_soc':scalar(116),'low_battery_soc':scalar(117),
                    'grid_charge_start_soc':scalar(127),'grid_charge_enabled_raw':get(130),
                    'bms_soc':scalar(214),'bms_voltage_v':scalar(215,.01),
                    'bms_temperature_c':None if scalar(217) is None else round((get(217)-1000)*.1,1),
                    'estimated_charge_limit_w':power(charge,configured_charge),
                    'estimated_grid_charge_limit_w':power(charge,configured_charge,grid_charge),
                    'estimated_discharge_limit_w':power(discharge,configured_discharge),
                    'note':'V × laveste strømgrænse; estimat før tab. BMS og firmware skal fortsat verificeres.'},
        'totals_kwh': {'battery_charge':counter(516),'battery_discharge':counter(518),
                       'grid_import':counter(522),'grid_export':counter(524),'load':counter(527)},
        'ev': {'mode_raw':get(259),'max_power_raw':get(260),'scale_verified':False},
        'read_only':True,
    }
