"""Versioned feasible bed profiles for dredge measurement.

A raw echo page leaves every beam with one or more candidate reflector
depths (hard bed, weed top, multipath), so no single guess may be booked as
paid volume.  The rules encoded here:

* one sound speed profile per water mass converts travel times for *all*
  beams of a survey - a favourable speed may never be picked per beam, and
  evidence from a period the profile does not cover may not borrow it;
* a measurable continuity bound prunes adjacent-beam bed slopes, so from an
  ambiguous page only complete feasible bed surfaces survive, each with its
  own contract volume;
* unswept shore cells are booked as manual-sounding volume, never filled
  with zero siltation;
* the two approved evidence routes resolve cells differently: a full
  dual-frequency sweep separates weed scatter from hard-bed reflection per
  beam and can sign a continuous volume, while plumb sediment cores certify
  only their own cell (depth, material, disposal) and may not be averaged
  over the line.

Payable, possibly-needed and manual-sounding volumes are booked separately.
Profiles are versioned and immutable so acceptance can recompute every
range from the raw evidence.
"""
import json
import re
from contextlib import closing
from .store import connect, read, log

CUT_DATUM_M = 15.0      # contract bed depth the canal must be restored to
MAX_BED_SLOPE = 1.0     # measurable continuity bound, m of depth per m of distance
TIE_TOLERANCE_M = 0.3   # dual-frequency returns closer than this agree on one reflector
EPS = 1e-6


def import_sound_speed(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'sound_speed_profile')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if (doc['speed_m_s'] <= 0 or doc['valid_from'] >= doc['valid_to']
                or doc['technician'] == doc['witness']):
            raise ValueError('invalid_sound_speed_proof')
        clash = db.execute('''SELECT 1 FROM sound_speed_profile WHERE canal_id=?
                              AND NOT(valid_to<=? OR ?<=valid_from)''',
                           (doc['canal_id'], doc['valid_from'], doc['valid_to'])).fetchone()
        if clash:
            raise ValueError('sound_speed_overlap')
        db.execute('INSERT INTO sound_speed_profile VALUES(?,?,?,?,?,?,?,?)',
                   (doc['profile_id'], doc['canal_id'], doc['speed_m_s'], doc['valid_from'],
                    doc['valid_to'], doc['technician'], doc['witness'], doc['id']))
        log(db, 'sound_speed_profile', doc['profile_id'], 'active', 'shared_water_mass_calibration', doc['id'])
        return {'profile_id': doc['profile_id'], 'status': 'active'}


def import_frequency_sweep(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'frequency_sweep')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        line = db.execute('SELECT * FROM bathy_line WHERE id=?', (doc['line_id'],)).fetchone()
        returns = doc.get('returns') or []
        if line is None or not doc.get('observed_at') or not returns:
            raise ValueError('invalid_frequency_sweep')
        seen = set()
        for ret in returns:
            key = (ret.get('beam_id'), ret.get('freq_khz'))
            if (not ret.get('beam_id') or not ret.get('freq_khz') or ret.get('time_ms', 0) <= 0
                    or ret.get('strength', 0) <= 0 or key in seen):
                raise ValueError('invalid_frequency_sweep')
            seen.add(key)
        for ret in returns:
            db.execute('INSERT INTO frequency_return VALUES(?,?,?,?,?,?,?,?)',
                       (f"{doc['id']}:{ret['beam_id']}:{ret['freq_khz']}", doc['line_id'], ret['beam_id'],
                        ret['freq_khz'], ret['time_ms'], ret['strength'], doc['observed_at'], doc['id']))
        log(db, 'frequency_sweep', doc['id'], 'recorded', 'dual_frequency_returns', doc['id'])
        return {'sweep_id': doc['id'], 'beams': len({ret['beam_id'] for ret in returns})}


