"""Capture canal intake evidence: EC pages, sealed bottles and ion analyses.

Two evidence routes supplement a conductivity page:

* ``ion_analysis`` — ion chromatography on the retained, seal-intact original
  bottle.  It pins the Na/Ca of that *historical* water parcel and can
  back-sign the parcel's original sampling window.  A broken seal cannot
  restore the old water; the bottle stays pending.
* ``electrode_patrol`` — continuous sodium-selective-electrode survey of the
  *next* round's parcel.  Each point is cross-checked against EC and the
  shared calibration-batch drift; it certifies only the covered intersection
  of a sampling window, never a full day, and never an old parcel.

Only Na and Ca analyses of the same parcel and same sampling window may
cross-constrain EC, and meters of one calibration batch share a single
baseline-drift parameter (``ion_bounds``).
"""
import json
from contextlib import closing
from .store import connect, read, read_doc
from . import ion_bounds
from .crop import advise, recompute_parcel


def _require_sample_shape(doc):
    for key in ('parcel_id', 'bottle_id', 'volume_ml', 'seal_intact',
                'batch_id', 'window_start', 'window_end'):
        if key not in doc:
            raise ValueError('missing_sample_chain')
    if not doc['parcel_id'] or not doc['bottle_id'] or doc['volume_ml'] <= 0:
        raise ValueError('invalid_sample')
    if doc['window_start'] >= doc['window_end']:
        raise ValueError('invalid_mixing_window')
    if not (doc['window_start'] <= doc['at'] <= doc['window_end']):
        raise ValueError('reading_outside_mixing_window')


def _batch_joint_drift(db, batch_id):
    """Drift interval still feasible for every chromatographed reading."""
    intervals = []
    for row in db.execute("""SELECT qr.ec_us, ir.na, ir.ca
                             FROM ion_result ir
                             JOIN quality_sample qs ON qs.bottle_id = ir.bottle_id
                             JOIN quality_reading qr ON qr.source_id = qs.source_id
                             WHERE ir.batch_id=? AND ir.method='chromatography'""",
                          (batch_id,)):
        intervals.append(ion_bounds.ion_drift_interval(row['ec_us'], row['na'], row['ca']))
    return ion_bounds.intersect_intervals(intervals) if intervals else None


def _parcel_windows(db, site_id, parcel_id):
    return [dict(r) for r in db.execute(
        'SELECT window_start,window_end,status,ratio_lo,ratio_hi,basis,source_id '
        'FROM crop_window WHERE site_id=? AND parcel_id=? ORDER BY window_start',
        (site_id, parcel_id))]


def _pending(db, site_id, parcel_id):
    return [dict(r) for r in db.execute(
        'SELECT bottle_id,window_start,window_end,reason FROM pending_bottle '
        'WHERE site_id=? AND parcel_id=? ORDER BY bottle_id', (site_id, parcel_id))]


def _ingest_site(db, doc):
    if not doc['site_id'] or not doc['crop']:
        raise ValueError('invalid_site')
    db.execute('INSERT INTO quality_site VALUES(?,?,?,?)',
               (doc['site_id'], doc['canal_id'], doc['crop'], doc['id']))
    return {'site_id': doc['site_id']}


def _ingest_batch(db, doc):
    na, ca, observed = doc['standard_na'], doc['standard_ca'], doc['observed_ec']
    if na < 0 or ca < 0 or (na + ca) <= 0 or observed < 0:
        raise ValueError('invalid_calibration_fluid')
    if db.execute('SELECT 1 FROM ion_calibration_batch WHERE id=?', (doc['batch_id'],)).fetchone():
        raise ValueError('batch_id_conflict')
    drift = ion_bounds.batch_drift(na + ca, observed)
    db.execute('INSERT INTO ion_calibration_batch VALUES(?,?,?,?,?,?)',
               (doc['batch_id'], na, ca, observed, drift, doc['id']))
    return {'batch_id': doc['batch_id'], 'drift': drift}


