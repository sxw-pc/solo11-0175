"""A provisional apparent-power charge without phase information."""


def estimate(db, meter_id):
    meter = db.execute('SELECT * FROM pump_meter WHERE id=?', (meter_id,)).fetchone()
    sample = db.execute('SELECT * FROM pump_sample WHERE meter_id=? ORDER BY end_at DESC LIMIT 1',
                        (meter_id,)).fetchone()
    apparent_kwh = sample['voltage_rms'] * sample['current_rms'] / 1000
    charge = apparent_kwh * meter['rate_per_kwh']
    db.execute('INSERT INTO pump_bill VALUES(?,?,?) ON CONFLICT(meter_id) DO UPDATE SET estimated_kwh=excluded.estimated_kwh,estimated_charge=excluded.estimated_charge',
               (meter_id, apparent_kwh, charge))
    return {'meter_id': meter_id, 'estimated_kwh': apparent_kwh, 'estimated_charge': charge}
