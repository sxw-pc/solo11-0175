import json
import math
from pathlib import Path

import pytest

from canalright.audit import audit
from canalright.power import (import_acquisition, import_meter, import_run,
                              import_sample, import_total_meter,
                              import_waveform, total_meter_signature)
from canalright.store import report

HOUR = ('2026-11-22T08:00:00Z', '2026-11-22T09:00:00Z')
FREQ = 60
SAMPLES = 60  # 10 samples/cycle over 6 whole cycles
DT = 1.0 / (FREQ * 10)
RMS_V, RMS_I = 230 * math.sqrt(2), 10 * math.sqrt(2)


def write(tmp_path, name, doc):
    path = tmp_path / f'{name}.json'
    path.write_text(json.dumps(doc))
    return path


def phase_waveform(phase_deg=-60, count=SAMPLES):
    samples = []
    omega = 2 * math.pi * FREQ
    for n in range(count):
        t = n * DT
        samples.append({
            't': t,
            'voltage': RMS_V * math.sin(omega * t),
            'current': RMS_I * math.sin(omega * t + math.radians(phase_deg)),
        })
    return samples


def meter(tmp_path, db, pump='P1', meter_id=None, station='ST1', rate=1):
    meter_id = meter_id or f'M-{pump}'
    import_meter(db, write(tmp_path, f'meter-{pump}', {
        'kind': 'pump_meter', 'id': f'meter-evidence-{pump}',
        'meter_id': meter_id, 'pump_id': pump, 'rate_per_kwh': rate,
        'station_id': station}))
    return meter_id


def acquisition(tmp_path, db, phase=0, acquisition_id='A1', clock='clk-1'):
    import_acquisition(db, write(tmp_path, f'acq-{acquisition_id}', {
        'kind': 'pump_acquisition', 'id': f'acq-evidence-{acquisition_id}',
        'acquisition_id': acquisition_id, 'clock_id': clock,
        'frequency_hz': FREQ, 'sample_rate_hz': FREQ * 10,
        'phase_offset_deg': phase,
        'calibrated': True, 'technician': 'lab', 'witness': 'auditor',
        'valid_from': '2026-11-01T00:00:00Z', 'valid_to': '2026-12-01T00:00:00Z'}))


def run(tmp_path, db, run_id='P1-run', pump='P1', window=HOUR):
    import_run(db, write(tmp_path, run_id, {
        'kind': 'pump_run', 'id': f'run-evidence-{run_id}', 'run_id': run_id,
        'pump_id': pump, 'start_at': window[0], 'end_at': window[1]}))
    return run_id


def waveform_doc(waveform_id, run_id, acquisition_id, samples, **changes):
    doc = {'kind': 'pump_waveform', 'id': f'wave-evidence-{waveform_id}',
           'waveform_id': waveform_id, 'run_id': run_id,
           'acquisition_id': acquisition_id, 'start_at': HOUR[0],
           'end_at': HOUR[1], 'samples': samples}
    doc.update(changes)
    return doc


def signed_total(tmp_path, **changes):
    doc = {'kind': 'pump_total_meter', 'id': 'total-evidence-1',
           'total_meter_id': 'TM-1', 'station_id': 'ST1',
           'start_at': HOUR[0], 'end_at': HOUR[1], 'kwh': 3.5,
           'rate_per_kwh': 1, 'signer': 'utility', 'loss_kwh_lower': 0,
           'loss_kwh_upper': 0}
    doc.update(changes)
    doc['signature'] = total_meter_signature(doc)
    return write(tmp_path, 'total', doc)


def settle_one(tmp_path, db, phase_deg):
    meter(tmp_path, db)
    acquisition(tmp_path, db, phase=phase_deg)
    run(tmp_path, db)
    return import_waveform(db, write(tmp_path, f'wave-{phase_deg}',
                                     waveform_doc('W1', 'P1-run', 'A1',
                                                  phase_waveform(phase_deg))))


# --- P1: the provisional page is still reproducible and still provisional ---

