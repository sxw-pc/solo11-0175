"""Provable bounds on net upstream fish migration.

A sonar pack only records device-local tracks: one fish can reappear on the
other end's sonar, one device can hand out several track hints for one fish
(tracking restart) or mirror one fish into two echoes. This module therefore
never pairs echoes greedily. It enumerates every partition of the window's
echoes into fish, keeps the partitions that admit at least one globally
consistent swimming history, and reports the min/max net upstream count over
those feasible worlds together with the witness trajectories.

Feasibility rules, applied to every fish of a world:
* waveform summaries must not conflict (known size classes must agree) —
  similar waveforms keep merge/split candidates open, conflicting ones never
  share a fish;
* crossing the reach between the two sonars needs length/max_speed seconds on
  the true clock, and each (sonar, power-on session) contributes ONE shared
  clock offset, bounded by its calibration, for all its echoes — so a chain
  that is feasible pair by pair can still be globally impossible;
* the later echo of a cross-end leg must read like an arrival from the other
  end ('up' at the upstream sonar, 'down' at the downstream one);
* a fish's first observed motion must match the implied departure, because no
  earlier detection exists that could hide a turnaround;
* same-end re-detections stay loiter/round-trip candidates (blind zones) and
  contribute zero net crossings either way.

Supplementary evidence narrows different times: synced side-view video links
confirm one individual across historical echoes; PIT readers confirm tagged
fish of a new round and cannot rename the old echoes.
"""
import json
from contextlib import closing
from datetime import datetime, timezone
from .store import connect, read


