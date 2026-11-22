"""Capture inclinometer readings on a canal embankment."""
from contextlib import closing
from .store import connect, read


def import_sensor(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bank_sensor')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not doc['sensor_id'] or not doc['segment']:
            raise ValueError('invalid_sensor')
        db.execute('INSERT INTO bank_sensor VALUES(?,?,?,?)',
                   (doc['sensor_id'], doc['canal_id'], doc['segment'], doc['id']))
        return {'sensor_id': doc['sensor_id']}


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
        return advise(db, sensor['segment'])