def test_rms_page_still_reproduces_the_2_3_provisional_bill(tmp_path):
    db = tmp_path / 'canal.sqlite'
    import_meter(db, write(tmp_path, 'meter', {
        'kind': 'pump_meter', 'id': 'M1', 'meter_id': 'M1', 'pump_id': 'P1',
        'rate_per_kwh': 1}))
    result = import_sample(db, write(tmp_path, 'sample', {
        'kind': 'pump_sample', 'id': 'S1', 'meter_id': 'M1',
        'start_at': HOUR[0], 'end_at': HOUR[1],
        'voltage_rms': 230, 'current_rms': 10}))
    assert (result['estimated_kwh'], result['estimated_charge']) == (2.3, 2.3)
    bills = report(db)['pump_bills']
    assert [(b['estimated_kwh'], b['estimated_charge']) for b in bills] == [(2.3, 2.3)]


def test_identical_rms_pages_hide_2_3_versus_1_15_real_energy(tmp_path):
    """Process A (in phase) and B (60 deg lag) share every RMS page, run
    window and provisional bill; only paired waveforms separate them."""
    in_phase = settle_one(tmp_path, tmp_path / 'a.sqlite', 0)
    lagging = settle_one(tmp_path, tmp_path / 'b.sqlite', -60)

    assert in_phase['status'] == 'valid'
    assert lagging['status'] == 'valid'
    assert in_phase['real_kwh'] == pytest.approx(2.3, abs=1e-6)
    assert lagging['real_kwh'] == pytest.approx(1.15, abs=1e-6)

    # Both databases carry the same apparent-power page and provisional bill;
    # the real settlement differs only because of the paired waveform.
    for label, path in (('a', tmp_path / 'a.sqlite'), ('b', tmp_path / 'b.sqlite')):
        import_sample(path, write(tmp_path, f'sample-{label}', {
            'kind': 'pump_sample', 'id': f'S-{label}', 'meter_id': 'M-P1',
            'start_at': HOUR[0], 'end_at': HOUR[1],
            'voltage_rms': 230, 'current_rms': 10}))
        sample = report(path)['pump_samples'][0]
        assert (sample['voltage_rms'], sample['current_rms']) == (230, 10)
        assert report(path)['pump_bills'][0]['estimated_charge'] == 2.3
        assert report(path)['pump_bills'][0]['estimated_kwh'] == 2.3


def test_apparent_power_billing_would_overcharge_the_lagging_process(tmp_path):
    """The old RMS-product rule bills 2.3 for both processes, overcharging
    the lagging one by 1.15 yuan; a blanket power factor of 0.5 undercharges
    the in-phase one by 1.15 yuan."""
    in_phase = settle_one(tmp_path, tmp_path / 'a.sqlite', 0)
    lagging = settle_one(tmp_path, tmp_path / 'b.sqlite', -60)
    provisional = 230 * 10 / 1000
    assert provisional - lagging['real_kwh'] == pytest.approx(1.15)
    assert in_phase['real_kwh'] - provisional * 0.5 == pytest.approx(1.15)
    # The true power factor is not recoverable from one RMS page:
    assert in_phase['real_kwh'] != lagging['real_kwh']


# --- shared phase offset, missing phases, pairing, clock drift -------------

def test_shared_phase_offset_cannot_be_retuned_per_waveform(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db)
    acquisition(tmp_path, db, phase=0)
    run(tmp_path, db)
    lagging = waveform_doc('W1', 'P1-run', 'A1', phase_waveform(-60))
    with pytest.raises(ValueError, match='phase_offset_conflict'):
        import_waveform(db, write(tmp_path, 'claim', dict(
            lagging, phase_offset_deg=-60)))
    assert report(db)['pump_waveforms'] == []


