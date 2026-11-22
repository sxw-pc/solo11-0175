"""Capture inclinometer readings on a canal embankment.

The sensor page registers the canal channel and the embankment segment.
Tilt pages store the raw absolute inclination and only trigger a provisional
``repair_review`` flag; structural displacement is decided separately in
``canalright.slip_proof`` because the raw page cannot separate wall rotation
from steel bracket thermal expansion.
"""
from contextlib import closing
from .store import connect, read, log


def import_sensor(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bank_sensor')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not doc.get('sensor_id') or not doc.get('canal_id') or not doc.get('segment'):
            raise ValueError('invalid_sensor')
        db.execute('INSERT INTO bank_sensor VALUES(?,?,?,?)',
                   (doc['sensor_id'], doc['canal_id'], doc['segment'], doc['id']))
        log(db, 'bank_sensor', doc['sensor_id'], 'registered',
            f"channel={doc['canal_id']};segment={doc['segment']}", doc['id'])
        return {'sensor_id': doc['sensor_id'], 'canal_id': doc['canal_id'],
                'segment': doc['segment']}


def import_sensor_zero(path, file):
    """Register the instrument zeroing time and its reference temperature."""
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bank_sensor_zero')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not db.execute('SELECT 1 FROM bank_sensor WHERE id=?',
                          (doc['sensor_id'],)).fetchone():
            raise ValueError('unknown_sensor')
        if not doc.get('zeroed_at') or 'ref_temperature_c' not in doc:
            raise ValueError('invalid_zero_page')
        db.execute('INSERT INTO bank_sensor_zero VALUES(?,?,?,?)',
                   (doc['sensor_id'], doc['zeroed_at'],
                    float(doc['ref_temperature_c']), doc['id']))
        return {'sensor_id': doc['sensor_id'], 'zeroed_at': doc['zeroed_at']}


def import_tilt(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bank_tilt')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        sensor = db.execute('SELECT * FROM bank_sensor WHERE id=?', (doc['sensor_id'],)).fetchone()
        if sensor is None or not -90 <= doc['tilt_deg'] <= 90:
            raise ValueError('invalid_tilt')
        db.execute('INSERT INTO bank_tilt VALUES(?,?,?,?)',
                   (doc['id'], doc['sensor_id'], doc['at'], doc['tilt_deg']))
        from .embankment import advise
        return advise(db, sensor['segment'], doc['id'])
