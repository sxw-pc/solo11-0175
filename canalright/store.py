import hashlib,json,sqlite3
from pathlib import Path
from contextlib import closing

SCHEMA='''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL,seq INTEGER NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS parcel(id TEXT PRIMARY KEY,owner TEXT NOT NULL,season TEXT NOT NULL,area INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS issuer(id TEXT PRIMARY KEY,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,parcel_prefix TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS water_right(id TEXT PRIMARY KEY,parcel_id TEXT NOT NULL REFERENCES parcel(id),quota INTEGER NOT NULL,status TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id),effective_from TEXT NOT NULL DEFAULT '',effective_to TEXT NOT NULL DEFAULT '',issuer_id TEXT NOT NULL DEFAULT 'legacy',realized INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS canal(id TEXT PRIMARY KEY,capacity INTEGER NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS maintenance(id TEXT PRIMARY KEY,canal_id TEXT NOT NULL REFERENCES canal(id),start_at TEXT NOT NULL,end_at TEXT NOT NULL,status TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS rotation(id TEXT PRIMARY KEY,right_id TEXT NOT NULL REFERENCES water_right(id),canal_id TEXT NOT NULL REFERENCES canal(id),start_at TEXT NOT NULL,end_at TEXT NOT NULL,planned INTEGER NOT NULL,priority INTEGER NOT NULL,status TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS gate_reading(id TEXT PRIMARY KEY,rotation_id TEXT NOT NULL REFERENCES rotation(id),gate_id TEXT NOT NULL,start_meter INTEGER NOT NULL,end_meter INTEGER NOT NULL,calibrated INTEGER NOT NULL,witness TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS calibration(id TEXT PRIMARY KEY,gate_id TEXT NOT NULL,valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,technician TEXT NOT NULL,witness TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS delivery(rotation_id TEXT PRIMARY KEY,status TEXT NOT NULL,actual INTEGER NOT NULL,shortage INTEGER NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS compensation(rotation_id TEXT PRIMARY KEY,units INTEGER NOT NULL,responsible TEXT NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS liability_review(rotation_id TEXT PRIMARY KEY,reason TEXT NOT NULL,responsible TEXT NOT NULL,units INTEGER NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,object_type TEXT NOT NULL,object_id TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,evidence_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pump_meter(id TEXT PRIMARY KEY,pump_id TEXT NOT NULL,rate_per_kwh REAL NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id),station_id TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS pump_sample(source_id TEXT PRIMARY KEY REFERENCES evidence(id),meter_id TEXT NOT NULL REFERENCES pump_meter(id),start_at TEXT NOT NULL,end_at TEXT NOT NULL,voltage_rms REAL NOT NULL,current_rms REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pump_bill(meter_id TEXT PRIMARY KEY,estimated_kwh REAL NOT NULL,estimated_charge REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pump_run(run_id TEXT PRIMARY KEY,pump_id TEXT NOT NULL,start_at TEXT NOT NULL,end_at TEXT NOT NULL,source_id TEXT NOT NULL UNIQUE REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS pump_acquisition(acquisition_id TEXT PRIMARY KEY,clock_id TEXT NOT NULL,frequency_hz REAL NOT NULL,sample_rate_hz REAL NOT NULL DEFAULT 0,phase_offset_deg REAL NOT NULL,calibrated INTEGER NOT NULL DEFAULT 0,technician TEXT NOT NULL DEFAULT '',witness TEXT NOT NULL DEFAULT '',valid_from TEXT NOT NULL DEFAULT '',valid_to TEXT NOT NULL DEFAULT '',source_id TEXT NOT NULL UNIQUE REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS pump_waveform(waveform_id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES pump_run(run_id),meter_id TEXT NOT NULL REFERENCES pump_meter(id),pump_id TEXT NOT NULL,acquisition_id TEXT NOT NULL DEFAULT '',clock_id TEXT NOT NULL,phase_offset_deg REAL NOT NULL DEFAULT 0,start_at TEXT NOT NULL,end_at TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL DEFAULT '',real_kwh REAL,lower_kwh REAL,upper_kwh REAL,covered_s REAL NOT NULL DEFAULT 0,sample_count INTEGER NOT NULL DEFAULT 0,missing_phases TEXT NOT NULL DEFAULT '',source_id TEXT NOT NULL UNIQUE REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS pump_total_meter(source_id TEXT PRIMARY KEY REFERENCES evidence(id),total_meter_id TEXT NOT NULL,station_id TEXT NOT NULL,start_at TEXT NOT NULL,end_at TEXT NOT NULL,kwh REAL NOT NULL,rate_per_kwh REAL NOT NULL,signer TEXT NOT NULL,signature TEXT NOT NULL,loss_kwh_lower REAL NOT NULL DEFAULT 0,loss_kwh_upper REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS real_settlement(settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,version INTEGER NOT NULL,scope TEXT NOT NULL,pump_id TEXT NOT NULL DEFAULT '',meter_id TEXT NOT NULL DEFAULT '',basis TEXT NOT NULL,basis_ref TEXT NOT NULL DEFAULT '',status TEXT NOT NULL,kwh REAL,lower_kwh REAL,upper_kwh REAL,charge REAL,lower_charge REAL,upper_charge REAL,rate_per_kwh REAL NOT NULL,total_meter_id TEXT NOT NULL DEFAULT '',loss_kwh_lower REAL NOT NULL DEFAULT 0,loss_kwh_upper REAL NOT NULL DEFAULT 0,source_id TEXT NOT NULL REFERENCES evidence(id),supersedes_id INTEGER,UNIQUE(scope,pump_id,basis,basis_ref,version));
CREATE TABLE IF NOT EXISTS settlement_allocation(settlement_id INTEGER NOT NULL REFERENCES real_settlement(settlement_id),pump_id TEXT NOT NULL,status TEXT NOT NULL,exact_kwh REAL,lower_kwh REAL NOT NULL,upper_kwh REAL NOT NULL,covered_start TEXT NOT NULL DEFAULT '',covered_end TEXT NOT NULL DEFAULT '',PRIMARY KEY(settlement_id,pump_id));
'''

