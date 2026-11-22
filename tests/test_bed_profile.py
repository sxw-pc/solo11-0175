"""Acceptance: feasible bed profiles decide what may be signed for dredging.

The 10 m x 5 m line L1 carries an identical 16 ms + 20 ms echo pair on
every beam at a shared 1500 m/s: 12 m and 15 m.  Scenario A (16 ms is the
bed, 20 ms is multipath) owes (15-12) x 50 = 150 m3; scenario B (16 ms is
the weed top, 20 ms is the bed) owes 0.  Times, strengths and dimensions
cannot tell them apart, so the profile must keep both feasible surfaces
until material evidence arrives.
"""
import json
from pathlib import Path
import pytest
from canalright.bathy import import_line, import_echo
from canalright.bed_profile import (import_sound_speed, import_frequency_sweep,
                                    import_core, build_profile, recompute_profile,
                                    material_disposal)
from canalright.dredge import sign_instruction, record_actual, payment_summary
from canalright.store import report

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'weed-bed-echo.json'
AT = '2026-11-01T08:00:00Z'
BEAMS = [f'B{i:02d}' for i in range(1, 11)]


def write(tmp, name, doc):
    p = tmp / f'{name}.json'
    p.write_text(json.dumps(doc))
    return p


def line(**kw):
    d = dict(kind='bathy_line', id='L1', line_id='L1', canal_id='C1',
             length_m=10, width_m=5, beam_count=10)
    d.update(kw)
    return d


def ssp(**kw):
    d = dict(kind='sound_speed_profile', id='ssp1', profile_id='SSP-C1', canal_id='C1',
             speed_m_s=1500, valid_from='2026-11-01T00:00:00Z', valid_to='2026-11-02T00:00:00Z',
             technician='hydro_lab', witness='water_office')
    d.update(kw)
    return d


def request(**kw):
    d = dict(kind='bed_profile_request', id='req1', line_id='L1', surveyed_at=AT)
    d.update(kw)
    return d


def instruction(**kw):
    d = dict(kind='dredge_instruction', id='ins1', instruction_id='D1', line_id='L1',
             profile_id='L1-v2', cut_m3=150, contractor='dredge_co')
    d.update(kw)
    return d


def actual(aid, instruction_id, volume):
    return dict(kind='dredge_actual', id=aid, instruction_id=instruction_id,
                volume_m3=volume, material='silt', disposal='spoil_bank')


def sweep(sid, beams, skip_low=(), high_ms=14, low_ms=16):
    returns = []
    for beam in beams:
        returns.append({'beam_id': beam, 'freq_khz': 200, 'time_ms': high_ms, 'strength': 0.5})
        if beam not in skip_low:
            returns.append({'beam_id': beam, 'freq_khz': 38, 'time_ms': low_ms, 'strength': 0.7})
    return dict(kind='frequency_sweep', id=sid, line_id='L1',
                observed_at='2026-11-01T09:00:00Z', returns=returns)


def core(cid, station, depth=12, material='silt', disposal='spoil_bank'):
    return dict(kind='sediment_core', id=f'ev-{cid}', core_id=cid, line_id='L1',
                station_m=station, offset_m=2.5, bed_depth_m=depth, material=material,
                disposal=disposal, observed_at='2026-11-01T10:00:00Z')


def base(tmp):
    db = tmp / 'canal.sqlite'
    import_line(db, write(tmp, 'line', line()))
    import_sound_speed(db, write(tmp, 'ssp', ssp()))
    return db


# acceptance 1: the fixed starting point stays 20 ms -> 15 m / 0 m3
def test_single_20ms_echo_stays_zero_cut(tmp_path):
    db = tmp_path / 'c.sqlite'
    import_line(db, write(tmp_path, 'line', line(id='L0', line_id='L0', beam_count=1)))
    advice = import_echo(db, write(tmp_path, 'echo', dict(
        kind='bathy_echo', id='E1', line_id='L0', beam_id='B1', time_ms=20, strength=0.8)))
    assert advice['depth_m'] == 15 and advice['cut_m3'] == 0
    import_sound_speed(db, write(tmp_path, 'ssp', ssp()))
    profile = build_profile(db, write(tmp_path, 'req', request(line_id='L0')))
    assert (profile['payable_m3'], profile['possible_m3'], profile['manual_m3']) == (0, 0, 0)
    assert profile['status'] == 'resolved'
    assert profile['hypotheses']['min_cut']['depths'] == {'B1': 15.0}


