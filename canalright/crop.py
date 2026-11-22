"""Crop intake decisions.

Two columns are kept deliberately separate:

* ``crop_advice`` is the provisional conductivity-only staging state written
  from an EC page (``hold`` at EC >= 1000).  It reflects total conductive
  contribution, never the harmful sodium fraction by itself.
* ``crop_window`` is the final per-parcel, per-sampling-window suitability,
  only confirmed by same-parcel, same-window ion evidence.  Lab results that
  arrive later recompute just the windows they cover.
"""
from . import ion_bounds
from .store import log


def advise(db, site_id):
    """Provisional advice from EC alone; unchanged staging behaviour."""
    row = db.execute('SELECT * FROM quality_reading WHERE site_id=? ORDER BY at DESC LIMIT 1',
                     (site_id,)).fetchone()
    status = ion_bounds.ec_provisional(row['ec_us'])
    db.execute('INSERT INTO crop_advice VALUES(?,?,?) ON CONFLICT(site_id) DO UPDATE SET status=excluded.status,ec_us=excluded.ec_us',
               (site_id, status, row['ec_us']))
    return {'site_id': site_id, 'status': status}


def _pending(db, sample, reason):
    db.execute('INSERT INTO pending_bottle VALUES(?,?,?,?,?,?) ON CONFLICT(site_id,parcel_id,bottle_id) DO UPDATE SET reason=excluded.reason,window_start=excluded.window_start,window_end=excluded.window_end',
               (sample['site_id'], sample['parcel_id'], sample['bottle_id'],
                sample['window_start'], sample['window_end'], reason))


def _insert_window(db, sample, start, end, ratio_lo, ratio_hi, basis, source_id):
    crop = db.execute('SELECT crop FROM quality_site WHERE id=?', (sample['site_id'],)).fetchone()['crop']
    status = ion_bounds.decide(ratio_lo, ratio_hi, crop)
    db.execute('INSERT INTO crop_window VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(site_id,parcel_id,window_start,window_end) DO UPDATE SET status=excluded.status,ratio_lo=excluded.ratio_lo,ratio_hi=excluded.ratio_hi,basis=excluded.basis,source_id=excluded.source_id',
               (sample['site_id'], sample['parcel_id'], start, end, status,
                ratio_lo, ratio_hi, basis, source_id))
    log(db, 'crop_window',
        f"{sample['site_id']}:{sample['parcel_id']}:{start}/{end}",
        status, basis, source_id)
    return status


def recompute_parcel(db, site_id, parcel_id, source_id):
    """Recompute only the windows of one historical/current water parcel.

    Evidence from another parcel or another day's water never touches these
    rows: every ion result and electrode patrol is matched on parcel id, and
    every segment stays inside the original sampling window.
    """
    samples = db.execute('SELECT * FROM quality_sample WHERE site_id=? AND parcel_id=? ORDER BY window_start',
                         (site_id, parcel_id)).fetchall()
    for sample in samples:
        key = (sample['site_id'], sample['parcel_id'], sample['window_start'], sample['window_end'])
        db.execute('DELETE FROM crop_window WHERE site_id=? AND parcel_id=? AND window_start=? AND window_end=?', key)
        db.execute('DELETE FROM pending_bottle WHERE site_id=? AND parcel_id=? AND bottle_id=?',
                   (sample['site_id'], sample['parcel_id'], sample['bottle_id']))
        s, e = sample['window_start'], sample['window_end']

        chrom = db.execute("SELECT * FROM ion_result WHERE site_id=? AND parcel_id=? AND bottle_id=? AND method='chromatography'",
                           (site_id, parcel_id, sample['bottle_id'])).fetchone()
        if chrom:
            _insert_window(db, sample, s, e, chrom['ratio'], chrom['ratio'],
                           'ion_chromatography', chrom['source_id'])
            continue

        patrols = db.execute('SELECT * FROM electrode_patrol WHERE site_id=? AND parcel_id=?',
                             (site_id, parcel_id)).fetchall()
        covered = []
        for patrol in patrols:
            intersection = ion_bounds.window_intersection(s, e, patrol['cover_start'], patrol['cover_end'])
            if not intersection:
                continue
            points = [r['ratio'] for r in db.execute(
                'SELECT ratio FROM electrode_point WHERE source_id=? AND at>=? AND at<=? ORDER BY at',
                (patrol['source_id'], intersection[0], intersection[1]))]
            if not points:
                continue
            lo, hi = ion_bounds.electrode_range(points)
            _insert_window(db, sample, intersection[0], intersection[1], lo, hi,
                           'sodium_electrode_patrol', patrol['source_id'])
            covered.append(intersection)

        if covered:
            covered.sort()
            cursor = s
            for c_start, c_end in covered:
                if cursor < c_start:
                    _insert_window(db, sample, cursor, c_start, 0.0, 1.0,
                                   'ec_only_no_ion_coverage', source_id)
                    _pending(db, sample, 'electrode_gap_needs_recheck')
                cursor = max(cursor, c_end)
            if cursor < e:
                _insert_window(db, sample, cursor, e, 0.0, 1.0,
                               'ec_only_no_ion_coverage', source_id)
                _pending(db, sample, 'electrode_gap_needs_recheck')
            continue

        lo, hi = ion_bounds.ec_only_range()
        _insert_window(db, sample, s, e, lo, hi, 'ec_only_provisional', source_id)
        reason = 'seal_broken_historical_parcel' if not sample['seal_intact'] else 'bottle_awaiting_ion_lab'
        _pending(db, sample, reason)
