"""Pump electrical evidence: RMS summaries, run windows and paired waveforms."""
import hashlib
import json
import math
from contextlib import closing
from datetime import datetime

from .store import connect, read

# A total-meter signature is the provider's detached seal over these fields.
# Offline we can only verify that the seal matches the bytes supplied; the
# utility keeps the real signing key in production.
TOTAL_METER_FIELDS = ('total_meter_id', 'station_id', 'start_at', 'end_at',
                      'kwh', 'rate_per_kwh', 'signer', 'loss_kwh_lower',
                      'loss_kwh_upper')


REJECT = {'unknown_run', 'unknown_meter', 'run_pump_mismatch',
          'clock_conflict', 'phase_offset_conflict'}


class EvidenceError(ValueError):
    """The document itself is malformed or contradicts its provenance.

    These reject the import (and roll it back).  Physical quality problems
    such as a missing phase, saturation or clock drift are different: the
    capture is stored as invalid evidence so settlement can see that the run
    is unmeasured, and it is never filled with zero power.
    """


def total_meter_signature(doc):
    payload = json.dumps({k: doc[k] for k in TOTAL_METER_FIELDS},
                         sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf8')).hexdigest()


def _instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _hours(start_at, end_at):
    return (_instant(end_at) - _instant(start_at)).total_seconds() / 3600


def import_meter(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'pump_meter')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not doc['pump_id'] or doc['rate_per_kwh'] < 0:
            raise ValueError('invalid_meter')
        if db.execute('SELECT 1 FROM pump_meter WHERE id=?', (doc['meter_id'],)).fetchone():
            raise ValueError('meter_conflict')
        db.execute('INSERT INTO pump_meter VALUES(?,?,?,?,?)',
                   (doc['meter_id'], doc['pump_id'], doc['rate_per_kwh'], doc['id'],
                    doc.get('station_id', '')))
        return {'meter_id': doc['meter_id']}


def import_sample(path, file):
    """Store an RMS page and refresh the provisional (apparent-power) bill.

    The page has no voltage/current pairing or phase, so the result is only a
    provisional estimate; it is never treated as a real-energy invoice.
    """
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'pump_sample')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        meter = db.execute('SELECT * FROM pump_meter WHERE id=?', (doc['meter_id'],)).fetchone()
        if meter is None or doc['start_at'] >= doc['end_at'] or doc['voltage_rms'] < 0 or doc['current_rms'] < 0:
            raise ValueError('invalid_sample')
        db.execute('INSERT INTO pump_sample VALUES(?,?,?,?,?,?)',
                   (doc['id'], doc['meter_id'], doc['start_at'], doc['end_at'],
                    doc['voltage_rms'], doc['current_rms']))
        from .tariff import estimate
        return estimate(db, doc['meter_id'])


def import_run(path, file):
    """Register a pump on/off window; real energy is only billed inside it."""
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'pump_run')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if (not doc['run_id'] or not doc['pump_id']
                or doc['start_at'] >= doc['end_at']):
            raise ValueError('invalid_run')
        if not db.execute('SELECT 1 FROM pump_meter WHERE pump_id=?',
                          (doc['pump_id'],)).fetchone():
            raise ValueError('unknown_pump')
        if db.execute('SELECT 1 FROM pump_run WHERE run_id=?', (doc['run_id'],)).fetchone():
            raise ValueError('run_conflict')
        db.execute('INSERT INTO pump_run VALUES(?,?,?,?,?)',
                   (doc['run_id'], doc['pump_id'], doc['start_at'], doc['end_at'], doc['id']))
        return {'run_id': doc['run_id'], 'pump_id': doc['pump_id']}


