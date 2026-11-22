"""Evidence-graded determination of continuous crest-overtopping events.

A fifteen-minute upload page only proves the water height at its own
sampling instant.  Between two pages the water may move anywhere inside
the physical envelope set by the probe's rated slew
(``max_slew_m_per_min``) and any pending elevation-calibration
deviation, so this module publishes three-valued windows instead of
interpolating the gaps away:

* ``clear``        -- every feasible continuous history stays below crest;
* ``overtopped``   -- every feasible continuous history is above crest;
* ``undetermined`` -- both kinds of feasible history still exist.

One calibration deviation shifts the site's whole segment jointly (a
single common-mode offset, not per-page noise).  A station power-outage
window is never certified ``clear`` from neighbouring samples alone;
only samples recovered from inside the outage (the gauge's own signed
buffer) can re-grade it.  Events are counted by the time-connectivity
the evidence resolves: ``clear`` windows separate events, while
``undetermined`` windows neither certify nor deny them, so the count is
published as a ``[lo, hi]`` range.  A sealed crest mark only proves the
highest level reached inside its photo window -- it can force the event
floor up but cannot fabricate a curve.  Every publication is versioned,
stores the evidence sequence it used and its own canonical bytes, and
new sampling evidence revises only the period it covers.
"""
import hashlib
import json
from contextlib import closing
from datetime import datetime, timezone

from .store import connect, read, log

CLEAR = 'clear'
OVERTOPPED = 'overtopped'
UNDETERMINED = 'undetermined'


def _parse(at):
    return datetime.fromisoformat(at.replace('Z', '+00:00')).timestamp()


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _site(db, site_id):
    site = db.execute('SELECT * FROM stage_site WHERE id=?', (site_id,)).fetchone()
    if site is None:
        raise ValueError('unknown_site')
    return site


def _samples(db, site):
    """All trusted sample points of the site, keyed by epoch second."""
    out = {}
    for row in db.execute('SELECT at, height_m FROM stage_reading WHERE gauge_id=?', (site['gauge_id'],)):
        out[_parse(row['at'])] = row['height_m']
    for row in db.execute('''SELECT s.at, s.height_m FROM stage_buffer_sample s
        JOIN stage_buffer_page p ON p.id=s.page_id WHERE p.site_id=?''', (site['id'],)):
        out[_parse(row['at'])] = row['height_m']
    return out


def _coverage(known, new_times):
    """Span the new samples may re-grade: extended to the nearest
    existing samples on each side (a deduplicated endpoint keeps its own
    bound, since the envelope beyond it cannot change)."""
    lo, hi = min(new_times), max(new_times)
    before = [t for t in known if t <= lo]
    after = [t for t in known if t >= hi]
    return (max(before) if before else lo, min(after) if after else hi)


def sample_coverage(db, site, at_list):
    return _coverage(_samples(db, site), [_parse(at) for at in at_list])


def _classify_gap(t0, up0, lo0, t1, up1, lo1, r, crest):
    """Three-valued classification of one inter-sample gap.

    ``upper(t)``/``lower(t)`` bound every continuous history that honours
    the slew rate; both are piecewise linear with one kink, so the gap
    splits at kinks and at crest crossings and each piece is classified
    by its midpoint.
    """
    def upper(t):
        return min(up0 + r * (t - t0), up1 + r * (t1 - t))

    def lower(t):
        return max(lo0 - r * (t - t0), lo1 - r * (t1 - t))

    crit = {t0, t1}
    for t in ((up1 - up0 + r * (t0 + t1)) / (2 * r), (lo0 - lo1 + r * (t0 + t1)) / (2 * r)):
        if t0 < t < t1:
            crit.add(t)
    pts = sorted(crit)
    extra = set()
    for a, b in zip(pts, pts[1:]):
        for f in (upper, lower):
            fa, fb = f(a), f(b)
            if (fa - crest) * (fb - crest) < 0:
                extra.add(a + (crest - fa) * (b - a) / (fb - fa))
    pts = sorted(set(pts) | extra)
    out = []
    for a, b in zip(pts, pts[1:]):
        if b - a < 1e-9:
            continue
        m = 0.5 * (a + b)
        state = OVERTOPPED if lower(m) > crest else (CLEAR if upper(m) <= crest else UNDETERMINED)
        out.append((a, b, state, (t0, t1)))
    return out