def test_one_capture_rejected_does_not_block_sibling_captures(tmp_path):
    """A bad sibling capture is evidence about that capture, not a reason to
    refuse an otherwise sound one."""
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db)
    acquisition(tmp_path, db)
    run(tmp_path, db)
    import_acquisition(db, write(tmp_path, 'acq-2', {
        'kind': 'pump_acquisition', 'id': 'acq-evidence-A2',
        'acquisition_id': 'A2', 'clock_id': 'clk-2', 'frequency_hz': FREQ,
        'sample_rate_hz': FREQ * 10, 'phase_offset_deg': 0,
        'calibrated': False}))
    bad = import_waveform(db, write(tmp_path, 'bad', waveform_doc(
        'W0', 'P1-run', 'A2', phase_waveform(0))))
    good = import_waveform(db, write(tmp_path, 'good', waveform_doc(
        'W1', 'P1-run', 'A1', phase_waveform(-60))))
    assert (bad['status'], bad['real_kwh']) == ('invalid', None)
    assert good['status'] == 'valid'
    assert good['real_kwh'] == pytest.approx(1.15, abs=1e-6)
    # Letting an invalid capture be replaced by a sound one updates the
    # settlement instead of leaving the run frozen as unmeasured.
    settlements = [row for row in report(db)['real_settlements']
                   if row['scope'] == 'pump']
    assert settlements[-1]['status'] == 'exact'


def test_per_sample_phase_cannot_be_smuggled_in(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db)
    acquisition(tmp_path, db, phase=0)
    run(tmp_path, db)
    samples = phase_waveform(-60)
    samples[3]['phase_deg'] = -60
    with pytest.raises(ValueError):
        import_waveform(db, write(tmp_path, 'smuggle',
                                  waveform_doc('W1', 'P1-run', 'A1', samples)))
    assert report(db)['pump_waveforms'] == []


def test_unpaired_or_unshared_samples_do_not_create_false_low_energy(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db)
    acquisition(tmp_path, db)
    run(tmp_path, db)
    # Shift the current by half a sample: multiplying the two pages
    # element-by-element yields a spurious near-zero energy.
    voltage = phase_waveform(0)
    current = phase_waveform(0)
    shifted = [{'t': sample['t'], 'voltage': sample['voltage']}
               for sample in voltage]
    for offset, sample in zip(shifted, current):
        offset['current'] = sample['current']
    shifted = shifted[1:]
    result = import_waveform(db, write(
        tmp_path, 'unpaired',
        waveform_doc('W1', 'P1-run', 'A1', shifted)))
    assert result['status'] == 'invalid'
    assert result['real_kwh'] is None
    assert report(db)['pump_waveforms'][0]['status'] == 'invalid'
    assert report(db)['real_settlements'][0]['status'] == 'unmeasured'


def test_missing_phase_capture_settles_nothing(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db)
    acquisition(tmp_path, db)
    run(tmp_path, db)
    result = import_waveform(db, write(tmp_path, 'missing', waveform_doc(
        'W1', 'P1-run', 'A1', phase_waveform(0), missing_phases=['C'])))
    assert result['status'] == 'invalid'
    assert result['real_kwh'] is None
    rows = report(db)['pump_waveforms']
    assert (rows[0]['status'], rows[0]['real_kwh']) == ('invalid', None)
    allocations = report(db)['settlement_allocations']
    assert all(a['exact_kwh'] is None for a in allocations)


def test_three_phase_capture_integrates_all_phases(tmp_path):
    """Each phase pair is integrated on its own and summed; three identical
    lagging phases therefore carry three times one phase's real energy."""
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db)
    acquisition(tmp_path, db)
    run(tmp_path, db)
    samples = []
    for sample in phase_waveform(-60):
        v = sample['voltage']
        i = sample['current']
        samples.append({'t': sample['t'],
                        'voltage': [v, v, v],
                        'current': [i, i, i]})
    result = import_waveform(db, write(tmp_path, 'three-phase', waveform_doc(
        'W1', 'P1-run', 'A1', samples)))
    assert result['status'] == 'valid'
    assert result['real_kwh'] == pytest.approx(3 * 1.15, abs=1e-6)


def test_saturated_capture_and_clock_drift_are_not_zero_filled(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db)
    acquisition(tmp_path, db)
    run(tmp_path, db)
    saturated = import_waveform(db, write(tmp_path, 'sat', waveform_doc(
        'W1', 'P1-run', 'A1', phase_waveform(0), saturated=True)))
    assert (saturated['status'], saturated['real_kwh']) == ('invalid', None)

    samples = phase_waveform(0)
    samples[5]['t'] += 0.001  # one sample arrives off the common clock
    drifted = import_waveform(db, write(tmp_path, 'drift', waveform_doc(
        'W2', 'P1-run', 'A1', samples)))
    assert (drifted['status'], drifted['real_kwh']) == ('invalid', None)