# acceptance 2: one original 16/20 page -> two feasible surfaces, 150 and 0
def test_dual_echo_one_original_two_feasible_surfaces(tmp_path):
    db = base(tmp_path)
    advice = import_echo(db, FIXTURE)
    # the retained legacy advice still picks the earliest echo: 12 m / 150 m3
    assert advice['depth_m'] == 12 and advice['cut_m3'] == 150
    profile = build_profile(db, write(tmp_path, 'req', request()))
    assert profile['payable_m3'] == 0 and profile['possible_m3'] == 150
    assert profile['manual_m3'] == 0 and profile['gap_area_m2'] == 0
    assert profile['unresolved_area_m2'] == 50
    assert profile['status'] == 'ranged'
    low, high = profile['hypotheses']['min_cut'], profile['hypotheses']['max_cut']
    assert low['cut_m3'] == 0 and all(low['depths'][b] == 15 for b in BEAMS)
    assert high['cut_m3'] == 150 and all(high['depths'][b] == 12 for b in BEAMS)
    assert set(low['depths']) == set(BEAMS) == set(high['depths'])
    # no guess may be signed as paid volume from the ambiguous original
    with pytest.raises(ValueError, match='instruction_exceeds_payable'):
        sign_instruction(db, write(tmp_path, 'instr', instruction(profile_id='L1-v1')))


# acceptance 3a: one sound speed per water mass, never borrowed across periods
def test_sound_speed_is_shared_and_not_borrowed_across_periods(tmp_path):
    db = tmp_path / 'c.sqlite'
    import_line(db, write(tmp_path, 'line', line()))
    import_echo(db, FIXTURE)
    with pytest.raises(ValueError, match='sound_speed_profile_missing'):
        build_profile(db, write(tmp_path, 'req', request()))
    import_sound_speed(db, write(tmp_path, 'ssp', ssp()))
    late = dict(kind='bathy_echo_page', id='late-page', line_id='L1',
                observed_at='2026-12-01T08:00:00Z',
                echoes=[{'beam_id': 'B01', 'time_ms': 20, 'strength': 0.9}])
    import_echo(db, write(tmp_path, 'late', late))
    # the November calibration does not cover a December echo
    with pytest.raises(ValueError, match='sound_speed_window_miss'):
        build_profile(db, write(tmp_path, 'req2', request(id='req2')))
    # and a December survey may not borrow the November water mass
    with pytest.raises(ValueError, match='sound_speed_profile_missing'):
        build_profile(db, write(tmp_path, 'req3', request(id='req3', surveyed_at='2026-12-01T08:00:00Z')))


def test_sound_speed_windows_must_not_overlap(tmp_path):
    db = tmp_path / 'c.sqlite'
    import_sound_speed(db, write(tmp_path, 'ssp', ssp()))
    with pytest.raises(ValueError, match='sound_speed_overlap'):
        import_sound_speed(db, write(tmp_path, 'ssp2', ssp(
            id='ssp2', profile_id='SSP-C1b',
            valid_from='2026-11-01T12:00:00Z', valid_to='2026-11-03T00:00:00Z')))


def test_one_speed_for_all_beams_scales_the_whole_range(tmp_path):
    db = tmp_path / 'c.sqlite'
    import_line(db, write(tmp_path, 'line', line(id='L2', line_id='L2', canal_id='C2')))
    import_sound_speed(db, write(tmp_path, 'ssp', ssp(
        id='ssp2', profile_id='SSP-C2', canal_id='C2', speed_m_s=1400)))
    page = json.loads(FIXTURE.read_text())
    page.update(id='L2-page', line_id='L2')
    import_echo(db, write(tmp_path, 'page', page))
    profile = build_profile(db, write(tmp_path, 'req', request(id='r2', line_id='L2')))
    # 1400 m/s for every beam: 11.2 m and 14.0 m, never a per-beam pick
    assert profile['speed_m_s'] == 1400
    assert profile['payable_m3'] == 50 and profile['possible_m3'] == 140