def _ingest_ec(db, doc):
    site = db.execute('SELECT * FROM quality_site WHERE id=?', (doc['site_id'],)).fetchone()
    if site is None or doc['ec_us'] < 0:
        raise ValueError('invalid_ec')
    sample_keys = ('parcel_id', 'bottle_id', 'volume_ml', 'seal_intact',
                   'batch_id', 'window_start', 'window_end')
    if not all(k in doc for k in sample_keys):
        # Legacy conductivity-only page: only the provisional staging column.
        db.execute('INSERT INTO quality_reading VALUES(?,?,?,?)',
                   (doc['id'], doc['site_id'], doc['at'], doc['ec_us']))
        return advise(db, doc['site_id'])
    _require_sample_shape(doc)
    batch = db.execute('SELECT * FROM ion_calibration_batch WHERE id=?', (doc['batch_id'],)).fetchone()
    if batch is None:
        raise ValueError('unknown_calibration_batch')
    if db.execute('SELECT 1 FROM quality_sample WHERE parcel_id=? AND window_start=? AND window_end=?',
                  (doc['parcel_id'], doc['window_start'], doc['window_end'])).fetchone():
        raise ValueError('parcel_window_conflict')
    if db.execute('SELECT 1 FROM quality_sample WHERE bottle_id=?', (doc['bottle_id'],)).fetchone():
        raise ValueError('bottle_id_conflict')
    # A neighbouring day's new water may never stand in for an old parcel.
    if db.execute('SELECT 1 FROM quality_sample WHERE site_id=? AND parcel_id=?',
                  (doc['site_id'], doc['parcel_id'])).fetchone():
        raise ValueError('parcel_reuse_forbidden')
    db.execute('INSERT INTO quality_reading VALUES(?,?,?,?)',
               (doc['id'], doc['site_id'], doc['at'], doc['ec_us']))
    db.execute('INSERT INTO quality_sample VALUES(?,?,?,?,?,?,?,?,?)',
               (doc['id'], doc['site_id'], doc['parcel_id'], doc['bottle_id'],
                doc['volume_ml'], 1 if doc['seal_intact'] else 0, doc['batch_id'],
                doc['window_start'], doc['window_end']))
    # Already-accepted ion evidence must keep sharing one batch drift.
    joint = _batch_joint_drift(db, doc['batch_id'])
    if joint is not None and not ion_bounds.drift_supported(batch['drift'], joint):
        raise ValueError('shared_batch_drift_incompatible')
    recompute_parcel(db, doc['site_id'], doc['parcel_id'], doc['id'])
    provisional = advise(db, doc['site_id'])
    return {'site_id': doc['site_id'], 'parcel_id': doc['parcel_id'],
            'ec_status': provisional['status'],
            'windows': _parcel_windows(db, doc['site_id'], doc['parcel_id']),
            'pending_bottles': _pending(db, doc['site_id'], doc['parcel_id'])}


def _ingest_ion(db, doc):
    sample = db.execute('SELECT * FROM quality_sample WHERE bottle_id=?', (doc['bottle_id'],)).fetchone()
    if sample is None or sample['parcel_id'] != doc['parcel_id'] or sample['site_id'] != doc['site_id']:
        raise ValueError('bottle_not_linked_to_parcel')
    if doc.get('method', 'chromatography') != 'chromatography':
        raise ValueError('invalid_ion_method')
    if not doc.get('seal_intact') or not sample['seal_intact']:
        raise ValueError('seal_broken_historical_parcel')
    if db.execute('SELECT 1 FROM ion_result WHERE bottle_id=?', (doc['bottle_id'],)).fetchone():
        raise ValueError('ion_result_conflict')
    if doc['na'] < 0 or doc['ca'] < 0 or (doc['na'] + doc['ca']) <= 0:
        raise ValueError('invalid_ion_composition')
    batch = db.execute('SELECT * FROM ion_calibration_batch WHERE id=?', (sample['batch_id'],)).fetchone()
    reading = db.execute('SELECT * FROM quality_reading WHERE source_id=?', (sample['source_id'],)).fetchone()
    if not ion_bounds.ion_matches_ec(reading['ec_us'], doc['na'], doc['ca'], batch['drift']):
        raise ValueError('ion_ec_crosscheck_failed')
    # Every meter of the batch must keep one shared feasible drift parameter.
    intervals = [ion_bounds.ion_drift_interval(reading['ec_us'], doc['na'], doc['ca'])]
    for other in db.execute("""SELECT qr.ec_us, ir.na, ir.ca FROM ion_result ir
                               JOIN quality_sample qs ON qs.bottle_id = ir.bottle_id
                               JOIN quality_reading qr ON qr.source_id = qs.source_id
                               WHERE ir.batch_id=? AND ir.method='chromatography'""",
                            (sample['batch_id'],)):
        intervals.append(ion_bounds.ion_drift_interval(other['ec_us'], other['na'], other['ca']))
    if ion_bounds.intersect_intervals(intervals) is None:
        raise ValueError('shared_batch_drift_incompatible')
    ratio = ion_bounds.sodium_ratio(doc['na'], doc['ca'])
    db.execute('INSERT INTO ion_result VALUES(?,?,?,?,?,?,?,?,?,?)',
               (doc['id'], doc['site_id'], doc['parcel_id'], doc['bottle_id'],
                sample['batch_id'], 'chromatography', doc['na'], doc['ca'], ratio, 1))
    recompute_parcel(db, doc['site_id'], doc['parcel_id'], doc['id'])
    return {'site_id': doc['site_id'], 'parcel_id': doc['parcel_id'], 'ratio': ratio,
            'windows': _parcel_windows(db, doc['site_id'], doc['parcel_id']),
            'pending_bottles': _pending(db, doc['site_id'], doc['parcel_id'])}