def test_uncalibrated_or_out_of_window_clock_is_rejected(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db)
    run(tmp_path, db)
    import_acquisition(db, write(tmp_path, 'uncal', {
        'kind': 'pump_acquisition', 'id': 'acq-evidence-uncal',
        'acquisition_id': 'A1', 'clock_id': 'clk-1', 'frequency_hz': FREQ,
        'sample_rate_hz': FREQ * 10, 'phase_offset_deg': 0,
        'calibrated': False}))
    result = import_waveform(db, write(tmp_path, 'w', waveform_doc(
        'W1', 'P1-run', 'A1', phase_waveform(0))))
    assert (result['status'], result['real_kwh']) == ('invalid', None)


# --- signed total meter -----------------------------------------------------

def test_signed_total_without_phase_cannot_name_a_per_pump_debt(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db, pump='P1')
    meter(tmp_path, db, pump='P2')
    import_sample(db, write(tmp_path, 'p1-sample', {
        'kind': 'pump_sample', 'id': 'S-P1', 'meter_id': 'M-P1',
        'start_at': HOUR[0], 'end_at': HOUR[1],
        'voltage_rms': 230, 'current_rms': 10}))
    import_sample(db, write(tmp_path, 'p2-sample', {
        'kind': 'pump_sample', 'id': 'S-P2', 'meter_id': 'M-P2',
        'start_at': HOUR[0], 'end_at': HOUR[1],
        'voltage_rms': 230, 'current_rms': 5.217391304347826}))
    result = import_total_meter(db, signed_total(tmp_path))

    assert result['settlement']['status'] == 'bounded'
    assert result['settlement']['station_kwh'] == 3.5
    assert result['settlement']['pool_status'] == 'pending_allocation'
    assert result['settlement']['pool_lower_kwh'] == 3.5
    assert result['settlement']['pool_upper_kwh'] == 3.5
    allocations = {a['pump_id']: a
                   for a in report(db)['settlement_allocations']}
    assert set(allocations) == {'P1', 'P2'}
    # Neither pump is handed an exact number; both carry a range.
    assert all(a['status'] == 'range' for a in allocations.values())
    assert all(a['exact_kwh'] is None for a in allocations.values())
    # The RMS product 230x10 is never promoted into a phase-derived split.
    assert allocations['P1']['lower_kwh'] == 0
    assert allocations['P1']['upper_kwh'] == pytest.approx(3.5)


def test_signed_total_with_pair_P1_closes_against_P2_and_loss(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db, pump='P1')
    meter(tmp_path, db, pump='P2')
    acquisition(tmp_path, db)
    run(tmp_path, db, run_id='P1-run', pump='P1')
    settlement = import_waveform(db, write(tmp_path, 'p1-wave', waveform_doc(
        'W1', 'P1-run', 'A1', phase_waveform(-60))))
    assert settlement['real_kwh'] == pytest.approx(1.15, abs=1e-6)

    total = import_total_meter(db, signed_total(
        tmp_path, kwh=3.5, loss_kwh_lower=0.1, loss_kwh_upper=0.35))
    result = total['settlement']
    assert result['status'] == 'bounded'
    assert result['pool_status'] == 'pending_allocation'
    # P1 has an exact invoice; P2 only has a feasible range; station closes.
    assert result['allocations']['P1']['status'] == 'exact'
    assert result['allocations']['P1']['exact_kwh'] == pytest.approx(1.15)
    assert result['allocations']['P2']['status'] == 'range'
    assert result['allocations']['P2']['lower_kwh'] == pytest.approx(2.0)
    assert result['allocations']['P2']['upper_kwh'] == pytest.approx(2.25)
    assert result['station_lower_kwh'] == pytest.approx(3.15)
    assert result['station_upper_kwh'] == pytest.approx(3.4)
    # The pool is exactly the part not backed by a paired waveform.
    assert result['pool_lower_kwh'] == pytest.approx(2.0)
    assert result['pool_upper_kwh'] == pytest.approx(2.25)
    assert result['pool_status'] == 'pending_allocation'


