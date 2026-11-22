import json
from pathlib import Path

import pytest

from canalright import ion_bounds
from canalright.quality import (
    import_bundle, import_ec, import_ion, import_electrode,
    import_ion_batch, import_site,
)
from canalright.store import connect, report
from canalright.audit import audit

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'sodium-intake.json'
TOL = ion_bounds.TOL


# --------------------------------------------------------------------------
# Pure ion_bounds logic: EC=1200 cannot identify a sodium fraction by itself.
# --------------------------------------------------------------------------

def test_ec_index_is_na_plus_ca_and_drift_is_shared():
    # Synthetic calibration fluid: EC index = Na + Ca, same unit.
    drift = ion_bounds.batch_drift(600 + 600, 1200)
    assert drift == 0
    assert ion_bounds.corrected_ec(1200, drift) == 1200
    drift_b2 = ion_bounds.batch_drift(1000, 1050)
    assert drift_b2 == 50
    assert ion_bounds.corrected_ec(1200, drift_b2) == 1150


def test_ec_alone_leaves_full_sodium_range():
    assert ion_bounds.ec_only_range() == (0.0, 1.0)
    # Both parcels of the worked example are plausible under EC=1200 alone.
    assert ion_bounds.ion_matches_ec(1200, 900, 300, 0)
    assert ion_bounds.ion_matches_ec(1200, 300, 900, 0)


def test_two_identical_ec_pages_pin_opposite_decisions():
    ratio_a = ion_bounds.sodium_ratio(900, 300)   # parcel 甲
    ratio_b = ion_bounds.sodium_ratio(300, 900)   # parcel 乙
    assert ratio_a == 0.75 and ratio_b == 0.25
    assert ion_bounds.decide(ratio_a, ratio_a, 'rice') == 'forbidden'
    assert ion_bounds.decide(ratio_b, ratio_b, 'rice') == 'suitable'
    assert ion_bounds.decide(*ion_bounds.ec_only_range(), 'rice') == 'pending'


def test_threshold_is_strict_above_half():
    assert ion_bounds.decide(0.5, 0.5, 'rice') == 'suitable'
    assert ion_bounds.decide(0.5 + 2 * TOL, 0.5 + 2 * TOL, 'rice') == 'forbidden'


def test_shared_batch_drift_must_have_one_parameter():
    # Reading 1200 with Na+Ca=1150 requires drift 50; the other needs drift 0.
    intervals = [
        ion_bounds.ion_drift_interval(1200, 945, 205),
        ion_bounds.ion_drift_interval(1200, 300, 900),
    ]
    assert ion_bounds.intersect_intervals(intervals) is None
    # Independent per-point drift values would mask this; the batch check must
    # reject any release set incompatible with one calibration fluid.
    compatible = [
        ion_bounds.ion_drift_interval(1200, 945, 205),
        ion_bounds.ion_drift_interval(1200, 345, 805),
    ]
    joint = ion_bounds.intersect_intervals(compatible)
    assert joint == pytest.approx((50, 50), abs=TOL)
    assert ion_bounds.drift_supported(50, joint)
    assert not ion_bounds.drift_supported(0, joint)


def test_electrode_cross_check_implies_ca_and_rejects_impossible_na():
    ca, ratio = ion_bounds.electrode_cross_check(1200, 400, 0)
    assert ca == 800 and ratio == pytest.approx(1 / 3)
    with pytest.raises(ValueError, match='electrode_ec_crosscheck_failed'):
        ion_bounds.electrode_cross_check(1200, 1300, 0)


def test_window_coverage_helpers_keep_points_inside_mixing_boundaries():
    assert ion_bounds.window_contains('07:00', '09:00', '08:00')
    assert not ion_bounds.window_contains('07:00', '09:00', '09:30')
    assert ion_bounds.window_intersection('07:00', '09:00', '07:30', '08:30') == ('07:30', '08:30')
    assert ion_bounds.window_intersection('07:00', '08:00', '08:00', '09:00') is None
    assert ion_bounds.remainder_segments('07:00', '09:00', '07:30', '08:30') == [
        ('07:00', '07:30'), ('08:30', '09:00')]


# --------------------------------------------------------------------------
# Persisted evidence chain, driven through the delivered fixture.
# --------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    path = tmp_path / 'sodium.sqlite'
    import_bundle(path, FIXTURE)
    return path


def _windows(report_obj, site_id, parcel_id):
    return [w for w in report_obj['crop_windows']
            if w['site_id'] == site_id and w['parcel_id'] == parcel_id]


def _statuses(report_obj, site_id, parcel_id):
    return {w['status'] for w in _windows(report_obj, site_id, parcel_id)}


