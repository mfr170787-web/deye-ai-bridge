"""Persistent invoice check. Counter-based energy, UTC keys and explicit gaps."""
import calendar
import io
import json
import math
import re
import sqlite3
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Europe/Copenhagen')
UTC=timezone.utc

def number(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError,ValueError):return None

def timestamp(s):return datetime.fromisoformat(s.replace('Z','+00:00')).astimezone(UTC)

def bounds(month):
    if not re.fullmatch(r'20\d\d-(0[1-9]|1[0-2])',month):raise ValueError('Ugyldig måned')
    year,mon=map(int,month.split('-'))
    start=datetime(year,mon,1,tzinfo=TZ)
    end=datetime(year+(mon==12),1 if mon==12 else mon+1,1,tzinfo=TZ)
    return start.astimezone(UTC),end.astimezone(UTC)

class MonthlyRegister:
    def __init__(self,path):
        self.path=Path(path);self.lock=threading.RLock()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.archive=self.path.parent/'monthly_reports';self.archive.mkdir(exist_ok=True)
        with self.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS points(t REAL PRIMARY KEY, counter REAL, total_price REAL);
            CREATE TABLE IF NOT EXISTS prices(t REAL PRIMARY KEY, body TEXT);
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);''')
    def connect(self):return sqlite3.connect(self.path,timeout=30)
    def points(self,samples):
        rows=[]
        for s in samples:
            try:
                telemetry=s.get('telemetry_json') or '{}'
                if isinstance(telemetry,str):telemetry=json.loads(telemetry)
                counter=number(telemetry.get('totals_kwh',{}).get('grid_import'))
                if counter is None:continue
                rows.append((timestamp(s['created']).timestamp(),counter,number(s.get('price_dkk'))))
            except (KeyError,ValueError,TypeError):continue
        with self.connect() as c:c.executemany('INSERT OR IGNORE INTO points VALUES(?,?,?)',rows)
    def ingest(self,sample,prices):
        with self.lock:
            self.points([sample])
            with self.connect() as c:
                for p in prices:
                    if p.get('source')!='Andel FlexEnergi + N1 og afgifter':continue
                    c.execute('INSERT OR REPLACE INTO prices VALUES(?,?)',(timestamp(p['time']).timestamp(),json.dumps(p,ensure_ascii=False)))
            self.finalize()
    def backfill(self,samples):
        with self.lock:self.points(samples)
    def months(self):
        with self.connect() as c:lo,hi=c.execute('SELECT min(t),max(t) FROM points').fetchone()
        if lo is None:return []
        cursor=datetime.fromtimestamp(lo,TZ).replace(day=1,hour=0,minute=0,second=0,microsecond=0)
        last=datetime.fromtimestamp(hi,TZ).strftime('%Y-%m');out=[]
        while cursor.strftime('%Y-%m')<=last:
            month=cursor.strftime('%Y-%m');out.append({'month':month,'archived':(self.archive/(month+'.xlsx')).exists()})
            cursor=(cursor.replace(day=28)+timedelta(days=4)).replace(day=1)
        return out[::-1]
    def report(self,month,now=None):
        start,end=bounds(month);now=now or datetime.now(UTC);rows={};t=start
        while t<end:
            rows[t.timestamp()]={'time':t.astimezone(TZ).isoformat(),'kwh':0.,'coverage_seconds':0.,'energy_price':None,'grid_tariff':None,'other_tariffs':None,'total_price':None,'source':'Pris mangler'}
            t+=timedelta(hours=1)
        with self.connect() as c:
            pts=c.execute('SELECT t,counter,total_price FROM points WHERE t>=? AND t<=? ORDER BY t',(start.timestamp()-180,end.timestamp()+180)).fetchall()
            prices={t:json.loads(body) for t,body in c.execute('SELECT t,body FROM prices WHERE t>=? AND t<?',(start.timestamp(),end.timestamp()))}
        for key,r in rows.items():
            p=prices.get(key)
            if p:
                r.update(energy_price=number(p.get('energy_price_dkk_kwh')),grid_tariff=number(p.get('grid_tariff_dkk_kwh')),total_price=number(p.get('price_dkk_kwh')),source=p['source'])
                if all(r[k] is not None for k in ('energy_price','grid_tariff','total_price')):r['other_tariffs']=r['total_price']-r['energy_price']-r['grid_tariff']
        for a,b in zip(pts,pts[1:]):
            seconds=b[0]-a[0];delta=b[1]-a[1]
            if not 0<seconds<=180 or not 0<=delta<=.1+30*seconds/3600:continue
            cursor=max(a[0],start.timestamp());stop=min(b[0],end.timestamp())
            while cursor<stop:
                key=math.floor(cursor/3600)*3600;finish=min(stop,key+3600)
                if key in rows:
                    r=rows[key];r['kwh']+=delta*(finish-cursor)/seconds;r['coverage_seconds']+=finish-cursor
                    # Historical samples retain the combined price only. Never invent its components.
                    if r['total_price'] is None and a[2] is not None and math.floor(a[0]/3600)*3600==key:
                        r['total_price']=a[2];r['source']='Gemt samlet Bridge-timepris; prisdele mangler'
                cursor=finish
        for r in rows.values():
            future=timestamp(r['time'])>=now
            r['status']='Fremtid' if future else 'Ingen målinger' if not r['coverage_seconds'] else 'Delvis dækning' if r['coverage_seconds']<3599 else 'Målt'
            if not future and r['total_price'] is None:r['status']+='; pris mangler'
            r['cost']=r['kwh']*r['total_price'] if r['total_price'] is not None and r['coverage_seconds'] else None
            r['energy_cost']=r['kwh']*r['energy_price'] if r['energy_price'] is not None and r['coverage_seconds'] else None
        return {'month':month,'created':datetime.now(UTC).isoformat(),'hours':list(rows.values()),'kwh':sum(r['kwh'] for r in rows.values()),'known_cost':sum(r['cost'] or 0 for r in rows.values()),'missing_hours':sum(r['status']!='Målt' for r in rows.values()),'method':'Deye kumulativt netkøb. Fordeling mellem timegrænser er estimeret tidsmæssigt. Huller over 180 sekunder og tællernulstilling medregnes ikke. Ikke afregningsmåleren. Priser er gemte Bridge-priser; tarif- og aftaletillæg skal kontrolleres mod aftalen. Moms inkluderet.'}
    def finalize(self,now=None):
        now=now or datetime.now(UTC)
        for item in self.months():
            month=item['month'];start,end=bounds(month)
            # Give late boundary observations a day; archives are never overwritten.
            if item['archived'] or now<end+timedelta(days=1):continue
            report=self.report(month,now);payload=excel(report)
            for suffix,data in [('.json',json.dumps(report,ensure_ascii=False).encode()),('.xlsx',payload)]:
                dest=self.archive/(month+suffix);temp=dest.with_suffix(suffix+'.tmp');temp.write_bytes(data);temp.replace(dest)
    def download(self,month):
        bounds(month)
        with self.lock:
            path=self.archive/(month+'.xlsx')
            return path.read_bytes() if path.exists() else excel(self.report(month))


def excel(report):
    """Small dependency-free XLSX exporter for the Pi; numeric cells and formulas."""
    hourly=[['Time (dansk tid og UTC-offset)','Netkøb kWh','Dækning minutter','FlexEnergi kr./kWh','Nettarif kr./kWh','Øvrig transport og afgift kr./kWh','Samlet kr./kWh','FlexEnergi kr.','Variabel udgift kr.','Datastatus','Priskilde']]
    for i,r in enumerate(report['hours'],2):
        hourly.append([r['time'],r['kwh'] if r['coverage_seconds'] else None,r['coverage_seconds']/60,r['energy_price'],r['grid_tariff'],r['other_tariffs'],r['total_price'],('formula',f'IF(OR(B{i}="",D{i}=""),"",B{i}*D{i})'),('formula',f'IF(OR(B{i}="",G{i}=""),"",B{i}*G{i})'),r['status'],r['source']])
    n=len(hourly)
    summary=[['Elregnskab',report['month']],['Oprettet',report['created']],['Metode',report['method']],['Status','Ufuldstændigt datagrundlag' if report['missing_hours'] else 'Alle timer dækket'],['Timer med manglende/delvise data',report['missing_hours']],['Registreret netkøb kWh',('formula',f'SUM(Timer!B2:B{n})')],['FlexEnergi, kendte prisdele kr.',('formula',f'SUM(Timer!H2:H{n})')],['Variabel udgift, kendte timer kr.',('formula',f'SUM(Timer!I2:I{n})')],['Andel abonnement kr. (indtast for perioden)',None],['Netabonnement kr. (indtast)',None],['Energinet abonnement kr. (indtast)',None],['Betalingsgebyr kr. (indtast)',None],['Samlet inkl. indtastede faste poster kr.',('formula','SUM(B8:B12)')],['Faktura netkøb kWh (indtast)',None],['Forskel registreret minus faktura kWh',('formula','IF(B14="","",B6-B14)')],['Faktura total kr. (indtast)',None],['Forskel kendt beregning minus faktura kr.',('formula','IF(B16="","",B13-B16)')],['Bemærkning','Tomme faste poster er ikke inkluderet. Prisdele kan mangle ved historisk import. En difference er ikke i sig selv en fakturafejl. Salg til nettet er ikke modregnet.']]
    def col(i):
        out=''
        while i:i,k=divmod(i-1,26);out=chr(65+k)+out
        return out
    def sheet(data):
        out=[]
        for i,row in enumerate(data,1):
            cells=[]
            for j,v in enumerate(row,1):
                ref=f'{col(j)}{i}';style=1 if i==1 else 2 if isinstance(v,(int,float,tuple)) else 0
                if v is None:continue
                if isinstance(v,tuple):cell=f'<c r="{ref}" s="2"><f>{escape(v[1])}</f></c>'
                elif isinstance(v,(int,float)):cell=f'<c r="{ref}" s="{style}"><v>{v}</v></c>'
                else:cell=f'<c r="{ref}" s="{style}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>'
                cells.append(cell)
            out.append(f'<row r="{i}">'+''.join(cells)+'</row>')
        return '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="1" width="42" customWidth="1"/><col min="2" max="11" width="24" customWidth="1"/></cols><sheetData>'+''.join(out)+'</sheetData></worksheet>'
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in (1,2))+'</Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Overblik" sheetId="1" r:id="rId1"/><sheet name="Timer" sheetId="2" r:id="rId2"/></sheets><calcPr fullCalcOnLoad="1"/></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in (1,2))+'<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml','<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="3"><xf fontId="0"/><xf fontId="1"/><xf numFmtId="4" fontId="0" applyNumberFormat="1"/></cellXfs></styleSheet>')
        for i,data in enumerate((summary,hourly),1):z.writestr(f'xl/worksheets/sheet{i}.xml',sheet(data))
    return out.getvalue()
