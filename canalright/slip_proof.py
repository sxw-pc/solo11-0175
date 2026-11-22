"""Structural displacement verdict for an embankment segment.

The raw pages only ever satisfy one equation per reading::

    y(t) = s(t) + k * (T(t) - T0)

where ``s`` is the real wall rotation and ``k`` is the unknown thermal
coefficient of the shared steel bracket batch. A single page pair cannot solve
for both unknowns, so this module never reports a point estimate: it keeps the
*feasible interval* of structural rotation given the declared bounds on ``k``
and publishes one of three per-segment verdicts:

* ``proven_displacement`` — every feasible value reaches 0.5 degrees;
* ``thermal_only``        — every feasible value stays below 0.5 degrees and
  no independent anchor defect is flagged;
* ``pending_review``      — the interval straddles 0.5 degrees or proof is
  missing, so no closure may be issued and sensors must be revisited.

Two supplementary evidence routes are handled inside the same rules:
bedrock-anchored total station observations (proves one segment's wall
rotation directly) and oven calibration of spare brackets from the same
material lot (pins down the batch coefficient shared across every segment).
Reviews and repair certificates are versioned separately from the raw
readings; old notices are never deleted.
"""
from contextlib import closing
from .store import connect, read, log

LIMIT_DEG = 0.5
ROUNDED = 9


def _r(value):
    value = round(value, ROUNDED)
    return 0.0 if value == 0 else value