def test_acceptance_1_ec_1200_keeps_provisional_hold_as_real_control(db):
    """The seed behaviour survives: EC staging still says hold at EC >= 1000."""
    rep = report(db)
    advice = {r['site_id']: r for r in rep['crop_advice']}
    assert advice['S1']['status'] == 'hold'
    assert advice['S1']['ec_us'] == 1200
    # The staging column never carries the ion-based final status.
    assert all(r['status'] in ('hold', 'suitable') for r in rep['crop_advice'])


def test_acceptance_2_same_ec_page_splits_into_forbidden_and_suitable(db):
    rep = report(db)
    pa, pb = _windows(rep, 'S1', 'P-A'), _windows(rep, 'S1', 'P-B')
    assert len(pa) == 1 and pa[0]['status'] == 'forbidden'
    assert pa[0]['ratio_lo'] == pa[0]['ratio_hi'] == 0.75
    assert len(pb) == 1 and pb[0]['status'] == 'suitable'
    assert pb[0]['ratio_lo'] == pb[0]['ratio_hi'] == 0.25
    # Same original EC page, site and timestamp; different bottles/parcels.
    readings = {r['source_id']: r for r in rep['quality_readings']}
    assert (readings['ec-S1-PA']['ec_us'], readings['ec-S1-PA']['at']) == \
           (readings['ec-S1-PB']['ec_us'], readings['ec-S1-PB']['at']) == (1200, '2026-11-22T08:00:00Z')
    samples = {s['source_id']: s for s in rep['quality_samples']}
    assert samples['ec-S1-PA']['bottle_id'] != samples['ec-S1-PB']['bottle_id']


def test_acceptance_3_bottle_link_drift_and_mixing_boundaries_do_not_fabricate(db, tmp_path):
    # Ion analysis must reference the retained bottle of the same parcel.
    rogue = {'id': 'ion-rogue', 'kind': 'ion_analysis', 'site_id': 'S1',
             'parcel_id': 'P-A', 'bottle_id': 'BTL-B-1', 'method': 'chromatography',
             'seal_intact': True, 'na': 300, 'ca': 900}
    with pytest.raises(ValueError, match='bottle_not_linked_to_parcel'):
        import_ion(db, _write(tmp_path, 'rogue.json', rogue))

    # Composition must satisfy Na+Ca = corrected EC under the batch drift.
    bad_comp = dict(rogue, id='ion-bad-comp', parcel_id='P-D',
                    bottle_id='BTL-D-1', na=1100, ca=300)
    with pytest.raises(ValueError, match='ion_ec_crosscheck_failed'):
        import_ion(db, _write(tmp_path, 'bad-comp.json', bad_comp))

    # A broken seal cannot restore the historical parcel via chromatography.
    broken = {'id': 'ion-broken', 'kind': 'ion_analysis', 'site_id': 'S1',
              'parcel_id': 'P-C', 'bottle_id': 'BTL-C-1', 'method': 'chromatography',
              'seal_intact': True, 'na': 300, 'ca': 900}
    with pytest.raises(ValueError, match='seal_broken_historical_parcel'):
        import_ion(db, _write(tmp_path, 'broken.json', broken))
    rep = report(db)
    pc = _windows(rep, 'S1', 'P-C')
    assert pc and all(w['status'] == 'pending' for w in pc)
    pending = {p['bottle_id']: p['reason'] for p in rep['pending_bottles']}
    assert pending['BTL-C-1'] == 'seal_broken_historical_parcel'

    # Electrode point outside any same-parcel mixing window is refused.
    patrol = {'id': 'elec-outside', 'kind': 'electrode_patrol', 'site_id': 'S1',
              'parcel_id': 'P-D', 'batch_id': 'B1',
              'cover_start': '2026-11-24T05:00:00Z', 'cover_end': '2026-11-24T06:00:00Z',
              'points': [{'at': '2026-11-24T05:30:00Z', 'na': 400}]}
    with pytest.raises(ValueError, match='point_outside_mixing_window'):
        import_electrode(db, _write(tmp_path, 'outside.json', patrol))