def import_core(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'sediment_core')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        line = db.execute('SELECT * FROM bathy_line WHERE id=?', (doc['line_id'],)).fetchone()
        if (line is None or not doc.get('observed_at')
                or not 0 <= doc['station_m'] <= line['length_m']
                or not 0 <= doc['offset_m'] <= line['width_m']
                or doc['bed_depth_m'] < 0 or not doc.get('material') or not doc.get('disposal')):
            raise ValueError('invalid_sediment_core')
        db.execute('INSERT INTO sediment_core VALUES(?,?,?,?,?,?,?,?,?)',
                   (doc['core_id'], doc['line_id'], doc['station_m'], doc['offset_m'],
                    doc['bed_depth_m'], doc['material'], doc['disposal'], doc['observed_at'], doc['id']))
        log(db, 'sediment_core', doc['core_id'], 'recorded', doc['material'], doc['id'])
        return {'core_id': doc['core_id'], 'status': 'recorded'}


def build_profile(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bed_profile_request')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        line = db.execute('SELECT * FROM bathy_line WHERE id=?', (doc['line_id'],)).fetchone()
        if line is None:
            raise ValueError('unknown_line')
        if not doc.get('surveyed_at'):
            raise ValueError('invalid_profile_request')
        upto_seq = db.execute('SELECT seq FROM evidence WHERE id=?', (doc['id'],)).fetchone()[0]
        result = _profile(db, line, doc['surveyed_at'], upto_seq)
        version = db.execute('SELECT COUNT(*) FROM bed_profile WHERE line_id=?',
                             (doc['line_id'],)).fetchone()[0] + 1
        profile_id = f"{doc['line_id']}-v{version}"
        status = 'resolved' if result['possible_m3'] == 0 and result['manual_m3'] == 0 else 'ranged'
        db.execute('INSERT INTO bed_profile VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (profile_id, doc['line_id'], version, doc['surveyed_at'], result['speed_m_s'],
                    result['payable_m3'], result['possible_m3'], result['manual_m3'],
                    result['gap_area_m2'], result['unresolved_area_m2'], status, doc['id']))
        for label in ('min_cut', 'max_cut'):
            hypothesis = result['hypotheses'][label]
            db.execute('INSERT INTO profile_hypothesis VALUES(?,?,?,?,?)',
                       (f'{profile_id}-{label}', profile_id, label, hypothesis['cut_m3'],
                        json.dumps(hypothesis['depths'], sort_keys=True)))
        log(db, 'bed_profile', profile_id, status, 'shared_sound_speed_continuity', doc['id'])
        return {'profile_id': profile_id, 'version': version, 'status': status, **result}


def recompute_profile(path, profile_id):
    with closing(connect(path)) as db:
        profile = db.execute('SELECT * FROM bed_profile WHERE id=?', (profile_id,)).fetchone()
        if profile is None:
            raise ValueError('unknown_profile')
        line = db.execute('SELECT * FROM bathy_line WHERE id=?', (profile['line_id'],)).fetchone()
        upto_seq = db.execute('SELECT seq FROM evidence WHERE id=?', (profile['source_id'],)).fetchone()[0]
        result = _profile(db, line, profile['surveyed_at'], upto_seq)
        stored = {row['label']: row for row in db.execute(
            'SELECT * FROM profile_hypothesis WHERE profile_id=?', (profile_id,))}
        matched = (result['payable_m3'] == profile['payable_m3']
                   and result['possible_m3'] == profile['possible_m3']
                   and result['manual_m3'] == profile['manual_m3']
                   and all(json.dumps(result['hypotheses'][label]['depths'], sort_keys=True) == stored[label]['depths']
                           for label in ('min_cut', 'max_cut')))
        return {'profile_id': profile_id, 'version': profile['version'], 'matched': matched,
                'payable_m3': profile['payable_m3'], 'possible_m3': profile['possible_m3'],
                'manual_m3': profile['manual_m3']}


def material_disposal(path, line_id):
    with closing(connect(path)) as db:
        line = db.execute('SELECT * FROM bathy_line WHERE id=?', (line_id,)).fetchone()
        if line is None:
            raise ValueError('unknown_line')
        _, _, cell_area = _layout(db, line)
        return [{'core_id': row['id'], 'station_m': row['station_m'], 'material': row['material'],
                 'disposal': row['disposal'], 'cut_m3': round(_cut(row['bed_depth_m'], cell_area), 6)}
                for row in db.execute('SELECT * FROM sediment_core WHERE line_id=? ORDER BY station_m',
                                      (line_id,))]