# ---------------------------------------------------------------- evidence --
def import_thermal_batch(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bank_thermal_batch')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        k_min = float(doc.get('k_min', 0.0))
        k_max = float(doc.get('k_max', 0.1))
        if not doc.get('batch_id') or not doc.get('material_lot') or not 0 <= k_min <= k_max:
            raise ValueError('invalid_thermal_batch')
        if db.execute('SELECT 1 FROM bank_thermal_batch WHERE batch_id=?',
                      (doc['batch_id'],)).fetchone():
            raise ValueError('batch_already_registered')
        db.execute('INSERT INTO bank_thermal_batch VALUES(?,?,?,?,?,?)',
                   (doc['batch_id'], doc['material_lot'], k_min, k_max, 0, doc['id']))
        log(db, 'bank_thermal_batch', doc['batch_id'], 'registered',
            f"material_lot={doc['material_lot']};k=[{k_min},{k_max}]", doc['id'])
        return {'batch_id': doc['batch_id'], 'k_min': k_min, 'k_max': k_max}


def import_bracket(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bank_bracket')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not db.execute('SELECT 1 FROM bank_sensor WHERE id=?',
                          (doc['sensor_id'],)).fetchone():
            raise ValueError('unknown_sensor')
        if not db.execute('SELECT 1 FROM bank_thermal_batch WHERE batch_id=?',
                          (doc['batch_id'],)).fetchone():
            raise ValueError('unknown_thermal_batch')
        db.execute('INSERT INTO bank_bracket VALUES(?,?,?)',
                   (doc['sensor_id'], doc['batch_id'], doc['id']))
        return {'sensor_id': doc['sensor_id'], 'batch_id': doc['batch_id']}


def import_temperature(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bank_temperature')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not db.execute('SELECT 1 FROM bank_sensor WHERE id=?',
                          (doc['sensor_id'],)).fetchone():
            raise ValueError('unknown_sensor')
        db.execute('INSERT INTO bank_temperature VALUES(?,?,?,?)',
                   (doc['id'], doc['sensor_id'], doc['at'], float(doc['temperature_c'])))
        return {'sensor_id': doc['sensor_id'], 'at': doc['at'],
                'temperature_c': float(doc['temperature_c'])}


def import_benchmark(path, file):
    """Register a bedrock benchmark. Flood-disturbed marks cannot witness slip."""
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bank_benchmark')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        bedrock = 1 if doc.get('bedrock_anchored') else 0
        disturbed = 1 if doc.get('flood_disturbed') else 0
        db.execute('INSERT INTO bank_benchmark VALUES(?,?,?,?)',
                   (doc['mark_id'], bedrock, disturbed, doc['id']))
        return {'mark_id': doc['mark_id'], 'bedrock_anchored': bool(bedrock),
                'flood_disturbed': bool(disturbed)}


def import_total_station(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bank_total_station')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        mark = db.execute('SELECT * FROM bank_benchmark WHERE mark_id=?',
                          (doc['mark_id'],)).fetchone()
        if mark is None:
            raise ValueError('unknown_benchmark')
        if not mark['bedrock_anchored']:
            raise ValueError('benchmark_not_bedrock_anchored')
        if mark['flood_disturbed']:
            # A flood-moved datum cannot attribute its own movement to the wall.
            raise ValueError('benchmark_flood_disturbed')
        db.execute('INSERT INTO bank_total_station VALUES(?,?,?,?,?)',
                   (doc['id'], doc['segment'], doc['mark_id'], doc['at'],
                    float(doc['wall_rotation_deg'])))
        log(db, 'bank_total_station', doc['segment'], 'observed',
            f"rotation={doc['wall_rotation_deg']}deg@{doc['at']}", doc['id'])
        return {'segment': doc['segment'], 'wall_rotation_deg': float(doc['wall_rotation_deg'])}


def import_oven_calibration(path, file):
    """Pin down one batch coefficient; the result applies to every segment."""
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bank_oven_calibration')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        batch = db.execute('SELECT * FROM bank_thermal_batch WHERE batch_id=?',
                           (doc['batch_id'],)).fetchone()
        if batch is None:
            raise ValueError('unknown_thermal_batch')
        k = float(doc['k_deg_per_c'])
        points = int(doc.get('points', 1))
        if points < 2 or not 0 <= k <= 0.1 or doc.get('technician') == doc.get('witness'):
            raise ValueError('invalid_calibration_proof')
        db.execute('INSERT INTO bank_oven_calibration VALUES(?,?,?,?,?,?)',
                   (doc['id'], doc['batch_id'], k, points,
                    doc['technician'], doc['witness']))
        db.execute('UPDATE bank_thermal_batch SET k_min=?,k_max=?,calibrated=1 WHERE batch_id=?',
                   (k, k, doc['batch_id']))
        log(db, 'bank_thermal_batch', doc['batch_id'], 'calibrated',
            f'shared_k={k};multi_point={points}', doc['id'])
        return {'batch_id': doc['batch_id'], 'k_deg_per_c': k, 'calibrated': True}


def import_photo(path, file):
    """Crack photos are catalogued evidence; a photo alone cannot date a crack."""
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bank_photo')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not doc.get('taken_at') or not doc.get('subject'):
            raise ValueError('invalid_photo')
        db.execute('INSERT INTO bank_photo VALUES(?,?,?,?)',
                   (doc['id'], doc['segment'], doc['taken_at'], doc['subject']))
        return {'segment': doc['segment'], 'subject': doc['subject']}