def import_acquisition(path, file):
    """Register a collector and its common phase/time basis.

    Every waveform taken by one acquisition during a run shares that
    acquisition's phase offset; it cannot be retuned per sample or per second.
    """
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'pump_acquisition')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if (not doc['acquisition_id'] or not doc['clock_id']
                or doc['frequency_hz'] <= 0
                or doc.get('sample_rate_hz', 0) <= 0
                or not -180 <= doc['phase_offset_deg'] <= 180):
            raise ValueError('invalid_acquisition')
        if doc['calibrated']:
            if (not doc['valid_from'] or doc['valid_from'] >= doc['valid_to']
                    or not doc['technician']
                    or doc['technician'] == doc['witness']):
                raise ValueError('invalid_clock_proof')
        if db.execute('SELECT 1 FROM pump_acquisition WHERE acquisition_id=?',
                      (doc['acquisition_id'],)).fetchone():
            raise ValueError('acquisition_conflict')
        # A clock has one physical phase bias.  Registering a second collector
        # on the same clock with a different bias would let the same grid
        # samples be re-read under a new offset.
        other = db.execute(
            'SELECT acquisition_id, phase_offset_deg FROM pump_acquisition '
            'WHERE clock_id=? AND ABS(phase_offset_deg-?)>1e-9',
            (doc['clock_id'], doc['phase_offset_deg'])).fetchone()
        if other:
            raise ValueError('clock_phase_conflict')
        db.execute('INSERT INTO pump_acquisition VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (doc['acquisition_id'], doc['clock_id'], doc['frequency_hz'],
                    doc['sample_rate_hz'], doc['phase_offset_deg'],
                    1 if doc['calibrated'] else 0,
                    doc.get('technician', ''), doc.get('witness', ''),
                    doc.get('valid_from', ''), doc.get('valid_to', ''), doc['id']))
        return {'acquisition_id': doc['acquisition_id'], 'clock_id': doc['clock_id']}


def _pair(sample):
    """Return ([(v, i), ...], phase_count) or raise for an unusable pairing."""
    voltage, current = sample.get('voltage'), sample.get('current')
    if isinstance(voltage, list) or isinstance(current, list):
        if not isinstance(voltage, list) or not isinstance(current, list):
            raise ValueError('unpaired_waveform')
        if len(voltage) != len(current) or not voltage:
            raise ValueError('unpaired_waveform')
        pairs = list(zip(voltage, current))
        phases = len(voltage)
    else:
        pairs = [(voltage, current)]
        phases = 1
    out = []
    for v, i in pairs:
        if (not isinstance(v, (int, float)) or not isinstance(i, (int, float))
                or not math.isfinite(v) or not math.isfinite(i)):
            raise ValueError('invalid_sample')
        out.append((float(v), float(i)))
    return out, phases