# acceptance 3b: the continuity bound prunes mixed surfaces
def test_continuity_bound_prunes_mixed_surfaces(tmp_path):
    db = tmp_path / 'c.sqlite'
    import_line(db, write(tmp_path, 'line', line(id='L3', line_id='L3', length_m=2, beam_count=2)))
    import_sound_speed(db, write(tmp_path, 'ssp', ssp()))
    page = dict(kind='bathy_echo_page', id='p3', line_id='L3', observed_at=AT, echoes=[
        {'beam_id': 'B1', 'time_ms': 16, 'strength': 0.6},
        {'beam_id': 'B2', 'time_ms': 16, 'strength': 0.6},
        {'beam_id': 'B2', 'time_ms': 20, 'strength': 0.9}])
    import_echo(db, write(tmp_path, 'page', page))
    profile = build_profile(db, write(tmp_path, 'req', request(id='r3', line_id='L3')))
    # B1 is certainly 12 m; a 3 m jump to a 15 m bed at B2 breaks the bound
    assert profile['payable_m3'] == 30 and profile['possible_m3'] == 0


def test_continuity_violation_has_no_feasible_surface(tmp_path):
    db = tmp_path / 'c.sqlite'
    import_line(db, write(tmp_path, 'line', line(id='L4', line_id='L4', length_m=2, beam_count=2)))
    import_sound_speed(db, write(tmp_path, 'ssp', ssp()))
    page = dict(kind='bathy_echo_page', id='p4', line_id='L4', observed_at=AT, echoes=[
        {'beam_id': 'B1', 'time_ms': 16, 'strength': 0.6},
        {'beam_id': 'B2', 'time_ms': 20, 'strength': 0.9}])
    import_echo(db, write(tmp_path, 'page', page))
    with pytest.raises(ValueError, match='no_feasible_bed_surface'):
        build_profile(db, write(tmp_path, 'req', request(id='r4', line_id='L4')))


# acceptance 3c: unswept shore blanks are not extrapolated to zero siltation
def test_unswept_shore_gap_is_not_zero_filled(tmp_path):
    db = tmp_path / 'c.sqlite'
    import_line(db, write(tmp_path, 'line', line(id='L5', line_id='L5')))
    import_sound_speed(db, write(tmp_path, 'ssp', ssp()))
    echoes = [{'beam_id': f'B{i:02d}', 'time_ms': 20, 'strength': 0.9} for i in range(1, 7)]
    import_echo(db, write(tmp_path, 'page', dict(
        kind='bathy_echo_page', id='p5', line_id='L5', observed_at=AT, echoes=echoes)))
    profile = build_profile(db, write(tmp_path, 'req', request(id='r5', line_id='L5')))
    assert profile['payable_m3'] == 0 and profile['possible_m3'] == 0
    # 4 unswept cells x 5 m2 stay out of every surface and wait for manual sounding
    assert profile['gap_area_m2'] == 20 and profile['manual_m3'] == 300
    assert profile['unresolved_area_m2'] == 20
    assert profile['status'] == 'ranged'


# acceptance 4a: full dual-frequency sweep signs a continuous line volume
def test_dual_frequency_full_coverage_signs_continuous_volume(tmp_path):
    db = base(tmp_path)
    import_echo(db, FIXTURE)
    build_profile(db, write(tmp_path, 'req', request()))
    import_frequency_sweep(db, write(tmp_path, 'sweep', sweep('sw1', BEAMS)))
    profile = build_profile(db, write(tmp_path, 'req2', request(id='req2')))
    # 200 kHz sees the weed top at 10.5 m, 38 kHz the hard bed at 12 m
    assert profile['version'] == 2 and profile['status'] == 'resolved'
    assert profile['payable_m3'] == 150 and profile['possible_m3'] == 0
    assert profile['unresolved_area_m2'] == 0
    outcome = sign_instruction(db, write(tmp_path, 'instr', instruction()))
    assert outcome['status'] == 'signed' and outcome['payable_remaining_m3'] == 0
    with pytest.raises(ValueError, match='instruction_exceeds_payable'):
        sign_instruction(db, write(tmp_path, 'instr2', instruction(id='ins2', instruction_id='D2', cut_m3=1)))
    # sonar separates weed from bed but cannot classify hauled material
    assert material_disposal(db, 'L1') == []


