"""A provisional count of local echo tracks, not confirmed passages."""


def count(db, canal_id):
    n = db.execute('''SELECT COUNT(DISTINCT e.sonar_id||':'||e.track_hint)
        FROM fish_echo e JOIN fish_sonar s ON s.id=e.sonar_id WHERE s.canal_id=?''',
                   (canal_id,)).fetchone()[0]
    db.execute('INSERT INTO fish_passage VALUES(?,?) ON CONFLICT(canal_id) DO UPDATE SET provisional_count=excluded.provisional_count',
               (canal_id, n))
    return {'canal_id': canal_id, 'provisional_count': n}
