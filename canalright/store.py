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
CREATE TABLE IF NOT EXISTS stage_gauge(id TEXT PRIMARY KEY,canal_id TEXT NOT NULL,bank TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS stage_reading(source_id TEXT PRIMARY KEY REFERENCES evidence(id),gauge_id TEXT NOT NULL REFERENCES stage_gauge(id),at TEXT NOT NULL,height_m REAL NOT NULL,uploaded_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS flood_warning(canal_id TEXT PRIMARY KEY,status TEXT NOT NULL,stage_m REAL NOT NULL,reading_id TEXT NOT NULL REFERENCES stage_reading(source_id));
CREATE TABLE IF NOT EXISTS stage_site(id TEXT PRIMARY KEY,gauge_id TEXT NOT NULL REFERENCES stage_gauge(id),crest_m REAL NOT NULL,max_slew_m_per_min REAL NOT NULL,probe_bandwidth_hz REAL NOT NULL,sample_clock TEXT NOT NULL,upload_clock TEXT NOT NULL,buffer_signer TEXT NOT NULL,surveyed_by TEXT NOT NULL,witness TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS stage_outage(id TEXT PRIMARY KEY,site_id TEXT NOT NULL REFERENCES stage_site(id),start_at TEXT NOT NULL,end_at TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS stage_buffer_page(id TEXT PRIMARY KEY,site_id TEXT NOT NULL REFERENCES stage_site(id),seq INTEGER NOT NULL,start_at TEXT NOT NULL,end_at TEXT NOT NULL,cadence_sec INTEGER NOT NULL,signed_by TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS stage_buffer_sample(page_id TEXT NOT NULL REFERENCES stage_buffer_page(id),at TEXT NOT NULL,height_m REAL NOT NULL,PRIMARY KEY(page_id,at));
CREATE TABLE IF NOT EXISTS crest_mark(id TEXT PRIMARY KEY,site_id TEXT NOT NULL REFERENCES stage_site(id),mark_m REAL NOT NULL,window_start TEXT NOT NULL,window_end TEXT NOT NULL,sealed_by TEXT NOT NULL,witness TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS stage_caldev(id TEXT PRIMARY KEY,site_id TEXT NOT NULL REFERENCES stage_site(id),valid_from TEXT NOT NULL,valid_to TEXT NOT NULL,max_offset_m REAL NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS overtop_publication(site_id TEXT NOT NULL REFERENCES stage_site(id),version INTEGER NOT NULL,evidence_seq INTEGER NOT NULL,windows TEXT NOT NULL,events_lo INTEGER NOT NULL,events_hi INTEGER,closures TEXT NOT NULL,pending TEXT NOT NULL,reopen_at TEXT,sha TEXT NOT NULL,raw BLOB NOT NULL,trigger_id TEXT NOT NULL,PRIMARY KEY(site_id,version));
'''

def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db

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
         'gauges':'SELECT * FROM stage_gauge ORDER BY id','stage_readings':'SELECT * FROM stage_reading ORDER BY source_id',
         'flood_warnings':'SELECT * FROM flood_warning ORDER BY canal_id',
         'stage_sites':'SELECT * FROM stage_site ORDER BY id','stage_outages':'SELECT * FROM stage_outage ORDER BY id',
         'stage_buffers':'SELECT * FROM stage_buffer_page ORDER BY id','stage_buffer_samples':'SELECT * FROM stage_buffer_sample ORDER BY page_id,at',
         'crest_marks':'SELECT * FROM crest_mark ORDER BY id','stage_caldevs':'SELECT * FROM stage_caldev ORDER BY id',
         'overtop_publications':'SELECT site_id,version,evidence_seq,windows,events_lo,events_hi,closures,pending,reopen_at,sha,trigger_id FROM overtop_publication ORDER BY site_id,version',
         'decisions':'SELECT * FROM decision_log ORDER BY id'}.items()}
