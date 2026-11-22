"""Capture pump electrical RMS summaries."""
from contextlib import closing
from .store import connect, read


def import_meter(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'pump_meter')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not doc['pump_id'] or doc['rate_per_kwh'] < 0:
            raise ValueError('invalid_meter')
        db.execute('INSERT INTO pump_meter VALUES(?,?,?,?)',
                   (doc['meter_id'], doc['pump_id'], doc['rate_per_kwh'], doc['id']))
        return {'meter_id': doc['meter_id']}


def import_sample(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'pump_sample')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        meter = db.execute('SELECT * FROM pump_meter WHERE id=?', (doc['meter_id'],)).fetchone()
        if meter is None or doc['start_at'] >= doc['end_at'] or doc['voltage_rms'] < 0 or doc['current_rms'] < 0:
            raise ValueError('invalid_sample')
        db.execute('INSERT INTO pump_sample VALUES(?,?,?,?,?,?)',
                   (doc['id'], doc['meter_id'], doc['start_at'], doc['end_at'],
                    doc['voltage_rms'], doc['current_rms']))
        from .tariff import estimate
        return estimate(db, doc['meter_id'])
