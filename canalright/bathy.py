"""Capture canal depth echoes with a provisional fixed sound speed."""
from contextlib import closing
from .store import connect, read


def import_line(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bathy_line')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        if doc['length_m'] <= 0 or doc['width_m'] <= 0:
            raise ValueError('invalid_line')
        db.execute('INSERT INTO bathy_line VALUES(?,?,?,?,?)',
                   (doc['line_id'], doc['canal_id'], doc['length_m'], doc['width_m'], doc['id']))
        return {'line_id': doc['line_id']}


def import_echo(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bathy_echo')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        line = db.execute('SELECT * FROM bathy_line WHERE id=?', (doc['line_id'],)).fetchone()
        if line is None or doc['time_ms'] <= 0 or doc['strength'] <= 0:
            raise ValueError('invalid_echo')
        db.execute('INSERT INTO bathy_echo VALUES(?,?,?,?,?)',
                   (doc['id'], doc['line_id'], doc['beam_id'], doc['time_ms'], doc['strength']))
        from .dredge import advise
        return advise(db, doc['line_id'])
