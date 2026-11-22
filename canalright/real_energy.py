"""Real active energy from paired waveforms, and station-total settlement.

Two authorised evidence paths, two deliberately different deliveries:

* A synchronized voltage/current waveform gives ``v*i`` over the same time
  base.  Integrated over the run it is an exact per-pump kWh invoice.
* A utility-signed total meter gives a station total and a loss band.  It can
  bound the station bill and the feasible range for each pump, but it cannot
  recover a missing phase angle, so it never turns into an exact per-pump
  charge.

Versioned settlements keep the old apparent-power ``pump_bill`` as a
provisional entry while real charges, pending allocations and their evidence
are recorded separately.
"""
from .store import log

EPS = 1e-9


def _next_version(db, scope, pump_id, basis, basis_ref):
    latest = db.execute(
        'SELECT COALESCE(MAX(version),0) FROM real_settlement '
        'WHERE scope=? AND pump_id=? AND basis=? AND basis_ref=?',
        (scope, pump_id, basis, basis_ref)).fetchone()[0]
    return latest + 1


def _supersedes(db, scope, pump_id, basis, basis_ref):
    row = db.execute(
        'SELECT settlement_id FROM real_settlement WHERE scope=? AND pump_id=? '
        'AND basis=? AND basis_ref=? ORDER BY version DESC LIMIT 1',
        (scope, pump_id, basis, basis_ref)).fetchone()
    return row['settlement_id'] if row else None


def _record(db, scope, pump_id, meter_id, basis, basis_ref, status, kwh,
            lower, upper, rate, source_id, total_meter_id='', loss_lower=0.0,
            loss_upper=0.0, allocations=()):
    """Insert one versioned settlement.  Older versions are never overwritten."""
    existing = db.execute(
        'SELECT * FROM real_settlement WHERE scope=? AND pump_id=? AND basis=? '
        'AND basis_ref=? AND source_id=? AND total_meter_id=?',
        (scope, pump_id, basis, basis_ref, source_id, total_meter_id)).fetchone()
    if existing:
        return dict(existing)
    version = _next_version(db, scope, pump_id, basis, basis_ref)
    charge = None if kwh is None else kwh * rate
    lower_charge = None if lower is None else lower * rate
    upper_charge = None if upper is None else upper * rate
    cursor = db.execute(
        'INSERT INTO real_settlement(version,scope,pump_id,meter_id,basis,basis_ref,'
        'status,kwh,lower_kwh,upper_kwh,charge,lower_charge,upper_charge,'
        'rate_per_kwh,total_meter_id,loss_kwh_lower,loss_kwh_upper,source_id,'
        'supersedes_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (version, scope, pump_id, meter_id, basis, basis_ref, status, kwh,
         lower, upper, charge, lower_charge, upper_charge, rate,
         total_meter_id, loss_lower, loss_upper, source_id,
         _supersedes(db, scope, pump_id, basis, basis_ref)))
    settlement_id = cursor.lastrowid
    for allocation in allocations:
        db.execute(
            'INSERT INTO settlement_allocation(settlement_id,pump_id,status,'
            'exact_kwh,lower_kwh,upper_kwh,covered_start,covered_end) '
            'VALUES(?,?,?,?,?,?,?,?)',
            (settlement_id, allocation['pump_id'], allocation['status'],
             allocation.get('exact_kwh'), allocation['lower_kwh'],
             allocation['upper_kwh'], allocation.get('covered_start', ''),
             allocation.get('covered_end', '')))
    log(db, 'settlement', f'{scope}:{pump_id or basis}', status, basis, source_id)
    return dict(db.execute('SELECT * FROM real_settlement WHERE settlement_id=?',
                           (settlement_id,)).fetchone())