def test_degraded_shore_frequency_keeps_its_range(tmp_path):
    db = base(tmp_path)
    import_echo(db, FIXTURE)
    build_profile(db, write(tmp_path, 'req', request()))
    import_frequency_sweep(db, write(tmp_path, 'sweep', sweep('sw1', BEAMS, skip_low={'B10'})))
    profile = build_profile(db, write(tmp_path, 'req2', request(id='req2')))
    # B10 lost its low frequency: its 12/15 m range survives, 9 cells sign
    assert profile['payable_m3'] == 135 and profile['possible_m3'] == 15
    assert profile['unresolved_area_m2'] == 5
    outcome = sign_instruction(db, write(tmp_path, 'instr', instruction(cut_m3=135)))
    assert outcome['status'] == 'signed'


# acceptance 4b: three plumb cores pay locally, never averaged over 50 m2
def test_three_cores_pay_locally_without_extrapolation(tmp_path):
    db = base(tmp_path)
    import_echo(db, FIXTURE)
    build_profile(db, write(tmp_path, 'req', request()))
    import_core(db, write(tmp_path, 'core1', core('K1', 1.5)))
    import_core(db, write(tmp_path, 'core2', core('K2', 4.5)))
    import_core(db, write(tmp_path, 'core3',
                          core('K3', 7.5, material='contaminated_silt', disposal='hazardous_facility')))
    profile = build_profile(db, write(tmp_path, 'req2', request(id='req2')))
    # three cells certified at 12 m: 3 x (15-12) x 5 m2; the rest keeps its range
    assert profile['payable_m3'] == 45
    assert profile['possible_m3'] == 105 and profile['manual_m3'] == 0
    assert profile['unresolved_area_m2'] == 35
    outcome = sign_instruction(db, write(tmp_path, 'instr', instruction(cut_m3=45)))
    assert outcome['status'] == 'signed'
    with pytest.raises(ValueError, match='instruction_exceeds_payable'):
        sign_instruction(db, write(tmp_path, 'instr2', instruction(id='ins2', instruction_id='D2', cut_m3=1)))
    disposal = material_disposal(db, 'L1')
    assert [(d['material'], d['cut_m3']) for d in disposal] == [
        ('silt', 15), ('silt', 15), ('contaminated_silt', 15)]
    assert disposal[2]['disposal'] == 'hazardous_facility'


# acceptance 5: raw waveforms, profile versions, instructions and payments trace
def test_versions_instructions_actuals_are_traceable(tmp_path):
    db = base(tmp_path)
    import_echo(db, FIXTURE)
    v1 = build_profile(db, write(tmp_path, 'req', request()))
    import_frequency_sweep(db, write(tmp_path, 'sweep', sweep('sw1', BEAMS)))
    v2 = build_profile(db, write(tmp_path, 'req2', request(id='req2')))
    sign_instruction(db, write(tmp_path, 'instr', instruction()))
    assert record_actual(db, write(tmp_path, 'act1', actual('A1', 'D1', 120)))['status'] == 'within_instruction'
    over = record_actual(db, write(tmp_path, 'act2', actual('A2', 'D1', 40)))
    assert over['status'] == 'overrun' and over['cumulative_m3'] == 160
    # every version recomputes from the raw evidence
    assert recompute_profile(db, v1['profile_id'])['matched']
    assert recompute_profile(db, v2['profile_id'])['matched']
    stored = report(db)
    assert [p['version'] for p in stored['bed_profiles']] == [1, 2]
    assert stored['bed_profiles'][0]['payable_m3'] == 0  # v1 untouched by v2
    assert len(stored['profile_hypotheses']) == 4
    assert len(stored['bathy_echoes']) == 20  # raw waveforms still on file
    # the three ledgers stay distinct: sonar estimate, instruction, hauled actual
    assert stored['dredge_advice'][0]['cut_m3'] == 150  # legacy 15 m advice retained
    summary = payment_summary(db, 'L1')
    assert summary['legacy_advice_cut_m3'] == 150
    assert summary['instructed_m3'] == 150 and summary['actual_m3'] == 160
    # replaying the same originals changes nothing
    assert import_echo(db, FIXTURE)['replayed']
    assert build_profile(db, write(tmp_path, 'req3', request()))['replayed']
    assert len(report(db)['bed_profiles']) == 2