def import_field_flag(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bank_field_flag')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if doc['flag'] not in ('anchor_loose', 'anchor_intact'):
            raise ValueError('invalid_field_flag')
        if not db.execute('SELECT 1 FROM bank_sensor WHERE id=?',
                          (doc['sensor_id'],)).fetchone():
            raise ValueError('unknown_sensor')
        db.execute('INSERT INTO bank_field_flag VALUES(?,?,?,?,?)',
                   (doc['id'], doc['segment'], doc['sensor_id'], doc['flag'], doc['noted_at']))
        log(db, 'bank_field_flag', doc['sensor_id'], doc['flag'],
            'independent_anchor_inspection', doc['id'])
        return {'sensor_id': doc['sensor_id'], 'flag': doc['flag']}


# ------------------------------------------------------------------- review --
def _latest_pair(db, sensor_id):
    tilt = db.execute('SELECT * FROM bank_tilt WHERE sensor_id=? ORDER BY at DESC LIMIT 1',
                      (sensor_id,)).fetchone()
    if tilt is None:
        return None
    temp = db.execute('SELECT * FROM bank_temperature WHERE sensor_id=? AND at<=? ORDER BY at DESC LIMIT 1',
                      (sensor_id, tilt['at'])).fetchone()
    zero = db.execute('SELECT * FROM bank_sensor_zero WHERE sensor_id=?',
                      (sensor_id,)).fetchone()
    if temp is None or zero is None:
        return None
    baseline = db.execute('SELECT * FROM bank_tilt WHERE sensor_id=? AND at<=? ORDER BY at DESC LIMIT 1',
                          (sensor_id, zero['zeroed_at'])).fetchone()
    y0 = baseline['tilt_deg'] if baseline is not None else 0.0
    t0 = zero['ref_temperature_c']
    return {'at': tilt['at'], 'delta_y': tilt['tilt_deg'] - y0,
            'delta_t': temp['temperature_c'] - t0, 'tilt_deg': tilt['tilt_deg']}


def _feasible_rotation(db, sensor_id):
    """Feasible [s_min, s_max] of real wall rotation, plus proof metadata."""
    pair = _latest_pair(db, sensor_id)
    if pair is None:
        return None
    bracket = db.execute('SELECT * FROM bank_bracket WHERE sensor_id=?',
                         (sensor_id,)).fetchone()
    if bracket is None:
        batch = None
    else:
        batch = db.execute('SELECT * FROM bank_thermal_batch WHERE batch_id=?',
                           (bracket['batch_id'],)).fetchone()
    if batch is None:
        # No batch linkage means thermal behaviour is wholly unconstrained.
        lo, hi = pair['delta_y'] - 0.1 * max(pair['delta_t'], 0.0), pair['delta_y']
        k_note = 'k_unconstrained'
    else:
        lo = pair['delta_y'] - batch['k_max'] * pair['delta_t']
        hi = pair['delta_y'] - batch['k_min'] * pair['delta_t']
        if lo > hi:
            lo, hi = hi, lo
        k_note = f"k=[{batch['k_min']},{batch['k_max']}]"
        if batch['calibrated']:
            k_note += ';oven_calibrated'
    return {'sensor_id': sensor_id, 'at': pair['at'], 'tilt_deg': pair['tilt_deg'],
            's_min': _r(lo), 's_max': _r(hi),
            'delta_t': pair['delta_t'], 'k_note': k_note,
            'batch_id': bracket['batch_id'] if bracket is not None else None}


def review_segment(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bank_slip_review')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        segment = doc['segment']
        sensors = [r['id'] for r in db.execute(
            'SELECT id FROM bank_sensor WHERE segment=? ORDER BY id', (segment,)).fetchall()]
        if not sensors:
            raise ValueError('unknown_segment')

        per_sensor, revisit, s_lo, s_hi, anchor_defect = [], [], None, None, False
        for sensor_id in sensors:
            bound = _feasible_rotation(db, sensor_id)
            if bound is None:
                revisit.append(sensor_id)
                continue
            per_sensor.append(bound)
            s_lo = bound['s_min'] if s_lo is None else min(s_lo, bound['s_min'])
            s_hi = bound['s_max'] if s_hi is None else max(s_hi, bound['s_max'])
            flag = db.execute("SELECT * FROM bank_field_flag WHERE sensor_id=? ORDER BY noted_at DESC LIMIT 1",
                              (sensor_id,)).fetchone()
            if flag is not None and flag['flag'] == 'anchor_loose':
                anchor_defect = True
                revisit.append(sensor_id)

        # Route 1: bedrock total station measures the wall rotation directly.
        ts = db.execute('''SELECT ts.* FROM bank_total_station ts JOIN bank_benchmark b ON b.mark_id=ts.mark_id
            WHERE ts.segment=? AND b.bedrock_anchored=1 AND b.flood_disturbed=0
            ORDER BY ts.at DESC LIMIT 1''', (segment,)).fetchone()
        basis, reason = [], []
        if ts is not None:
            s_lo = s_hi = _r(ts['wall_rotation_deg'])
            basis.append(f"total_station:{ts['source_id']}@{ts['at']}")
            reason.append('bedrock_total_station_wall_rotation')
        else:
            reason.append('feasible_interval_from_y=s+k*dT')
        for bound in per_sensor:
            basis.append(f"{bound['sensor_id']}:s=[{bound['s_min']},{bound['s_max']}];{bound['k_note']}")
        if any(b['batch_id'] for b in per_sensor):
            basis.append('batch_coefficient_shared_across_segments')
        if anchor_defect:
            reason.append('independent_anchor_loose_field_flag')

        if s_lo is None:
            status = 'pending_review'
            reason = ['no_usable_tilt_temperature_zero_pages']
        elif ts is not None:
            status = 'proven_displacement' if abs(ts['wall_rotation_deg']) >= LIMIT_DEG else 'thermal_only'
        elif anchor_defect:
            # Thermal subtraction cannot be trusted while the anchor moves on its own.
            status = 'pending_review'
        elif s_lo >= LIMIT_DEG or s_hi <= -LIMIT_DEG:
            status = 'proven_displacement'
        elif abs(s_hi) < LIMIT_DEG and abs(s_lo) < LIMIT_DEG:
            status = 'thermal_only'
        else:
            status = 'pending_review'
            reason.append('interval_straddles_0.5deg_threshold')

        length_m = int(doc.get('segment_length_m', 0))
        closed_length = length_m if status == 'proven_displacement' else 0
        if status != 'proven_displacement':
            for bound in per_sensor:
                if bound['sensor_id'] not in revisit and status == 'pending_review':
                    revisit.append(bound['sensor_id'])
        revisit = sorted(set(revisit))

        previous = db.execute('SELECT COALESCE(MAX(version),0)+1 AS v FROM bank_slip_review WHERE segment=?',
                              (segment,)).fetchone()['v']
        db.execute('INSERT INTO bank_slip_review VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (segment, previous, status, s_lo if s_lo is not None else 0.0,
                    s_hi if s_hi is not None else 0.0, closed_length,
                    ','.join(revisit), ';'.join(reason), ';'.join(basis),
                    doc['id'], doc.get('reviewed_at', '')))
        log(db, 'bank_slip_review', segment, status, ';'.join(reason), doc['id'])
        return {'segment': segment, 'version': previous, 'status': status,
                's_min': s_lo, 's_max': s_hi, 'closed_length_m': closed_length,
                'revisit_sensors': revisit}


def issue_repair_certificate(path, file):
    """A repair certificate may only be drawn on a proven displacement review."""
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bank_repair_certificate')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        review = db.execute('''SELECT * FROM bank_slip_review WHERE segment=?
            ORDER BY version DESC LIMIT 1''', (doc['segment'],)).fetchone()
        if review is None or review['status'] != 'proven_displacement':
            raise ValueError('no_proven_displacement_review')
        length_m = int(doc.get('length_m', review['closed_length_m']))
        if length_m <= 0 or length_m > review['closed_length_m']:
            raise ValueError('certificate_length_outside_review')
        db.execute('INSERT INTO bank_repair_certificate VALUES(?,?,?,?,?,?)',
                   (doc['cert_id'], doc['segment'], length_m,
                    review['source_id'], 'issued', doc['id']))
        log(db, 'bank_repair_certificate', doc['cert_id'], 'issued',
            f"segment={doc['segment']};length={length_m}", doc['id'])
        return {'cert_id': doc['cert_id'], 'segment': doc['segment'],
                'length_m': length_m, 'status': 'issued'}