def test_acceptance_3b_cross_station_shared_drift_rejects_independent_values(db, tmp_path):
    # S3 and S4 share batch B2 (drift +50): both chromatograms are accepted
    # jointly and diverge in their crop decisions through the same parameter.
    rep = report(db)
    assert _statuses(rep, 'S3', 'P-X') == {'forbidden'}
    assert _statuses(rep, 'S4', 'P-Y') == {'suitable'}
    batch = {b['id']: b for b in rep['ion_batches']}['B2']
    assert batch['drift'] == 50

    # A new parcel on B2 whose composition demands a different drift breaks the
    # single shared parameter and must be rejected atomically.
    _site_batch_ec(tmp_path, db, 'P-Z', 'BTL-Z-1', 'S3', '2026-11-25', 1200, 'B2')
    ion = {'id': 'ion-z', 'kind': 'ion_analysis', 'site_id': 'S3', 'parcel_id': 'P-Z',
           'bottle_id': 'BTL-Z-1', 'method': 'chromatography', 'seal_intact': True,
           'na': 340, 'ca': 800}  # sum 1140 passes reading check, demands drift 60
    with pytest.raises(ValueError, match='shared_batch_drift_incompatible'):
        import_ion(db, _write(tmp_path, 'ion-z.json', ion))
    # The rejected analysis leaves no result row behind.
    assert not [r for r in report(db)['ion_results'] if r['source_id'] == 'ion-z']


def test_acceptance_4_chromatography_and_electrode_cover_different_windows(db):
    rep = report(db)
    # Historical chromatography back-signs the full old sampling window.
    pb = _windows(rep, 'S1', 'P-B')[0]
    assert (pb['window_start'], pb['window_end'], pb['basis']) == (
        '2026-11-22T07:00:00Z', '2026-11-22T09:00:00Z', 'ion_chromatography')

    # The next-round electrode patrol certifies only the covered intersection;
    # the flanks stay pending and the bottle is queued for recheck.
    pd = _windows(rep, 'S1', 'P-D')
    segments = {(w['window_start'][11:16], w['window_end'][11:16]): w for w in pd}
    certified = segments[('07:30', '08:30')]
    assert certified['status'] == 'suitable'
    assert certified['basis'] == 'sodium_electrode_patrol'
    assert certified['ratio_lo'] == pytest.approx(1 / 3, abs=TOL)
    assert certified['ratio_hi'] == pytest.approx(5 / 12, abs=TOL)
    assert segments[('07:00', '07:30')]['status'] == 'pending'
    assert segments[('08:30', '09:00')]['status'] == 'pending'
    pending = {p['bottle_id']: p for p in rep['pending_bottles']}
    assert pending['BTL-D-1']['reason'] == 'electrode_gap_needs_recheck'


def test_new_water_can_never_replace_an_old_parcel(db, tmp_path):
    # Re-using a parcel id on a neighbouring day is refused outright.
    doc = {'id': 'ec-reuse', 'kind': 'quality_ec', 'site_id': 'S1',
           'at': '2026-11-25T08:00:00Z', 'ec_us': 1200, 'parcel_id': 'P-B',
           'bottle_id': 'BTL-B-2', 'volume_ml': 500, 'seal_intact': True,
           'batch_id': 'B1',
           'window_start': '2026-11-25T07:00:00Z', 'window_end': '2026-11-25T09:00:00Z'}
    with pytest.raises(ValueError, match='parcel_reuse_forbidden'):
        import_ec(db, _write(tmp_path, 'reuse.json', doc))
    # The old parcel's signed window is untouched.
    assert _statuses(report(db), 'S1', 'P-B') == {'suitable'}


def test_acceptance_5_every_state_is_traceable_and_replay_is_deterministic(db):
    rep = report(db)
    # Original EC pages, ion samples, final windows and change sources all kept.
    assert len(rep['quality_readings']) == 6
    assert len(rep['ion_results']) == 4
    assert len(rep['electrode_points']) == 3
    sources = {w['source_id'] for w in rep['crop_windows']}
    assert {'ion-S1-PA', 'ion-S1-PB', 'elec-S1-PD', 'ec-S1-PC'} <= sources
    # Each final decision is in the append-only log with its evidence source.
    decision_sources = {d['evidence_id'] for d in rep['decisions']
                        if d['object_type'] == 'crop_window'}
    assert sources <= decision_sources
    # Raw bytes of every document survive for audit rebuild.
    with connect(db) as check:
        kinds = {r['kind'] for r in check.execute('SELECT DISTINCT kind FROM evidence')}
    assert {'quality_ec', 'ion_analysis', 'electrode_patrol',
            'ion_calibration_batch', 'quality_site'} <= kinds
    assert audit(db) == {'status': 'matched', 'source_count': 16, 'differences': []}
    # Re-importing the whole bundle is idempotent.
    again = import_bundle(db, FIXTURE)
    assert all('replayed' in item for item in again['results'])
    assert audit(db)['status'] == 'matched'


