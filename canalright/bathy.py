"""Capture canal depth echoes with a provisional fixed sound speed."""
import json
from contextlib import closing
from pathlib import Path
from .store import connect, read


def import_line(path, file):
    with closing(connect(path)) as db, db:
        doc, fresh = read(db, file, 'bathy_line')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        beam_count = doc.get('beam_count', 0)
        if (doc['length_m'] <= 0 or doc['width_m'] <= 0
                or not isinstance(beam_count, int) or beam_count < 0):
            raise ValueError('invalid_line')
        db.execute('INSERT INTO bathy_line VALUES(?,?,?,?,?,?)',
                   (doc['line_id'], doc['canal_id'], doc['length_m'], doc['width_m'], beam_count, doc['id']))
        return {'line_id': doc['line_id']}


def import_echo(path, file):
    with closing(connect(path)) as db, db:
        if json.loads(Path(file).read_text(encoding='utf8')).get('kind') == 'bathy_echo_page':
            return _import_page(db, file)
        doc, fresh = read(db, file, 'bathy_echo')
        if not fresh:
            return {'id': doc['id'], 'replayed': True}
        line = db.execute('SELECT * FROM bathy_line WHERE id=?', (doc['line_id'],)).fetchone()
        if line is None or doc['time_ms'] <= 0 or doc['strength'] <= 0:
            raise ValueError('invalid_echo')
        db.execute('INSERT INTO bathy_echo VALUES(?,?,?,?,?,?,?)',
                   (doc['id'], doc['id'], doc['line_id'], doc['beam_id'], doc['time_ms'],
                    doc['strength'], doc.get('observed_at', '')))
        from .dredge import advise
        return advise(db, doc['line_id'])


def _import_page(db, file):
    doc, fresh = read(db, file, 'bathy_echo_page')
    if not fresh:
        return {'id': doc['id'], 'replayed': True}
    line = db.execute('SELECT * FROM bathy_line WHERE id=?', (doc['line_id'],)).fetchone()
    echoes = doc.get('echoes') or []
    if line is None or not doc.get('observed_at') or not echoes:
        raise ValueError('invalid_echo_page')
    seen = set()
    for echo in echoes:
        key = (echo.get('beam_id'), echo.get('time_ms'))
        if (not echo.get('beam_id') or echo.get('time_ms', 0) <= 0
                or echo.get('strength', 0) <= 0 or key in seen):
            raise ValueError('invalid_echo_page')
        seen.add(key)
    for echo in echoes:
        db.execute('INSERT INTO bathy_echo VALUES(?,?,?,?,?,?,?)',
                   (f"{doc['id']}:{echo['beam_id']}:{echo['time_ms']}", doc['id'], doc['line_id'],
                    echo['beam_id'], echo['time_ms'], echo['strength'], doc['observed_at']))
    from .dredge import advise
    return advise(db, doc['line_id'])