def test_total_meter_rejects_bad_signature(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db, pump='P1')
    doc = json.loads(signed_total(tmp_path, kwh=3.5).read_text())
    doc['kwh'] = 1.0
    with pytest.raises(ValueError, match='total_meter_signature_invalid'):
        import_total_meter(db, write(tmp_path, 'tampered', doc))


def test_inconsistent_estimates_never_hide_behind_the_station_total(tmp_path):
    """When a blanket 0.5 power factor makes both pumps 'exact' at half of
    their apparent power, the sum falls below the signed total.  That gap is
    surfaced as a loss/pool discrepancy, not silently dropped."""
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db, pump='P1')
    meter(tmp_path, db, pump='P2')
    for pump in ('P1', 'P2'):
        import_sample(db, write(tmp_path, f'{pump}-sample', {
            'kind': 'pump_sample', 'id': f'S-{pump}', 'meter_id': f'M-{pump}',
            'start_at': HOUR[0], 'end_at': HOUR[1],
            'voltage_rms': 230, 'current_rms': 10}))
    result = import_total_meter(db, signed_total(
        tmp_path, kwh=3.5, loss_kwh_lower=0, loss_kwh_upper=0.1))['settlement']
    assert result['allocations']['P1']['status'] == 'range'
    assert result['status'] in ('bounded', 'loss_out_of_bounds')


# --- station overlap, independent loss and incomplete evidence -------------

def test_partial_run_inside_total_window_is_a_range_not_last_pump_residual(tmp_path):
    """P1's capture starts before the total window.  That part of the run
    cannot be split by subtraction, so P1 gets an open lower bound rather
    than being credited (or charged) by difference."""
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db, pump='P1')
    meter(tmp_path, db, pump='P2')
    acquisition(tmp_path, db)
    run(tmp_path, db, run_id='P1-run', pump='P1',
        window=('2026-11-22T07:30:00Z', '2026-11-22T08:30:00Z'))
    import_waveform(db, write(tmp_path, 'p1-wave', waveform_doc(
        'W1', 'P1-run', 'A1', phase_waveform(-60),
        start_at='2026-11-22T07:30:00Z', end_at='2026-11-22T08:30:00Z')))
    result = import_total_meter(db, signed_total(
        tmp_path, kwh=3.5))['settlement']
    assert result['status'] == 'bounded'
    assert result['allocations']['P1']['status'] == 'range'
    assert result['allocations']['P1']['lower_kwh'] == 0
    assert result['allocations']['P1']['upper_kwh'] == pytest.approx(1.15)
    assert result['allocations']['P2']['status'] == 'range'
    # Nothing is silently dumped on P2; the closed part is just the pool.
    assert result['pool_status'] == 'pending_allocation'


def test_run_without_any_capture_leaves_the_pump_unmeasured(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db, pump='P1')
    meter(tmp_path, db, pump='P2')
    run(tmp_path, db, run_id='P1-run', pump='P1')
    result = import_total_meter(db, signed_total(tmp_path))['settlement']
    assert result['allocations']['P1']['status'] == 'range'
    assert result['allocations']['P1']['lower_kwh'] == 0
    # P1 has a registered on-window and no usable energy: the run is not a
    # measured zero, so the pool does not shrink.
    assert result['pool_lower_kwh'] == pytest.approx(3.5)


def test_capture_fragment_cannot_be_scaled_to_the_run(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db)
    acquisition(tmp_path, db)
    run(tmp_path, db)
    doc = waveform_doc('W1', 'P1-run', 'A1', phase_waveform(0))
    doc['start_at'] = '2026-11-22T08:10:00Z'
    doc['end_at'] = '2026-11-22T08:20:00Z'
    result = import_waveform(db, write(tmp_path, 'fragment', doc))
    assert (result['status'], result['real_kwh']) == ('invalid', None)
    assert report(db)['pump_waveforms'][0]['reason'] == 'incomplete_coverage'


