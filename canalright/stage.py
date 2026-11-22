"""Collect a canal bank's one-point water level."""
from contextlib import closing
from .store import connect, read


def import_gauge(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'stage_gauge')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if not doc['gauge_id'] or not doc['canal_id']:
            raise ValueError('invalid_gauge')
        db.execute('INSERT INTO stage_gauge VALUES(?,?,?,?)',
                   (doc['gauge_id'], doc['canal_id'], doc['bank'], doc['id']))
        return {'gauge_id': doc['gauge_id']}


def import_stage(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'stage_reading')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        gauge = db.execute('SELECT * FROM stage_gauge WHERE id=?', (doc['gauge_id'],)).fetchone()
        if gauge is None or doc['height_m'] < 0:
            raise ValueError('invalid_stage')
        db.execute('INSERT INTO stage_reading VALUES(?,?,?,?)',
                   (doc['id'], doc['gauge_id'], doc['at'], doc['height_m']))
        from .flood import update_warning
        return update_warning(db, gauge['canal_id'], doc['id'])
