"""Provisional warning based on one bank's water height alone."""


def update_warning(db, canal_id, source_id):
    row = db.execute('''SELECT r.* FROM stage_reading r
        JOIN stage_gauge g ON g.id=r.gauge_id WHERE g.canal_id=? ORDER BY r.at DESC LIMIT 1''',
                     (canal_id,)).fetchone()
    status = 'high_stage' if row['height_m'] >= 1.2 else 'normal'
    db.execute('INSERT INTO flood_warning VALUES(?,?,?,?) ON CONFLICT(canal_id) DO UPDATE SET status=excluded.status,stage_m=excluded.stage_m,reading_id=excluded.reading_id',
               (canal_id, status, row['height_m'], source_id))
    return {'canal_id': canal_id, 'status': status}