def _validate_waveform(db, doc):
    """Classify a paired capture.  Any gap, phase loss, saturation or clock
    doubt invalidates the whole capture; it is never filled with zero power."""
    run = db.execute('SELECT * FROM pump_run WHERE run_id=?', (doc.get('run_id'),)).fetchone()
    if run is None:
        raise ValueError('unknown_run')
    meter = db.execute('SELECT * FROM pump_meter WHERE pump_id=?', (run['pump_id'],)).fetchone()
    if meter is None:
        raise ValueError('unknown_meter')
    if doc.get('pump_id') and doc['pump_id'] != run['pump_id']:
        raise ValueError('run_pump_mismatch')
    acquisition = db.execute('SELECT * FROM pump_acquisition WHERE acquisition_id=?',
                             (doc.get('acquisition_id'),)).fetchone()
    if acquisition is None:
        raise ValueError('clock_unverified')
    if doc.get('clock_id') and doc['clock_id'] != acquisition['clock_id']:
        raise ValueError('clock_conflict')
    if doc.get('phase_offset_deg') is not None and abs(
            doc['phase_offset_deg'] - acquisition['phase_offset_deg']) > 1e-9:
        raise ValueError('phase_offset_conflict')
    if acquisition['calibrated'] != 1:
        raise ValueError('clock_unverified')
    if not (acquisition['valid_from'] <= doc['start_at']
            and doc['end_at'] <= acquisition['valid_to']):
        raise ValueError('clock_unverified')
    # The capture must span the whole on-window.  A fragment cannot be
    # scaled up to the run and must not be filled with zeroes; it leaves the
    # run unmeasured instead.
    if not (doc['start_at'] <= run['start_at'] and run['end_at'] <= doc['end_at']):
        raise ValueError('incomplete_coverage')

    samples = doc.get('samples')
    if not isinstance(samples, list) or len(samples) < 8:
        raise EvidenceError('incomplete_coverage')
    if any(set(sample) - {'t', 'voltage', 'current'} for sample in samples):
        raise EvidenceError('per_sample_phase_not_allowed')

    dt_expected = 1.0 / acquisition['sample_rate_hz']
    times = []
    for sample in samples:
        if not isinstance(sample.get('t'), (int, float)):
            raise ValueError('clock_drift')
        times.append(float(sample['t']))
    if abs(times[0]) > 1e-9:
        raise ValueError('incomplete_coverage')
    deltas = [b - a for a, b in zip(times, times[1:])]
    if not deltas or any(abs(delta - dt_expected) > dt_expected * 0.01 for delta in deltas):
        raise ValueError('clock_drift')
    # The sampled window is a short, whole-cycle slice of the run; its duration
    # must be an integer number of grid cycles so the mean of v*i is the active
    # power.
    cycles = len(samples) * dt_expected * acquisition['frequency_hz']
    if abs(cycles - round(cycles)) > 1e-6 or round(cycles) < 1:
        raise ValueError('clock_drift')
    if abs(doc.get('clock_drift_s', 0)) > 1e-3:
        raise ValueError('clock_drift')

    pairs_by_sample = [_pair(sample) for sample in samples]
    phase_counts = {len(pairs) for pairs, _ in pairs_by_sample}
    if len(phase_counts) != 1:
        raise ValueError('unpaired_waveform')
    phases = phase_counts.pop()
    if doc.get('missing_phases') or phases not in (1, 3):
        raise ValueError('missing_phase')
    if doc.get('saturated') or any(abs(v) > 1000 or abs(i) > 1000
                                   for pairs, _ in pairs_by_sample for v, i in pairs):
        raise ValueError('saturated')

    duration = _hours(run['start_at'], run['end_at'])
    if duration <= 0:
        raise ValueError('invalid_run')
    # mean over whole synchronized cycles of the sum of per-phase v*i is the
    # active power.  The acquisition's shared phase offset is part of the
    # evidence and is used exactly as recorded; a different offset means a
    # different capture.
    power = sum(v * i for pairs, _ in pairs_by_sample for v, i in pairs) / len(samples)
    return run, meter, acquisition, power, duration, phases


