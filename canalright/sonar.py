"""Capture canal sonar echoes with device-local track hints.

Echo originals additionally carry a waveform summary, a swimming direction,
the valid detection band they were sampled in, and the device clock
calibration of their power-on session. The clock offset is a property of the
(session), not of the single echo: every echo of one session shares it.
"""
import json
from contextlib import closing
from .store import connect, read

DIRECTIONS = ('up', 'down', 'unknown')


def _echo_extra(doc, err):
    direction = doc.get('direction', 'unknown')
    if direction not in DIRECTIONS:
        raise ValueError(err)
    band = doc.get('band') or (0, 0)
    clock = doc.get('clock') or {}
    offset = clock.get('offset_s', (0, 0))
    if len(band) != 2 or band[0] > band[1] or len(offset) != 2 or offset[0] > offset[1]:
        raise ValueError(err)
    if band[1] and not band[0] <= doc['range_m'] <= band[1]:
        raise ValueError(err)  # echo outside its valid detection band
    return {'direction': direction,
            'waveform': json.dumps(doc.get('waveform', {}), sort_keys=True),
            'band_lo': band[0], 'band_hi': band[1],
            'clock_session': clock.get('session', ''), 'clock_lo': offset[0], 'clock_hi': offset[1]}


def _insert_echo(db, echo_id, source_id, doc, err):
    if not doc.get('track_hint') or doc['range_m'] < 0:
        raise ValueError(err)
    x = _echo_extra(doc, err)
    db.execute('INSERT INTO fish_echo VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (echo_id, source_id, doc['sonar_id'], doc['track_hint'], doc['at'], doc['range_m'],
                x['direction'], x['waveform'], x['band_lo'], x['band_hi'],
                x['clock_session'], x['clock_lo'], x['clock_hi']))


def import_sonar(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'fish_sonar')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not doc['sonar_id'] or not doc['canal_id'] or not doc.get('bank'):
            raise ValueError('invalid_sonar')
        db.execute('INSERT INTO fish_sonar VALUES(?,?,?,?)',
                   (doc['sonar_id'], doc['canal_id'], doc['bank'], doc['id']))
        return {'sonar_id': doc['sonar_id']}


def import_echo(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'fish_echo')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        sonar = db.execute('SELECT * FROM fish_sonar WHERE id=?', (doc['sonar_id'],)).fetchone()
        if sonar is None:
            raise ValueError('invalid_echo')
        _insert_echo(db, doc['id'], doc['id'], doc, 'invalid_echo')
        from .passage import count
        return count(db, sonar['canal_id'])


def import_pack(path, file):
    """Import a sealed sonar package: fishway geometry, sonar banks, echo originals.

    The package is stored as one evidence blob; every member row points back to
    it via source_id, so raw echoes stay traceable to the sealed pack.
    """
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'fish_pack')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        fishway = doc['fishway']
        sonars = doc['sonars']
        banks = {s.get('sonar_id'): s.get('bank') for s in sonars}
        if (not doc.get('canal_id') or fishway['length_m'] <= 0 or fishway['max_speed_kmh'] <= 0
                or not all(banks.values())
                or fishway['up_sonar'] not in banks or fishway['down_sonar'] not in banks):
            raise ValueError('invalid_pack')
        for sonar_id, bank in banks.items():
            old = db.execute('SELECT * FROM fish_sonar WHERE id=?', (sonar_id,)).fetchone()
            if old is None:
                db.execute('INSERT INTO fish_sonar VALUES(?,?,?,?)', (sonar_id, doc['canal_id'], bank, doc['id']))
            elif old['canal_id'] != doc['canal_id'] or old['bank'] != bank:
                raise ValueError('invalid_pack')
        if db.execute('SELECT 1 FROM fish_fishway WHERE id=?', (fishway['id'],)).fetchone():
            raise ValueError('invalid_pack')
        db.execute('INSERT INTO fish_fishway VALUES(?,?,?,?,?,?,?)',
                   (fishway['id'], doc['canal_id'], fishway['up_sonar'], fishway['down_sonar'],
                    fishway['length_m'], fishway['max_speed_kmh'], doc['id']))
        for echo in doc['echoes']:
            if echo.get('sonar_id') not in banks:
                raise ValueError('invalid_pack')
            if db.execute('SELECT 1 FROM fish_echo WHERE id=?', (echo['id'],)).fetchone():
                raise ValueError('invalid_pack')
            _insert_echo(db, echo['id'], doc['id'], echo, 'invalid_pack')
        from .passage import count
        result = count(db, doc['canal_id'])
        return {'pack': doc['id'], 'sonars': len(sonars), 'echoes': len(doc['echoes']),
                'provisional_count': result['provisional_count']}