def settle_run(db, run_id, evidence_id):
    """Settle one pump run from its capture.

    Only a valid paired waveform becomes an exact per-pump charge.  An
    unusable capture (missing phase, saturation, clock drift, gaps) leaves the
    run unmeasured: no zero-energy fill-in and no per-pump invoice.
    """
    run = db.execute('SELECT * FROM pump_run WHERE run_id=?', (run_id,)).fetchone()
    if run is None:
        raise ValueError('unknown_run')
    meter = db.execute('SELECT * FROM pump_meter WHERE pump_id=?',
                       (run['pump_id'],)).fetchone()
    waveform = db.execute(
        "SELECT * FROM pump_waveform WHERE run_id=? AND status='valid' "
        'ORDER BY rowid DESC LIMIT 1', (run_id,)).fetchone()
    if waveform is None:
        invalid = db.execute(
            "SELECT * FROM pump_waveform WHERE run_id=? AND status='invalid' "
            'ORDER BY rowid DESC LIMIT 1', (run_id,)).fetchone()
        if invalid is not None:
            tracked = db.execute(
                "SELECT 1 FROM real_settlement WHERE scope='pump' AND pump_id=? "
                "AND basis='waveform' AND basis_ref=? AND status='unmeasured'",
                (run['pump_id'], run_id)).fetchone()
            if not tracked:
                _record(db, 'pump', run['pump_id'], meter['id'], 'waveform',
                        run_id, 'unmeasured', None, None, None,
                        meter['rate_per_kwh'], evidence_id)
        return {'run_id': run_id, 'status': 'unmeasured',
                'reason': invalid['reason'] if invalid else 'no_capture',
                'waveform_id': invalid['waveform_id'] if invalid else None}
    kwh = waveform['real_kwh']
    settlement = _record(
        db, 'pump', run['pump_id'], meter['id'], 'waveform', run_id,
        'exact', kwh, kwh - EPS, kwh + EPS, meter['rate_per_kwh'], evidence_id)
    return {'run_id': run_id, 'status': 'exact', 'waveform_id': waveform['waveform_id'],
            'kwh': kwh, 'settlement': settlement}


def _run_bounds(db, pump_id, start_at, end_at):
    """Feasible real energy of one pump inside a station window.

    Returns ``(lower, upper, exact)``.  ``exact`` is True only when every
    registered on-window of the pump is fully inside the station window and
    every such on-window has a valid paired capture.  A run with no usable
    capture leaves ``upper`` None: an absent or broken record is not a
    measured zero.
    """
    runs = db.execute(
        'SELECT * FROM pump_run WHERE pump_id=? AND start_at<? AND end_at>? '
        'ORDER BY start_at, run_id', (pump_id, end_at, start_at)).fetchall()
    if not runs:
        # No on-window evidence.  That is not proof the pump drew nothing, so
        # the pump's range stays open rather than being zero-filled.
        return 0.0, None, False

    lower = 0.0
    upper = 0.0
    exact = True
    for run in runs:
        waveform = db.execute(
            "SELECT * FROM pump_waveform WHERE run_id=? AND status='valid' "
            "ORDER BY rowid DESC LIMIT 1", (run['run_id'],)).fetchone()
        if waveform is None:
            # An unmeasured or invalidated run contributes no known number.
            exact = False
            upper = None
            continue
        if run['start_at'] >= start_at and run['end_at'] <= end_at:
            lower += waveform['real_kwh']
            if upper is not None:
                upper += waveform['real_kwh']
        else:
            # Part of this run falls outside the total window.  Its full
            # energy is a safe upper bound; the lower bound remains open.
            exact = False
            if upper is not None:
                upper += waveform['real_kwh']
    return lower, upper, exact


