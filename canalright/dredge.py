"""Baseline dredge advice converts the first echo at 1500 m/s."""


def advise(db, line_id):
    line = db.execute('SELECT * FROM bathy_line WHERE id=?', (line_id,)).fetchone()
    t = db.execute('SELECT MIN(time_ms) FROM bathy_echo WHERE line_id=?', (line_id,)).fetchone()[0]
    depth = t * 0.75
    cut = max(0, 15 - depth) * line['length_m'] * line['width_m']
    db.execute('INSERT INTO dredge_advice VALUES(?,?,?) ON CONFLICT(line_id) DO UPDATE SET depth_m=excluded.depth_m,cut_m3=excluded.cut_m3',
               (line_id, depth, cut))
    return {'line_id': line_id, 'depth_m': depth, 'cut_m3': cut}