def _merge_adjacent(windows):
    out = []
    for w in sorted(windows):
        if w[1] - w[0] < 1e-9:
            continue
        if out and out[-1][2] == w[2] and abs(out[-1][1] - w[0]) < 1e-6:
            out[-1] = (out[-1][0], w[1], w[2])
        else:
            out.append((w[0], w[1], w[2]))
    return out


def _classify(site, samples, outages, caldevs, win_lo, win_hi):
    r = site['max_slew_m_per_min'] / 60.0
    crest = site['crest_m']

    def delta(t):
        lo = hi = 0.0
        for c in caldevs:
            if _parse(c['valid_from']) <= t <= _parse(c['valid_to']):
                lo, hi = min(lo, -c['max_offset_m']), max(hi, c['max_offset_m'])
        return lo, hi

    ts = sorted(samples)
    windows = []
    if not ts:
        windows.append((win_lo, win_hi, UNDETERMINED, None))
    else:
        if ts[0] > win_lo:
            windows.append((win_lo, ts[0], UNDETERMINED, None))
        for t0, t1 in zip(ts, ts[1:]):
            lo0, hi0 = delta(t0)
            lo1, hi1 = delta(t1)
            windows += _classify_gap(t0, samples[t0] + hi0, samples[t0] + lo0,
                                     t1, samples[t1] + hi1, samples[t1] + lo1, r, crest)
        if ts[-1] < win_hi:
            windows.append((ts[-1], win_hi, UNDETERMINED, None))
    # A power outage blanks the upload path: any part of it not bracketed
    # by samples recovered from inside the outage stays undetermined.
    forced = []
    for (a, b, state, gap) in windows:
        pieces = [(a, b, state)]
        for o in outages:
            s, e = _parse(o['start_at']), _parse(o['end_at'])
            if gap is not None and gap[0] >= s and gap[1] <= e:
                continue
            nxt = []
            for (x, y, st) in pieces:
                if st == UNDETERMINED or y <= s or x >= e:
                    nxt.append((x, y, st))
                    continue
                if x < s:
                    nxt.append((x, s, st))
                nxt.append((max(x, s), min(y, e), UNDETERMINED))
                if y > e:
                    nxt.append((e, y, st))
            pieces = nxt
        forced += [(x, y, st) for (x, y, st) in pieces if y - x > 1e-9]
    return _merge_adjacent(forced)


def _check_marks(site, samples, caldevs, marks):
    """A sealed mark must be physically reachable: if even the highest
    feasible level inside its photo window stays below the mark, the mark
    is a forgery and the evidence is rejected."""
    crest = site['crest_m']
    over = [m for m in marks if m['mark_m'] > crest]
    if not over:
        return
    r = site['max_slew_m_per_min'] / 60.0
    ts = sorted(samples)

    def dhi(t):
        d = 0.0
        for c in caldevs:
            if _parse(c['valid_from']) <= t <= _parse(c['valid_to']):
                d = max(d, c['max_offset_m'])
        return d

    def upper_max(ws, we):
        if not ts or ws < ts[0] or we > ts[-1]:
            return float('inf')
        best = max((samples[t] + dhi(t) for t in ts if ws <= t <= we), default=float('-inf'))
        for t0, t1 in zip(ts, ts[1:]):
            lo, hi = max(t0, ws), min(t1, we)
            if lo >= hi:
                continue
            up0, up1 = samples[t0] + dhi(t0), samples[t1] + dhi(t1)
            kink = (up1 - up0 + r * (t0 + t1)) / (2 * r)
            for t in (lo, hi, kink):
                if lo <= t <= hi:
                    best = max(best, min(up0 + r * (t - t0), up1 + r * (t1 - t)))
        return best

    for m in over:
        if upper_max(_parse(m['window_start']), _parse(m['window_end'])) < m['mark_m'] - 1e-9:
            raise ValueError('crest_mark_conflict')