def test_independent_loss_bounds_station_and_pool(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db, pump='P1')
    meter(tmp_path, db, pump='P2')
    acquisition(tmp_path, db)
    run(tmp_path, db, run_id='P1-run', pump='P1')
    import_waveform(db, write(tmp_path, 'p1-wave', waveform_doc(
        'W1', 'P1-run', 'A1', phase_waveform(-60))))
    result = import_total_meter(db, signed_total(
        tmp_path, kwh=3.5, loss_kwh_lower=0.05,
        loss_kwh_upper=0.25))['settlement']
    assert result['station_lower_kwh'] == pytest.approx(3.25)
    assert result['station_upper_kwh'] == pytest.approx(3.45)
    assert result['exact_kwh'] == pytest.approx(1.15)
    assert result['pool_lower_kwh'] == pytest.approx(2.10)
    assert result['pool_upper_kwh'] == pytest.approx(2.30)
    assert result['allocations']['P2']['lower_kwh'] == pytest.approx(2.10)
    assert result['allocations']['P2']['upper_kwh'] == pytest.approx(2.30)
    assert result['status'] == 'bounded'


def test_readings_that_cannot_close_the_total_are_flagged_not_reassigned(tmp_path):
    """Two paired captures whose sum exceeds the signed station total cannot
    both be true.  The mismatch is surfaced; neither pump is retroactively
    reduced to make the total work."""
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db, pump='P1')
    meter(tmp_path, db, pump='P2')
    acquisition(tmp_path, db)
    for pump in ('P1', 'P2'):
        run(tmp_path, db, run_id=f'{pump}-run', pump=pump)
        import_waveform(db, write(tmp_path, f'{pump}-wave', waveform_doc(
            f'W-{pump}', f'{pump}-run', 'A1', phase_waveform(0))))
    result = import_total_meter(db, signed_total(
        tmp_path, kwh=3.5, loss_kwh_lower=0, loss_kwh_upper=0))['settlement']
    assert result['status'] == 'loss_out_of_bounds'
    assert result['pool_status'] == 'loss_out_of_bounds'
    # Exact per-pump invoices were still delivered; the station total is the
    # part that failed to reconcile.
    assert result['allocations']['P1']['exact_kwh'] == pytest.approx(2.3)
    assert result['allocations']['P2']['exact_kwh'] == pytest.approx(2.3)
    assert result['exact_kwh'] == pytest.approx(4.6)


def test_total_meter_covers_station_pumps_only(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db, pump='P1', station='ST1')
    meter(tmp_path, db, pump='P2', station='ST1')
    meter(tmp_path, db, pump='P9', station='ST2')
    run(tmp_path, db, run_id='P9-run', pump='P9')
    result = import_total_meter(db, signed_total(tmp_path))['settlement']
    assert set(result['allocations']) == {'P1', 'P2'}


def test_retry_with_sound_evidence_supersedes_the_unmeasured_version(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db)
    acquisition(tmp_path, db)
    run(tmp_path, db)
    first = import_waveform(db, write(tmp_path, 'partial', waveform_doc(
        'W0', 'P1-run', 'A1', phase_waveform(0), saturated=True)))
    assert first['settlement']['status'] == 'unmeasured'
    second = import_waveform(db, write(tmp_path, 'sound', waveform_doc(
        'W1', 'P1-run', 'A1', phase_waveform(-60))))
    assert second['settlement']['status'] == 'exact'
    settlements = [row for row in report(db)['real_settlements']
                   if row['scope'] == 'pump' and row['basis_ref'] == 'P1-run']
    assert [row['status'] for row in settlements] == ['unmeasured', 'exact']
    assert settlements[1]['version'] == 2
    assert settlements[1]['supersedes_id'] == settlements[0]['settlement_id']


# --- versioning -------------------------------------------------------------