def test_later_lab_result_recomputes_only_its_own_parcel(tmp_path):
    path = tmp_path / 'late.sqlite'
    docs = json.loads(FIXTURE.read_text())
    # Stop before the two S1 chromatograms arrive.
    head = [d for d in docs if d['id'] not in ('ion-S1-PA', 'ion-S1-PB')]
    bundle = tmp_path / 'head.json'
    bundle.write_text(json.dumps(head))
    import_bundle(path, bundle)
    before = report(path)
    assert _statuses(before, 'S1', 'P-A') == {'pending'}
    assert _statuses(before, 'S1', 'P-B') == {'pending'}

    import_ion(path, _write(tmp_path, 'ion-a.json', _doc(docs, 'ion-S1-PA')))
    middle = report(path)
    # Only P-A flips; P-B remains pending and P-D electrode windows are intact.
    assert _statuses(middle, 'S1', 'P-A') == {'forbidden'}
    assert _statuses(middle, 'S1', 'P-B') == {'pending'}
    assert len(_windows(middle, 'S1', 'P-D')) == 3
    import_ion(path, _write(tmp_path, 'ion-b.json', _doc(docs, 'ion-S1-PB')))
    assert _statuses(report(path), 'S1', 'P-B') == {'suitable'}
    assert audit(path)['status'] == 'matched'


def test_failed_evidence_rolls_back_and_cannot_touch_other_parcels(db, tmp_path):
    bad = {'id': 'ion-fail', 'kind': 'ion_analysis', 'site_id': 'S1', 'parcel_id': 'P-D',
           'bottle_id': 'BTL-D-1', 'method': 'chromatography', 'seal_intact': True,
           'na': 9999, 'ca': 9999}
    with pytest.raises(ValueError, match='ion_ec_crosscheck_failed'):
        import_ion(db, _write(tmp_path, 'bad.json', bad))
    rep = report(db)
    assert not [r for r in rep['ion_results'] if r['source_id'] == 'ion-fail']
    # P-D keeps only its electrode-derived windows; nothing was overwritten.
    assert {w['basis'] for w in _windows(rep, 'S1', 'P-D')} == {
        'sodium_electrode_patrol', 'ec_only_no_ion_coverage'}

    # A patrol whose points land outside every same-parcel mixing window
    # cannot certify, and it never disturbs another parcel's signed window.
    patrol = {'id': 'elec-foreign', 'kind': 'electrode_patrol', 'site_id': 'S1',
              'parcel_id': 'P-B', 'batch_id': 'B1',
              'cover_start': '2026-12-01T07:30:00Z', 'cover_end': '2026-12-01T08:30:00Z',
              'points': [{'at': '2026-12-01T08:00:00Z', 'na': 400}]}
    with pytest.raises(ValueError, match='point_outside_mixing_window'):
        import_electrode(db, _write(tmp_path, 'foreign.json', patrol))
    pb = _windows(report(db), 'S1', 'P-B')
    assert len(pb) == 1 and pb[0]['basis'] == 'ion_chromatography'


def test_legacy_ec_page_still_drives_only_the_provisional_column(tmp_path):
    path = tmp_path / 'legacy.sqlite'
    import_site(path, _write(tmp_path, 'site.json', {
        'id': 'S1', 'kind': 'quality_site', 'site_id': 'S1',
        'canal_id': 'C1', 'crop': 'rice'}))
    out = import_ec(path, _write(tmp_path, 'ec.json', {
        'id': 'E1', 'kind': 'quality_ec', 'site_id': 'S1',
        'at': '2026-11-22T08:00:00Z', 'ec_us': 1200}))
    assert out == {'site_id': 'S1', 'status': 'hold'}
    assert report(path)['crop_advice'][0]['ec_us'] == 1200
    assert report(path)['crop_windows'] == []


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _write(tmp_path, name, doc):
    file = tmp_path / name
    file.write_text(json.dumps(doc))
    return file


def _doc(docs, doc_id):
    return next(d for d in docs if d['id'] == doc_id)


def _site_batch_ec(tmp_path, db, parcel_id, bottle_id, site_id, day, ec, batch_id):
    import_ec(db, _write(tmp_path, f'ec-{parcel_id}.json', {
        'id': f'ec-{site_id}-{parcel_id}', 'kind': 'quality_ec', 'site_id': site_id,
        'at': f'{day}T08:00:00Z', 'ec_us': ec, 'parcel_id': parcel_id,
        'bottle_id': bottle_id, 'volume_ml': 500, 'seal_intact': True,
        'batch_id': batch_id,
        'window_start': f'{day}T07:00:00Z', 'window_end': f'{day}T09:00:00Z'}))