def _ingest_electrode(db, doc):
    if doc['cover_start'] >= doc['cover_end'] or not doc.get('points'):
        raise ValueError('invalid_patrol_coverage')
    batch = db.execute('SELECT * FROM ion_calibration_batch WHERE id=?', (doc['batch_id'],)).fetchone()
    if batch is None:
        raise ValueError('unknown_calibration_batch')
    if db.execute('SELECT 1 FROM electrode_patrol WHERE site_id=? AND parcel_id=? AND cover_start=? AND cover_end=?',
                  (doc['site_id'], doc['parcel_id'], doc['cover_start'], doc['cover_end'])).fetchone():
        raise ValueError('patrol_conflict')
    points = sorted(doc['points'], key=lambda p: p['at'])
    if any(points[i]['at'] >= points[i + 1]['at'] for i in range(len(points) - 1)):
        raise ValueError('invalid_patrol_points')
    verified = []
    for point in points:
        if not (doc['cover_start'] <= point['at'] <= doc['cover_end']):
            raise ValueError('point_outside_patrol_coverage')
        # Mixing-window boundary: an instantaneous point must land inside a
        # same-parcel sample window; it can never be stretched to a full day.
        sample = db.execute('SELECT * FROM quality_sample WHERE site_id=? AND parcel_id=? '
                            'AND window_start<=? AND window_end>=? ORDER BY window_start LIMIT 1',
                            (doc['site_id'], doc['parcel_id'], point['at'], point['at'])).fetchone()
        if sample is None:
            raise ValueError('point_outside_mixing_window')
        reading = db.execute('SELECT * FROM quality_reading WHERE source_id=?', (sample['source_id'],)).fetchone()
        ca, ratio = ion_bounds.electrode_cross_check(reading['ec_us'], point['na'], batch['drift'])
        verified.append((point['at'], point['na'], ca, ratio))
    db.execute('INSERT INTO electrode_patrol VALUES(?,?,?,?,?,?)',
               (doc['id'], doc['site_id'], doc['parcel_id'], doc['batch_id'],
                doc['cover_start'], doc['cover_end']))
    db.executemany('INSERT INTO electrode_point VALUES(?,?,?,?,?)',
                   [(doc['id'], at, na, ca, ratio) for at, na, ca, ratio in verified])
    recompute_parcel(db, doc['site_id'], doc['parcel_id'], doc['id'])
    return {'site_id': doc['site_id'], 'parcel_id': doc['parcel_id'],
            'windows': _parcel_windows(db, doc['site_id'], doc['parcel_id']),
            'pending_bottles': _pending(db, doc['site_id'], doc['parcel_id'])}


_INGEST = {
    'quality_site': _ingest_site,
    'ion_calibration_batch': _ingest_batch,
    'quality_ec': _ingest_ec,
    'ion_analysis': _ingest_ion,
    'electrode_patrol': _ingest_electrode,
}


def _import_one(path, file, kind):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, kind)
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        return _INGEST[kind](db, doc)


def import_site(path, file):
    return _import_one(path, file, 'quality_site')


def import_ion_batch(path, file):
    return _import_one(path, file, 'ion_calibration_batch')


def import_ec(path, file):
    return _import_one(path, file, 'quality_ec')


def import_ion(path, file):
    return _import_one(path, file, 'ion_analysis')


def import_electrode(path, file):
    return _import_one(path, file, 'electrode_patrol')


def import_bundle(path, file):
    """Ingest an ordered array of evidence documents inside one transaction.

    Every document is stored with its own original bytes and SHA (the bundle
    is a replay convenience, not a merged evidence item), so audit replay and
    idempotent re-import behave exactly like single-file imports.
    """
    with open(file, 'rb') as handle:
        docs = json.loads(handle.read())
    if not isinstance(docs, list) or not docs:
        raise ValueError('invalid_bundle')
    with closing(connect(path)) as db, db:
        applied = []
        for doc in docs:
            kind = doc.get('kind')
            if kind not in _INGEST:
                raise ValueError('evidence_kind_or_id')
            _, fresh = read_doc(db, doc, kind)
            if not fresh:
                applied.append({'id': doc['id'], 'replayed': True})
            else:
                applied.append(_INGEST[kind](db, doc))
        return {'imported': len(docs), 'results': applied}