def _migrate(db):
    columns={row[1] for row in db.execute('PRAGMA table_info(pump_meter)')}
    if columns and 'station_id' not in columns:
        db.execute("ALTER TABLE pump_meter ADD COLUMN station_id TEXT NOT NULL DEFAULT ''")

def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);_migrate(db);return db

def read(db,file,kind):
    raw=Path(file).read_bytes();doc=json.loads(raw)
    if doc.get('kind')!=kind or not doc.get('id'):raise ValueError('evidence_kind_or_id')
    digest=hashlib.sha256(raw).hexdigest();old=db.execute('SELECT sha FROM evidence WHERE id=?',(doc['id'],)).fetchone()
    if old:
        if old['sha']!=digest:raise ValueError('evidence_id_conflict')
        return doc,False
    seq=db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM evidence').fetchone()[0]
    db.execute('INSERT INTO evidence VALUES(?,?,?,?,?)',(doc['id'],kind,digest,raw,seq))
    return doc,True

def log(db,kind,key,status,reason,evidence_id):
    db.execute('INSERT INTO decision_log(object_type,object_id,status,reason,evidence_id) VALUES(?,?,?,?,?)',(kind,key,status,reason,evidence_id))

def report(path):
    with closing(connect(path)) as db:
        return {kind:[dict(row) for row in db.execute(sql)] for kind,sql in {
         'issuers':'SELECT * FROM issuer ORDER BY id','rights':'SELECT * FROM water_right ORDER BY id','rotations':'SELECT * FROM rotation ORDER BY id','maintenance':'SELECT * FROM maintenance ORDER BY id',
         'calibrations':'SELECT * FROM calibration ORDER BY id','reviews':'SELECT * FROM liability_review ORDER BY rotation_id',
         'deliveries':'SELECT * FROM delivery ORDER BY rotation_id','compensation':'SELECT * FROM compensation ORDER BY rotation_id',
         'pump_meters':'SELECT * FROM pump_meter ORDER BY id','pump_samples':'SELECT * FROM pump_sample ORDER BY source_id',
         'pump_bills':'SELECT * FROM pump_bill ORDER BY meter_id',
         'pump_runs':'SELECT * FROM pump_run ORDER BY run_id','pump_acquisitions':'SELECT * FROM pump_acquisition ORDER BY acquisition_id',
         'pump_waveforms':'SELECT * FROM pump_waveform ORDER BY waveform_id','pump_total_meters':'SELECT * FROM pump_total_meter ORDER BY total_meter_id',
         'real_settlements':'SELECT * FROM real_settlement ORDER BY settlement_id','settlement_allocations':'SELECT * FROM settlement_allocation ORDER BY settlement_id,pump_id',
         'decisions':'SELECT * FROM decision_log ORDER BY id'}.items()}