def _profile(db, line, surveyed_at, upto_seq):
    # a version is a pure function of the evidence logged up to its request
    seen = 'SELECT id FROM evidence WHERE seq<=?'
    ssp = db.execute(f'''SELECT * FROM sound_speed_profile WHERE canal_id=? AND valid_from<=? AND ?<valid_to
                         AND source_id IN ({seen}) ORDER BY valid_from DESC LIMIT 1''',
                     (line['canal_id'], surveyed_at, surveyed_at, upto_seq)).fetchone()
    if ssp is None:
        raise ValueError('sound_speed_profile_missing')
    speed = ssp['speed_m_s']
    echoes = db.execute(f'SELECT * FROM bathy_echo WHERE line_id=? AND source_id IN ({seen})',
                        (line['id'], upto_seq)).fetchall()
    returns = db.execute(f'SELECT * FROM frequency_return WHERE line_id=? AND source_id IN ({seen})',
                         (line['id'], upto_seq)).fetchall()
    cores = db.execute(f'SELECT * FROM sediment_core WHERE line_id=? AND source_id IN ({seen})',
                       (line['id'], upto_seq)).fetchall()
    for stamp in ([e['observed_at'] for e in echoes] + [r['observed_at'] for r in returns]
                  + [c['observed_at'] for c in cores]):
        if stamp and not ssp['valid_from'] <= stamp < ssp['valid_to']:
            raise ValueError('sound_speed_window_miss')
    cells, spacing, cell_area = _layout(db, line, upto_seq)
    candidates = {}
    for echo in echoes:
        candidates.setdefault(echo['beam_id'], set()).add(_depth(echo['time_ms'], speed))
    fixed = _resolved_cells(cells, spacing, speed, returns, cores)
    for i, cell in enumerate(cells):
        if i in fixed:
            cell.update(state='fixed', cands=[fixed[i]])
        elif cell['beam'] is None or not candidates.get(cell['beam']):
            cell.update(state='gap', cands=[])
        else:
            cell.update(state='echo', cands=sorted(candidates[cell['beam']]))
    min_total, max_total, min_depths, max_depths = _extremes(cells, spacing, cell_area)
    gaps = sum(1 for cell in cells if cell['state'] == 'gap')
    ambiguous = sum(1 for cell in cells if cell['state'] == 'echo' and len(cell['cands']) > 1)
    surfaces = {}
    for label, depths in (('min_cut', min_depths), ('max_cut', max_depths)):
        surfaces[label] = {(cell['beam'] if cell['beam'] is not None else f'#{i}'): depths[i]
                           for i, cell in enumerate(cells) if cell['state'] != 'gap'}
    return {'speed_m_s': speed,
            'payable_m3': round(min_total, 6),
            'possible_m3': round(max_total - min_total, 6),
            'manual_m3': round(gaps * cell_area * CUT_DATUM_M, 6),
            'gap_area_m2': round(gaps * cell_area, 6),
            'unresolved_area_m2': round((ambiguous + gaps) * cell_area, 6),
            'hypotheses': {
                'min_cut': {'cut_m3': round(min_total, 6), 'depths': surfaces['min_cut']},
                'max_cut': {'cut_m3': round(max_total, 6), 'depths': surfaces['max_cut']}}}