def settle_station(db, total_meter_id, evidence_id):
    """Settle a station from a signed total meter plus whatever per-pump data
    exists.  Each pump gets a feasible interval; the residual is a pool with
    its own status, never an arbitrary assignment to one pump.
    """
    total = db.execute('SELECT * FROM pump_total_meter WHERE total_meter_id=? '
                       'ORDER BY rowid DESC LIMIT 1',
                       (total_meter_id,)).fetchone()
    if total is None:
        raise ValueError('unknown_total_meter')
    start_at, end_at = total['start_at'], total['end_at']
    rate = total['rate_per_kwh']
    pumps = [row['pump_id'] for row in db.execute(
        'SELECT DISTINCT pump_id FROM pump_meter WHERE station_id=? ORDER BY pump_id',
        (total['station_id'],))]
    if not pumps:
        raise ValueError('unknown_station')

    local = {}
    for pump_id in pumps:
        lower, upper, exact = _run_bounds(db, pump_id, start_at, end_at)
        local[pump_id] = {'lower': lower, 'upper': upper, 'open': upper is None,
                          'status': 'exact' if exact else 'range'}

    # A total meter covers the whole station.  With loss in
    # [loss_lower, loss_upper], the pumps together must lie in
    # [kwh - loss_upper, kwh - loss_lower].  No pump may be billed outside
    # that joint constraint, and no residual is dumped on the last pump.
    station_lower = max(0.0, total['kwh'] - total['loss_kwh_upper'])
    station_upper = total['kwh'] - total['loss_kwh_lower']
    exact_range = [p for p in pumps if local[p]['status'] == 'exact']
    exact_sum = sum(local[p]['lower'] for p in exact_range)
    infeasible = False
    for pump_id in pumps:
        if local[pump_id]['status'] == 'exact':
            continue
        closed_others = [local[q] for q in pumps if q != pump_id and not local[q]['open']]
        open_others = any(local[q]['open'] for q in pumps
                          if q != pump_id and local[q]['status'] != 'exact')
        other_lo = sum(item['lower'] for item in closed_others)
        # An open sibling could itself absorb the whole remainder, so it adds
        # no upper bound to this pump's feasible range.
        other_hi = None if open_others else sum(item['upper'] for item in closed_others)
        lower = local[pump_id]['lower']
        if other_hi is not None:
            lower = max(lower, station_lower - other_hi, 0.0)
        if local[pump_id]['open']:
            upper = max(0.0, station_upper - other_lo)
        else:
            upper = min(local[pump_id]['upper'], station_upper - other_lo)
        if lower > upper + 1e-6:
            infeasible = True
        local[pump_id]['lower'] = lower
        local[pump_id]['upper'] = max(lower, upper)
        local[pump_id]['open'] = False

    measured_lower = sum(local[p]['lower'] for p in pumps)
    measured_upper = sum(local[p]['upper'] for p in pumps)
    within_joint = (measured_lower <= station_upper + 1e-6
                    and measured_upper >= station_lower - 1e-6)
    pool_lower = max(0.0, station_lower - exact_sum)
    pool_upper = max(0.0, station_upper - exact_sum)

    if infeasible or not within_joint:
        status = 'loss_out_of_bounds'
        pool_status = 'loss_out_of_bounds'
    elif exact_range and len(exact_range) == len(pumps):
        status = 'exact'
        pool_status = 'loss'
    else:
        status = 'bounded'
        pool_status = 'pending_allocation'

    allocations = [{
        'pump_id': pump_id,
        'status': local[pump_id]['status'],
        'exact_kwh': (local[pump_id]['lower']
                      if local[pump_id]['status'] == 'exact' else None),
        'lower_kwh': local[pump_id]['lower'],
        'upper_kwh': local[pump_id]['upper'],
        'covered_start': start_at, 'covered_end': end_at,
    } for pump_id in pumps]

    settlement = _record(
        db, 'station', '', total['station_id'], 'total_meter', total_meter_id,
        status, total['kwh'], station_lower, station_upper, rate, evidence_id,
        total_meter_id=total_meter_id, loss_lower=total['loss_kwh_lower'],
        loss_upper=total['loss_kwh_upper'], allocations=allocations)
    pool = _record(
        db, 'station', '', total['station_id'], 'pool', total_meter_id,
        pool_status, None, pool_lower, pool_upper, rate, evidence_id,
        total_meter_id=total_meter_id, loss_lower=total['loss_kwh_lower'],
        loss_upper=total['loss_kwh_upper'])
    return {'total_meter_id': total_meter_id, 'status': status,
            'station_kwh': total['kwh'], 'station_lower_kwh': station_lower,
            'station_upper_kwh': station_upper, 'exact_kwh': exact_sum,
            'pool_lower_kwh': pool_lower, 'pool_upper_kwh': pool_upper,
            'pool_status': pool_status,
            'allocations': {p: {'status': local[p]['status'],
                                'exact_kwh': (local[p]['lower']
                                              if local[p]['status'] == 'exact'
                                              else None),
                                'lower_kwh': local[p]['lower'],
                                'upper_kwh': local[p]['upper']} for p in pumps},
            'settlement_id': settlement['settlement_id'],
            'pool_settlement_id': pool['settlement_id']}


def resettle_station_windows(db, pump_id, start_at, end_at, evidence_id):
    """Re-run every station total touched by new per-pump evidence.

    Each pass records a fresh settlement version; earlier versions and the
    evidence that produced them remain readable.
    """
    station = db.execute('SELECT station_id FROM pump_meter WHERE pump_id=?',
                         (pump_id,)).fetchone()
    if station is None or not station['station_id']:
        return []
    totals = db.execute(
        'SELECT DISTINCT total_meter_id FROM pump_total_meter WHERE station_id=? '
        'AND start_at<? AND end_at>? ORDER BY total_meter_id',
        (station['station_id'], end_at, start_at)).fetchall()
    outcomes = []
    for row in totals:
        outcomes.append(settle_station(db, row['total_meter_id'], evidence_id))
    return outcomes
