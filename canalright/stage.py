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
        if doc.get('uploaded_at') and doc['uploaded_at'] < doc['at']:
            raise ValueError('invalid_stage')
        site = db.execute('SELECT * FROM stage_site WHERE gauge_id=? ORDER BY id LIMIT 1',
                          (doc['gauge_id'],)).fetchone()
        coverage = None
        if site:
            from .overtop import sample_coverage
            coverage = sample_coverage(db, site, [doc['at']])
        db.execute('INSERT INTO stage_reading VALUES(?,?,?,?,?)',
                   (doc['id'], doc['gauge_id'], doc['at'], doc['height_m'], doc.get('uploaded_at', '')))
        from .flood import update_warning
        result = update_warning(db, gauge['canal_id'], doc['id'])
        if site:
            from .overtop import assess_site
            pub = assess_site(db, site['id'], doc['id'], coverage)
            if pub:
                result['overtop'] = {'version': pub['version'], 'events_lo': pub['events_lo'],
                                     'events_hi': pub['events_hi'], 'reopen_at': pub['reopen_at']}
        return result