def _ts(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()


def _fishway(db, canal_id):
    row = db.execute('SELECT * FROM fish_fishway WHERE canal_id=? ORDER BY id', (canal_id,)).fetchone()
    if row is None:
        raise ValueError('fishway_unknown')
    return row


def _load_window(db, canal_id, start, end):
    fishway = _fishway(db, canal_id)
    rows = db.execute('''SELECT e.* FROM fish_echo e JOIN fish_sonar s ON s.id=e.sonar_id
        WHERE s.canal_id=? AND e.at>=? AND e.at<=? ORDER BY e.at,e.id''', (canal_id, start, end)).fetchall()
    echoes = []
    for r in rows:
        if r['sonar_id'] == fishway['up_sonar']:
            end_name = 'up'
        elif r['sonar_id'] == fishway['down_sonar']:
            end_name = 'down'
        else:
            continue  # canal sonar that is not part of this fishway reach
        echoes.append({'id': r['id'], 'sonar_id': r['sonar_id'], 'end': end_name, 'at': r['at'],
                       't': _ts(r['at']), 'direction': r['direction'], 'track_hint': r['track_hint'],
                       'waveform': json.loads(r['waveform']), 'session': (r['sonar_id'], r['clock_session']),
                       'clock_lo': r['clock_lo'], 'clock_hi': r['clock_hi'], 'source_id': r['source_id']})
    return fishway, echoes


def _video_constraints(db, canal_id, echoes):
    """Identity links confirmed by synced side-view video (e.g. tail-fin notch)."""
    have = {e['id'] for e in echoes}
    links, applied, caveats = [], [], []
    for v in db.execute('SELECT * FROM fish_video WHERE canal_id=? ORDER BY id', (canal_id,)):
        used = False
        for chain in json.loads(v['links']):
            if chain and all(eid in have for eid in chain):
                links.append(list(chain))
                used = True
        if used:
            applied.append(v['source_id'])
            if v['occluded_parallel']:
                caveats.append('video_occluded_parallel_fish_remain_in_range')
    return links, applied, caveats


def _groups(echoes, links):
    """Pre-merge echoes that video confirmed as one individual."""
    parent = list(range(len(echoes)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pos = {e['id']: i for i, e in enumerate(echoes)}
    for chain in links:
        for eid in chain[1:]:
            parent[find(pos[chain[0]])] = find(pos[eid])
    grouped = {}
    for i in range(len(echoes)):
        grouped.setdefault(find(i), []).append(i)
    return [tuple(sorted(g)) for g in grouped.values()]


def _partitions(items):
    """Yield every set partition of items; each item is a tuple of echo indices."""
    if not items:
        yield []
        return
    first, rest = items[0], items[1:]
    for part in _partitions(rest):
        for i in range(len(part)):
            yield [list(b) for b in part[:i]] + [[first] + list(part[i])] + [list(b) for b in part[i + 1:]]
        yield [list(b) for b in part] + [[first]]


def _leg(e1, e2, first):
    """One leg between consecutive echoes of a fish, or None if impossible."""
    if e1['end'] == e2['end']:
        # milling near the sonar or an unobserved round trip: blind-zone candidate
        return {'from': e1['id'], 'to': e2['id'], 'kind': 'loiter', 'net': 0}
    toward, net, kind = ('up', 1, 'up_crossing') if e1['end'] == 'down' else ('down', -1, 'down_crossing')
    if e2['direction'] not in (toward, 'unknown'):
        return None  # later echo does not read like an arrival from the other end
    if first and e1['direction'] not in (toward, 'unknown'):
        return None  # first-ever sighting already moves away from the implied crossing
    return {'from': e1['id'], 'to': e2['id'], 'kind': kind, 'net': net}


def _offsets_feasible(sessions, constraints):
    """Shared clock offsets: x[b]-x[a] >= w constraints plus calibration bounds."""
    nodes = ['@zero'] + sorted(sessions)
    edges = []
    for key, (lo, hi) in sessions.items():
        edges.append(('@zero', key, lo))
        edges.append((key, '@zero', -hi))
    edges += [(a, b, w) for a, b, w in constraints]
    dist = {n: 0.0 for n in nodes}
    for _ in range(len(nodes)):
        changed = False
        for u, v, w in edges:
            if dist[u] + w > dist[v]:
                dist[v] = dist[u] + w
                changed = True
        if not changed:
            return True
    return False  # positive cycle: no shared clock assignment exists


def _ref(e):
    return {'id': e['id'], 'sonar_id': e['sonar_id'], 'end': e['end'], 'at': e['at'],
            'direction': e['direction'], 'track_hint': e['track_hint']}


def _feasible_worlds(echoes, items, min_travel):
    worlds = []
    for part in _partitions(items):
        sessions, constraints, fishes, net = {}, [], [], 0
        feasible = True
        for block in part:
            idx = sorted((i for item in block for i in item), key=lambda i: (echoes[i]['t'], echoes[i]['id']))
            classes = {echoes[i]['waveform'].get('size_class') for i in idx} - {None, ''}
            if len(classes) > 1:
                feasible = False
                break
            legs = []
            for pos in range(len(idx) - 1):
                leg = _leg(echoes[idx[pos]], echoes[idx[pos + 1]], first=pos == 0)
                if leg is None:
                    feasible = False
                    break
                legs.append(leg)
                net += leg['net']
                if leg['kind'] != 'loiter':
                    w = min_travel - (echoes[idx[pos + 1]]['t'] - echoes[idx[pos]]['t'])
                    constraints.append((echoes[idx[pos]]['session'], echoes[idx[pos + 1]]['session'],
                                        round(w, 9)))  # absorb float noise at the feasibility boundary
            if not feasible:
                break
            for i in idx:
                e = echoes[i]
                lo, hi = sessions.get(e['session'], (e['clock_lo'], e['clock_hi']))
                sessions[e['session']] = (max(lo, e['clock_lo']), min(hi, e['clock_hi']))
            fishes.append({'echoes': [_ref(echoes[i]) for i in idx], 'legs': legs,
                           'net': sum(leg['net'] for leg in legs)})
        if feasible and all(lo <= hi for lo, hi in sessions.values()) \
                and _offsets_feasible(sessions, constraints):
            worlds.append({'net': net, 'fish': fishes})
    return worlds


def _store_net(db, canal_id, start, end, method, lower, upper, worlds, evidence, caveats):
    version = db.execute('''SELECT COUNT(*) FROM fish_net
        WHERE canal_id=? AND window_start=? AND window_end=? AND method=?''',
                         (canal_id, start, end, method)).fetchone()[0] + 1
    net_id = f'net:{canal_id}:{method}:{start}:{end}:v{version}'
    db.execute('INSERT INTO fish_net VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (net_id, canal_id, start, end, method, lower, upper,
                json.dumps(worlds, ensure_ascii=False, sort_keys=True),
                json.dumps(evidence), json.dumps(caveats), version))
    return {'id': net_id, 'canal_id': canal_id, 'window': {'start': start, 'end': end}, 'method': method,
            'lower': lower, 'upper': upper, 'worlds': worlds, 'evidence': evidence,
            'caveats': caveats, 'version': version}


def compute_net(db, canal_id, start, end):
    """Provable [lower, upper] net upstream count over one time window."""
    fishway, echoes = _load_window(db, canal_id, start, end)
    links, applied, caveats = _video_constraints(db, canal_id, echoes)
    items = _groups(echoes, links)
    min_travel = fishway['length_m'] * 3.6 / fishway['max_speed_kmh']
    worlds = _feasible_worlds(echoes, items, min_travel)
    nets = [w['net'] for w in worlds]
    lower, upper = min(nets), max(nets)
    evidence = sorted({e['source_id'] for e in echoes} | {fishway['source_id']} | set(applied))
    result = _store_net(db, canal_id, start, end, 'sonar', lower, upper, worlds, evidence, caveats)
    result['lower_witness'] = nets.index(lower)
    result['upper_witness'] = nets.index(upper)
    return result


def pit_net(db, canal_id, start, end):
    """Confirmed net upstream count of tagged fish from cross-end PIT reads."""
    fishway = _fishway(db, canal_id)
    rows = db.execute('SELECT * FROM fish_pit WHERE canal_id=? AND start_at>=? AND end_at<=? ORDER BY id',
                      (canal_id, start, end)).fetchall()
    if not rows:
        raise ValueError('pit_window_uncovered')
    by_tag = {}
    for row in rows:
        for r in json.loads(row['reads']):
            by_tag.setdefault(r['tag'], []).append(r)
    confirmed, trajectories = 0, []
    for tag in sorted(by_tag):
        reads = sorted(by_tag[tag], key=lambda r: (_ts(r['at']), r['end']))
        legs, net = [], 0
        for a, b in zip(reads, reads[1:]):
            if a['end'] == b['end']:
                kind, leg_net = 'loiter', 0
            elif a['end'] == 'down':
                kind, leg_net = 'up_crossing', 1
            else:
                kind, leg_net = 'down_crossing', -1
            legs.append({'from': a['at'], 'to': b['at'], 'kind': kind, 'net': leg_net})
            net += leg_net
        confirmed += net
        trajectories.append({'tag': tag, 'reads': reads, 'legs': legs, 'net': net})
    caveats = ['pit_covers_tagged_fish_only', 'unmarked_fish_not_determined']
    evidence = sorted({r['source_id'] for r in rows} | {fishway['source_id']})
    result = _store_net(db, canal_id, start, end, 'pit', confirmed, confirmed,
                        [{'net': confirmed, 'fish': trajectories}], evidence, caveats)
    result['coverage'] = 'tagged_only'
    return result


def import_video(path, file):
    """Register synced side-view video evidence: continuous fin-match identity links."""
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'fish_video')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        window = doc['window']
        if not doc.get('canal_id') or window['start'] >= window['end'] or not doc.get('links'):
            raise ValueError('invalid_video')
        for chain in doc['links']:
            if len(chain) < 2:
                raise ValueError('invalid_video')
            for eid in chain:
                row = db.execute('SELECT at FROM fish_echo WHERE id=?', (eid,)).fetchone()
                if row is None or not window['start'] <= row['at'] <= window['end']:
                    raise ValueError('invalid_video')
        db.execute('INSERT INTO fish_video VALUES(?,?,?,?,?,?,?)',
                   (doc['id'], doc['canal_id'], window['start'], window['end'],
                    json.dumps(doc['links'], sort_keys=True), 1 if doc.get('occluded_parallel') else 0, doc['id']))
        return {'video': doc['id'], 'links': len(doc['links'])}


def import_pit(path, file):
    """Register a PIT-reader round: harmless tag reads at both ends of the fishway."""
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'fish_pit')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        window = doc['window']
        _fishway(db, doc['canal_id'])
        if window['start'] >= window['end'] or not doc.get('reads'):
            raise ValueError('invalid_pit')
        for r in doc['reads']:
            if r.get('end') not in ('up', 'down') or not r.get('tag') or not r.get('at') \
                    or not window['start'] <= r['at'] <= window['end']:
                raise ValueError('invalid_pit')
        db.execute('INSERT INTO fish_pit VALUES(?,?,?,?,?,?)',
                   (doc['id'], doc['canal_id'], window['start'], window['end'],
                    json.dumps(doc['reads'], sort_keys=True), doc['id']))
        return {'pit': doc['id'], 'reads': len(doc['reads'])}


def run_net(path, canal_id, start, end):
    with closing(connect(path)) as db, db:
        return compute_net(db, canal_id, start, end)


def run_pit_net(path, canal_id, start, end):
    with closing(connect(path)) as db, db:
        return pit_net(db, canal_id, start, end)