def test_settlements_keep_versions_and_original_evidence(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db, pump='P1')
    meter(tmp_path, db, pump='P2')
    acquisition(tmp_path, db)
    run(tmp_path, db, run_id='P1-run', pump='P1')
    import_sample(db, write(tmp_path, 'p1-sample', {
        'kind': 'pump_sample', 'id': 'S-P1', 'meter_id': 'M-P1',
        'start_at': HOUR[0], 'end_at': HOUR[1],
        'voltage_rms': 230, 'current_rms': 10}))
    import_waveform(db, write(tmp_path, 'p1-wave', waveform_doc(
        'W1', 'P1-run', 'A1', phase_waveform(-60))))
    import_total_meter(db, signed_total(tmp_path))
    import_total_meter(db, signed_total(
        tmp_path, id='total-evidence-2', kwh=3.4, signer='utility'))

    state = report(db)
    # The original RMS page and its 2.3 yuan provisional entry are untouched
    # by the real settlement sitting beside them.
    assert [(s['voltage_rms'], s['current_rms'])
            for s in state['pump_samples']] == [(230, 10)]
    assert [(b['estimated_kwh'], b['estimated_charge'])
            for b in state['pump_bills']] == [(2.3, 2.3)]
    # Both signed totals are retained; the second creates a new version.
    totals = {row['total_meter_id']: row for row in state['pump_total_meters']}
    assert totals['TM-1']['kwh'] == 3.4
    versions = [row for row in state['real_settlements']
                if row['basis'] == 'total_meter']
    assert len(versions) == 2
    assert sorted(row['version'] for row in versions) == [1, 2]
    assert {row['source_id'] for row in versions} == {
        'total-evidence-1', 'total-evidence-2'}
    pump_versions = [row for row in state['real_settlements']
                     if row['scope'] == 'pump']
    assert pump_versions and pump_versions[-1]['status'] == 'exact'
    assert pump_versions[-1]['kwh'] == pytest.approx(1.15)
    # Every layer is replayable from its own evidence bytes.
    assert audit(db)['status'] == 'matched'


# --- fixture and replay -----------------------------------------------------

def test_pump_phase_fixture_matches_the_recorded_numbers():
    fixture = json.loads((Path(__file__).parent.parent / 'fixtures' /
                          'pump-phase.json').read_text())
    window = fixture['window']
    process = fixture['processes']['B']
    assert window['sample_count'] == window['cycles'] * window['samples_per_cycle']
    assert fixture['processes']['A']['real_kwh'] == pytest.approx(
        window['voltage_rms'] * window['current_rms'] / 1000)
    assert process['real_kwh'] == pytest.approx(
        window['voltage_rms'] * window['current_rms'] / 1000
        * math.cos(math.radians(process['phase_deg'])))
    # A total of 3.5 is what A plus B plus a small independent loss would show;
    # it is exactly why the total cannot name each pump's share on its own.
    assert fixture['station_total']['kwh'] == pytest.approx(
        fixture['processes']['A']['real_kwh'] + process['real_kwh'] + 0.05)
    assert fixture['station_total']['signature'] == total_meter_signature(
        fixture['station_total'])


def test_real_pump_evidence_replays_through_audit(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db, pump='P1')
    meter(tmp_path, db, pump='P2')
    acquisition(tmp_path, db)
    run(tmp_path, db, run_id='P1-run', pump='P1')
    import_sample(db, write(tmp_path, 'p1-sample', {
        'kind': 'pump_sample', 'id': 'S-P1', 'meter_id': 'M-P1',
        'start_at': HOUR[0], 'end_at': HOUR[1],
        'voltage_rms': 230, 'current_rms': 10}))
    import_waveform(db, write(tmp_path, 'p1-wave', waveform_doc(
        'W1', 'P1-run', 'A1', phase_waveform(-60))))
    import_total_meter(db, signed_total(tmp_path, kwh=3.5))
    result = audit(db)
    assert result['status'] == 'matched', result['differences']
    # two meters, acquisition, run, RMS page, waveform and signed total
    assert result['source_count'] == 7


def test_same_clock_cannot_be_reregistered_with_a_different_phase(tmp_path):
    db = tmp_path / 'canal.sqlite'
    meter(tmp_path, db)
    run(tmp_path, db)
    acquisition(tmp_path, db, phase=0, acquisition_id='A1', clock='clk-1')
    with pytest.raises(ValueError, match='clock_phase_conflict'):
        acquisition(tmp_path, db, phase=-60, acquisition_id='A2', clock='clk-1')
    # A different physical clock may of course carry a different bias.
    acquisition(tmp_path, db, phase=-60, acquisition_id='A3', clock='clk-2')
    assert len(report(db)['pump_acquisitions']) == 2