def import_waveform(path, file):
    """Store a synchronized v/i capture and settle the run it belongs to.

    The capture must pair voltage and current on the same time base.  A
    missing phase, saturated channel, drifting clock or a gap invalidates the
    capture; the run then has no exact real-energy figure and is left for the
    station-level pool instead of being filled with zero power.
    """
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'pump_waveform')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not doc.get('waveform_id'):
            raise ValueError('invalid_waveform')
        if not doc.get('start_at') or not doc.get('end_at') or doc['start_at'] >= doc['end_at']:
            raise ValueError('invalid_waveform')
        if db.execute('SELECT 1 FROM pump_waveform WHERE waveform_id=?',
                      (doc['waveform_id'],)).fetchone():
            raise ValueError('waveform_conflict')
        run = db.execute('SELECT * FROM pump_run WHERE run_id=?', (doc.get('run_id'),)).fetchone()
        if run is None:
            raise ValueError('unknown_run')
        meter = db.execute('SELECT * FROM pump_meter WHERE pump_id=?', (run['pump_id'],)).fetchone()
        if meter is None:
            raise ValueError('unknown_meter')
        acquisition = db.execute('SELECT * FROM pump_acquisition WHERE acquisition_id=?',
                                 (doc.get('acquisition_id'),)).fetchone()
        try:
            _, _, acquisition, power, duration, _ = _validate_waveform(db, doc)
        except EvidenceError:
            raise
        except ValueError as exc:
            if str(exc) in REJECT:
                raise
            status, reason, real_kwh, covered_s = 'invalid', str(exc), None, 0.0
        else:
            status, reason = 'valid', ''
            real_kwh = round(power * duration / 1000, 9)
            covered_s = round(duration * 3600, 3)
            if db.execute("SELECT 1 FROM pump_waveform WHERE run_id=? AND status='valid'",
                          (run['run_id'],)).fetchone():
                raise ValueError('waveform_conflict_for_run')
        lower = upper = real_kwh
        if real_kwh is not None:
            # The exact waveform fixes the energy inside its covered window;
            # quantisation of the recorded samples leaves a tiny envelope.
            lower = real_kwh * (1 - 1e-6)
            upper = real_kwh * (1 + 1e-6)
        clock_id = doc.get('clock_id') or (acquisition['clock_id'] if acquisition else '')
        phase_offset = doc.get('phase_offset_deg')
        if phase_offset is None:
            phase_offset = acquisition['phase_offset_deg'] if acquisition else 0
        samples = doc.get('samples')
        sample_count = len(samples) if isinstance(samples, list) else 0
        missing = doc.get('missing_phases')
        missing_phases = ','.join(missing) if isinstance(missing, list) else str(missing or '')
        db.execute('INSERT INTO pump_waveform VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (doc['waveform_id'], run['run_id'], meter['id'], run['pump_id'],
                    doc.get('acquisition_id', ''), clock_id, phase_offset,
                    doc['start_at'], doc['end_at'], status, reason,
                    real_kwh, lower, upper, covered_s, sample_count,
                    missing_phases, doc['id']))
        from .real_energy import settle_run, resettle_station_windows
        settlement = settle_run(db, run['run_id'], doc['id'])
        resettle_station_windows(db, run['pump_id'], doc['start_at'],
                                 doc['end_at'], doc['id'])
        return {'waveform_id': doc['waveform_id'], 'status': status,
                'real_kwh': real_kwh, 'settlement': settlement}


def import_total_meter(path, file):
    """Register a utility-signed total meter and re-open station settlement.

    A station total can bound what the pumps together owe, but it cannot
    reveal how a phase-shifted pump split that energy between them.
    """
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'pump_total_meter')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if (not doc['total_meter_id'] or not doc['station_id']
                or doc['start_at'] >= doc['end_at'] or doc['kwh'] < 0
                or doc['rate_per_kwh'] < 0 or not doc['signer']
                or not doc['signature']):
            raise ValueError('invalid_total_meter')
        if doc['loss_kwh_lower'] < 0 or doc['loss_kwh_lower'] > doc['loss_kwh_upper']:
            raise ValueError('invalid_loss_bounds')
        if doc['signature'] != total_meter_signature(doc):
            raise ValueError('total_meter_signature_invalid')
        pumps = db.execute('SELECT 1 FROM pump_meter WHERE station_id=?',
                           (doc['station_id'],)).fetchone()
        if not pumps:
            raise ValueError('unknown_station')
        db.execute('INSERT INTO pump_total_meter VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (doc['id'], doc['total_meter_id'], doc['station_id'],
                    doc['start_at'], doc['end_at'], doc['kwh'],
                    doc['rate_per_kwh'], doc['signer'], doc['signature'],
                    doc['loss_kwh_lower'], doc['loss_kwh_upper']))
        from .real_energy import settle_station
        settlement = settle_station(db, doc['total_meter_id'], doc['id'])
        return {'total_meter_id': doc['total_meter_id'], 'settlement': settlement}