def _summarize(site, windows, marks):
    crest = site['crest_m']
    segments, current = [], []
    for w in windows:
        if w[2] == CLEAR:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(w)
    if current:
        segments.append(current)
    lo = hi = 0
    for seg in segments:
        clo = sum(1 for w in seg if w[2] == OVERTOPPED)
        lo += 1 if clo else 0
        hi += max(clo, 1)
    if lo == 0 and any(m['mark_m'] > crest for m in marks):
        lo = 1
    closures = [{'start': _iso(a), 'end': _iso(b)} for a, b, s in windows if s == OVERTOPPED]
    pending = [{'start': _iso(a), 'end': _iso(b)} for a, b, s in windows if s == UNDETERMINED]
    reopen = None
    if windows and windows[-1][2] == CLEAR and any(w[2] == OVERTOPPED for w in windows):
        reopen = _iso(windows[-1][0])
    return {'events_lo': lo, 'events_hi': hi, 'closures': closures, 'pending': pending, 'reopen_at': reopen}


def _merge(prev_windows, fresh, coverage, win_lo, win_hi):
    """Carry the previous publication over outside ``coverage``; the new
    assessment only re-grades the period the new evidence covers (plus
    any territory the evidence window newly reaches)."""
    lo, hi = coverage
    kept = []
    for w in prev_windows:
        a, b, s = _parse(w['start']), _parse(w['end']), w['state']
        if b <= lo or a >= hi:
            kept.append((a, b, s))
        else:
            if a < lo:
                kept.append((a, lo, s))
            if b > hi:
                kept.append((hi, b, s))
    prev_lo = min(_parse(w['start']) for w in prev_windows)
    prev_hi = max(_parse(w['end']) for w in prev_windows)
    regions = [(lo, hi)]
    if win_hi > prev_hi:
        regions.append((prev_hi, win_hi))
    if win_lo < prev_lo:
        regions.append((win_lo, prev_lo))
    regions.sort()
    union = []
    for a, b in regions:
        if union and a <= union[-1][1] + 1e-9:
            union[-1] = (union[-1][0], max(union[-1][1], b))
        else:
            union.append((a, b))
    new = []
    for a, b in union:
        new += [(max(x, a), min(y, b), s) for (x, y, s) in fresh if y > a and x < b]
    return _merge_adjacent(kept + new)


