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
CREATE TABLE IF NOT EXISTS quality_site(id TEXT PRIMARY KEY,canal_id TEXT NOT NULL,crop TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS quality_reading(source_id TEXT PRIMARY KEY REFERENCES evidence(id),site_id TEXT NOT NULL REFERENCES quality_site(id),at TEXT NOT NULL,ec_us REAL NOT NULL);
CREATE TABLE IF NOT EXISTS crop_advice(site_id TEXT PRIMARY KEY,status TEXT NOT NULL,ec_us REAL NOT NULL);
-- Ion evidence chain: each sample is one sealed bottle drawing one water
-- parcel during one mixing window.  The reading row stays the EC staging state.
CREATE TABLE IF NOT EXISTS quality_sample(source_id TEXT PRIMARY KEY REFERENCES quality_reading(source_id),site_id TEXT NOT NULL REFERENCES quality_site(id),parcel_id TEXT NOT NULL,bottle_id TEXT NOT NULL,volume_ml REAL NOT NULL,seal_intact INTEGER NOT NULL,batch_id TEXT NOT NULL REFERENCES ion_calibration_batch(id),window_start TEXT NOT NULL,window_end TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ion_calibration_batch(id TEXT PRIMARY KEY,standard_na REAL NOT NULL,standard_ca REAL NOT NULL,observed_ec REAL NOT NULL,drift REAL NOT NULL,source_id TEXT NOT NULL REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS ion_result(source_id TEXT PRIMARY KEY REFERENCES evidence(id),site_id TEXT NOT NULL,parcel_id TEXT NOT NULL,bottle_id TEXT NOT NULL,batch_id TEXT NOT NULL,method TEXT NOT NULL,na REAL NOT NULL,ca REAL NOT NULL,ratio REAL NOT NULL,seal_checked INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS electrode_patrol(source_id TEXT PRIMARY KEY REFERENCES evidence(id),site_id TEXT NOT NULL,parcel_id TEXT NOT NULL,batch_id TEXT NOT NULL,cover_start TEXT NOT NULL,cover_end TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS electrode_point(source_id TEXT NOT NULL REFERENCES electrode_patrol(source_id),at TEXT NOT NULL,na REAL NOT NULL,ca REAL NOT NULL,ratio REAL NOT NULL,PRIMARY KEY(source_id,at));
-- Final, per-parcel, per-window crop suitability.  Kept apart from the
-- conductivity-only crop_advice staging column; later lab evidence only
-- replaces the windows it actually covers.
CREATE TABLE IF NOT EXISTS crop_window(site_id TEXT NOT NULL,parcel_id TEXT NOT NULL,window_start TEXT NOT NULL,window_end TEXT NOT NULL,status TEXT NOT NULL,ratio_lo REAL NOT NULL,ratio_hi REAL NOT NULL,basis TEXT NOT NULL,source_id TEXT NOT NULL,PRIMARY KEY(site_id,parcel_id,window_start,window_end));
CREATE TABLE IF NOT EXISTS pending_bottle(site_id TEXT NOT NULL,parcel_id TEXT NOT NULL,bottle_id TEXT NOT NULL,window_start TEXT NOT NULL,window_end TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(site_id,parcel_id,bottle_id));
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

def read_doc(db,doc,kind):
    if doc.get('kind')!=kind or not doc.get('id'):raise ValueError('evidence_kind_or_id')
    raw=json.dumps(doc,sort_keys=True,ensure_ascii=False).encode('utf8')
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
         'quality_sites':'SELECT * FROM quality_site ORDER BY id','quality_readings':'SELECT * FROM quality_reading ORDER BY source_id',
         'quality_samples':'SELECT * FROM quality_sample ORDER BY source_id','ion_batches':'SELECT * FROM ion_calibration_batch ORDER BY id',
         'ion_results':'SELECT * FROM ion_result ORDER BY source_id','electrode_patrols':'SELECT * FROM electrode_patrol ORDER BY source_id',
         'electrode_points':'SELECT * FROM electrode_point ORDER BY source_id,at','crop_windows':'SELECT * FROM crop_window ORDER BY site_id,parcel_id,window_start',
         'pending_bottles':'SELECT * FROM pending_bottle ORDER BY site_id,parcel_id,bottle_id',
         'crop_advice':'SELECT * FROM crop_advice ORDER BY site_id',
         'decisions':'SELECT * FROM decision_log ORDER BY id'}.items()}