def _resolved_cells(cells, spacing, speed, returns, cores):
    beam_index = {cell['beam']: i for i, cell in enumerate(cells) if cell['beam'] is not None}
    fixed = {}
    by_beam = {}
    for ret in returns:
        by_beam.setdefault(ret['beam_id'], {}).setdefault(ret['freq_khz'], []).append(ret)
    for beam, freqs in by_beam.items():
        if beam not in beam_index or len(freqs) < 2:
            continue  # single-frequency or unknown beam: cannot separate weed from bed
        if any(len(hits) != 1 for hits in freqs.values()):
            continue  # ambiguous returns: frequency-degraded beam keeps its range
        low_depth = _depth(freqs[min(freqs)][0]['time_ms'], speed)
        high_depth = _depth(freqs[max(freqs)][0]['time_ms'], speed)
        if low_depth + TIE_TOLERANCE_M < high_depth:
            continue  # physically inconsistent split: keeps its range
        fixed[beam_index[beam]] = low_depth  # low frequency carries the hard-bed reflection
    cored = set()
    for core in cores:
        idx = min(int(core['station_m'] / spacing), len(cells) - 1)
        if idx in cored:
            raise ValueError('core_cell_conflict')
        cored.add(idx)
        fixed[idx] = core['bed_depth_m']  # plumb truth certifies its own cell only
    return fixed


def _extremes(cells, spacing, cell_area):
    n = len(cells)
    certain = 0.0
    min_total = max_total = 0.0
    min_depths = [None] * n
    max_depths = [None] * n
    i = 0
    while i < n:
        state = cells[i]['state']
        if state == 'gap':
            i += 1
            continue
        if state == 'fixed':
            depth = cells[i]['cands'][0]
            certain += _cut(depth, cell_area)
            min_depths[i] = max_depths[i] = depth
            i += 1
            continue
        j = i
        while j < n and cells[j]['state'] == 'echo':
            j += 1
        run = cells[i:j]
        low, low_path = _run_extreme(run, spacing, cell_area, want_min=True)
        high, high_path = _run_extreme(run, spacing, cell_area, want_min=False)
        if low is None:
            raise ValueError('no_feasible_bed_surface')
        min_total += low
        max_total += high
        for k in range(j - i):
            min_depths[i + k] = low_path[k]
            max_depths[i + k] = high_path[k]
        i = j
    return certain + min_total, certain + max_total, min_depths, max_depths


def _run_extreme(run, spacing, cell_area, want_min):
    bound = MAX_BED_SLOPE * spacing + EPS
    prev = {depth: _cut(depth, cell_area) for depth in run[0]['cands']}
    parents = []
    for cell in run[1:]:
        cur, par = {}, {}
        for depth in cell['cands']:
            links = [(value, parent) for parent, value in prev.items() if abs(depth - parent) <= bound]
            if links:
                value, parent = min(links) if want_min else max(links)
                cur[depth] = value + _cut(depth, cell_area)
                par[depth] = parent
        if not cur:
            return None, None
        parents.append(par)
        prev = cur
    pick = min if want_min else max
    end = pick(prev, key=lambda depth: prev[depth])
    path = [end]
    for par in reversed(parents):
        path.append(par[path[-1]])
    path.reverse()
    return prev[end], path


def _layout(db, line, upto_seq=None):
    seen = 'SELECT id FROM evidence WHERE seq<=?'
    if upto_seq is None:
        upto_seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM evidence').fetchone()[0]
    beams = {row['beam_id'] for row in db.execute(
        f'SELECT DISTINCT beam_id FROM bathy_echo WHERE line_id=? AND source_id IN ({seen})',
        (line['id'], upto_seq))}
    beams |= {row['beam_id'] for row in db.execute(
        f'SELECT DISTINCT beam_id FROM frequency_return WHERE line_id=? AND source_id IN ({seen})',
        (line['id'], upto_seq))}
    beams = sorted(beams, key=_beam_key)
    # observed beams fill the leading cells in sorted order; unobserved
    # trailing cells are shore gaps and stay out of every feasible surface
    n = max(line['beam_count'], len(beams), 1)
    spacing = line['length_m'] / n
    cell_area = line['length_m'] * line['width_m'] / n
    cells = [{'beam': beams[i] if i < len(beams) else None} for i in range(n)]
    return cells, spacing, cell_area


def _beam_key(beam_id):
    return [int(token) if token.isdigit() else token for token in re.split(r'(\d+)', str(beam_id))]


def _depth(time_ms, speed_m_s):
    return round(time_ms * speed_m_s / 2000.0, 6)


def _cut(depth_m, cell_area):
    return max(0.0, CUT_DATUM_M - depth_m) * cell_area