def assess_site(db, site_id, trigger_id, coverage):
    """Recompute the site's overtopping publication.  ``coverage`` is the
    (start, end) span the triggering evidence may re-grade; everything
    outside it is carried over from the previous publication verbatim.
    Publishes a new version only when the graded windows or the summary
    actually change."""
    site = _site(db, site_id)
    samples = _samples(db, site)
    outages = [dict(r) for r in db.execute('SELECT * FROM stage_outage WHERE site_id=?', (site_id,))]
    marks = [dict(r) for r in db.execute('SELECT * FROM crest_mark WHERE site_id=?', (site_id,))]
    caldevs = [dict(r) for r in db.execute('SELECT * FROM stage_caldev WHERE site_id=?', (site_id,))]
    bounds = list(samples)
    bounds += [_parse(o['start_at']) for o in outages] + [_parse(o['end_at']) for o in outages]
    bounds += [_parse(m['window_start']) for m in marks] + [_parse(m['window_end']) for m in marks]
    if coverage is not None:
        bounds += [coverage[0], coverage[1]]
    if len(bounds) < 2:
        return None
    win_lo, win_hi = min(bounds), max(bounds)
    if win_hi - win_lo < 1e-9:
        return None
    fresh = _classify(site, samples, outages, caldevs, win_lo, win_hi)
    _check_marks(site, samples, caldevs, marks)
    prev = db.execute('SELECT * FROM overtop_publication WHERE site_id=? ORDER BY version DESC LIMIT 1',
                      (site_id,)).fetchone()
    if prev is not None and coverage is not None:
        merged = _merge(json.loads(prev['windows']), fresh, coverage, win_lo, win_hi)
    else:
        merged = list(fresh)
    summary = _summarize(site, merged, marks)
    windows_iso = [{'start': _iso(a), 'end': _iso(b), 'state': s} for a, b, s in merged]
    if (prev is not None and json.loads(prev['windows']) == windows_iso
            and prev['events_lo'] == summary['events_lo'] and prev['events_hi'] == summary['events_hi']
            and json.loads(prev['closures']) == summary['closures']
            and json.loads(prev['pending']) == summary['pending']
            and prev['reopen_at'] == summary['reopen_at']):
        return {'site_id': site_id, 'version': prev['version'], 'events_lo': prev['events_lo'],
                'events_hi': prev['events_hi'], 'reopen_at': prev['reopen_at'], 'unchanged': True}
    version = (prev['version'] + 1) if prev else 1
    evidence_seq = db.execute('SELECT COALESCE(MAX(seq),0) FROM evidence').fetchone()[0]
    pub = {'site_id': site_id, 'version': version, 'evidence_seq': evidence_seq,
           'windows': windows_iso,
           'events': {'lo': summary['events_lo'], 'hi': summary['events_hi']},
           'closures': summary['closures'], 'pending': summary['pending'],
           'reopen_at': summary['reopen_at'], 'trigger': trigger_id}
    raw = json.dumps(pub, sort_keys=True, separators=(',', ':')).encode()
    sha = hashlib.sha256(raw).hexdigest()
    db.execute('INSERT INTO overtop_publication VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (site_id, version, evidence_seq, json.dumps(windows_iso), summary['events_lo'],
                summary['events_hi'], json.dumps(summary['closures']), json.dumps(summary['pending']),
                summary['reopen_at'], sha, raw, trigger_id))
    log(db, 'overtop_publication', site_id, 'published', f'version_{version}', trigger_id)
    return {'site_id': site_id, 'version': version, 'events_lo': summary['events_lo'],
            'events_hi': summary['events_hi'], 'reopen_at': summary['reopen_at'], 'unchanged': False}


def import_site(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'stage_site')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        gauge = db.execute('SELECT * FROM stage_gauge WHERE id=?', (doc['gauge_id'],)).fetchone()
        if gauge is None or doc['crest_m'] <= 0 or doc['max_slew_m_per_min'] <= 0 or doc['probe_bandwidth_hz'] <= 0:
            raise ValueError('invalid_site')
        if not doc['sample_clock'] or not doc['upload_clock'] or not doc['buffer_signer'] or doc['surveyed_by'] == doc['witness']:
            raise ValueError('untrusted_site_proof')
        db.execute('INSERT INTO stage_site VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (doc['site_id'], doc['gauge_id'], doc['crest_m'], doc['max_slew_m_per_min'],
                    doc['probe_bandwidth_hz'], doc['sample_clock'], doc['upload_clock'],
                    doc['buffer_signer'], doc['surveyed_by'], doc['witness'], doc['id']))
        log(db, 'stage_site', doc['site_id'], 'active', 'crest_and_bandwidth_registered', doc['id'])
        assess_site(db, doc['site_id'], doc['id'], None)
        return {'site_id': doc['site_id'], 'status': 'active'}


def import_outage(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'stage_outage')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        _site(db, doc['site_id'])
        if doc['start_at'] >= doc['end_at']:
            raise ValueError('invalid_outage_window')
        db.execute('INSERT INTO stage_outage VALUES(?,?,?,?,?)',
                   (doc['id'], doc['site_id'], doc['start_at'], doc['end_at'], doc['id']))
        log(db, 'stage_outage', doc['site_id'], 'recorded', 'station_power_gap', doc['id'])
        assess_site(db, doc['site_id'], doc['id'], (_parse(doc['start_at']), _parse(doc['end_at'])))
        return {'site_id': doc['site_id'], 'status': 'recorded'}


def import_buffer(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'stage_buffer')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        site = _site(db, doc['site_id'])
        samples = doc.get('samples') or []
        cadence = doc.get('cadence_sec', 60)
        start, end = _parse(doc['start_at']), _parse(doc['end_at'])
        if not isinstance(doc['seq'], int) or doc['seq'] < 1 or start >= end or cadence < 1:
            raise ValueError('invalid_buffer_page')
        if not doc.get('signature') or doc.get('signed_by') != site['buffer_signer']:
            raise ValueError('untrusted_buffer_page')
        if db.execute('SELECT 1 FROM stage_buffer_page WHERE site_id=? AND seq=?',
                      (doc['site_id'], doc['seq'])).fetchone():
            raise ValueError('buffer_seq_conflict')
        count = round((end - start) / cadence) + 1
        expected = [start + i * cadence for i in range(count)]
        if (len(samples) != count
                or any(abs(_parse(s['at']) - t) > 1e-6 for s, t in zip(samples, expected))
                or any(s['height_m'] < 0 for s in samples)):
            raise ValueError('buffer_page_incomplete')
        known = _samples(db, site)
        for s in samples:
            t = _parse(s['at'])
            if t in known and abs(known[t] - s['height_m']) > 1e-9:
                raise ValueError('sample_conflict')
        coverage = _coverage(known, [_parse(s['at']) for s in samples])
        db.execute('INSERT INTO stage_buffer_page VALUES(?,?,?,?,?,?,?,?)',
                   (doc['id'], doc['site_id'], doc['seq'], doc['start_at'], doc['end_at'],
                    cadence, doc['signed_by'], doc['id']))
        for s in samples:
            db.execute('INSERT INTO stage_buffer_sample VALUES(?,?,?)', (doc['id'], s['at'], s['height_m']))
        log(db, 'stage_buffer', doc['site_id'], 'recovered', 'signed_local_pages', doc['id'])
        assess_site(db, doc['site_id'], doc['id'], coverage)
        return {'site_id': doc['site_id'], 'status': 'recovered', 'samples': len(samples)}


def import_mark(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'crest_mark')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        _site(db, doc['site_id'])
        if doc['mark_m'] <= 0 or doc['window_start'] >= doc['window_end'] or doc['sealed_by'] == doc['witness']:
            raise ValueError('invalid_crest_mark')
        db.execute('INSERT INTO crest_mark VALUES(?,?,?,?,?,?,?,?)',
                   (doc['id'], doc['site_id'], doc['mark_m'], doc['window_start'], doc['window_end'],
                    doc['sealed_by'], doc['witness'], doc['id']))
        log(db, 'crest_mark', doc['site_id'], 'sealed', 'high_water_mark', doc['id'])
        assess_site(db, doc['site_id'], doc['id'], (_parse(doc['window_start']), _parse(doc['window_end'])))
        return {'site_id': doc['site_id'], 'status': 'sealed'}


def import_caldev(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'stage_caldev')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        _site(db, doc['site_id'])
        if doc['valid_from'] >= doc['valid_to'] or doc['max_offset_m'] < 0:
            raise ValueError('invalid_caldev')
        db.execute('INSERT INTO stage_caldev VALUES(?,?,?,?,?,?)',
                   (doc['id'], doc['site_id'], doc['valid_from'], doc['valid_to'], doc['max_offset_m'], doc['id']))
        log(db, 'stage_caldev', doc['site_id'], 'pending', 'common_mode_elevation_deviation', doc['id'])
        assess_site(db, doc['site_id'], doc['id'], (_parse(doc['valid_from']), _parse(doc['valid_to'])))
        return {'site_id': doc['site_id'], 'status': 'pending'}
