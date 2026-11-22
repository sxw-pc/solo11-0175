import tempfile
from pathlib import Path
from contextlib import closing
from .store import connect,report
from .rights import import_right,revoke_right,import_issuer
from .rotation import import_rotation,close_canal,open_canal
from .gate import import_gate,import_calibration,review_shortage
from .tilt import import_sensor,import_sensor_zero,import_tilt
from . import slip_proof

HANDLERS={'issuer':import_issuer,'right':import_right,'right_revoke':revoke_right,'rotation':import_rotation,'canal_close':close_canal,'canal_open':open_canal,'calibration':import_calibration,'gate':import_gate,'liability_review':review_shortage,
'bank_sensor':import_sensor,'bank_sensor_zero':import_sensor_zero,'bank_tilt':import_tilt,
'bank_thermal_batch':slip_proof.import_thermal_batch,'bank_bracket':slip_proof.import_bracket,
'bank_temperature':slip_proof.import_temperature,'bank_benchmark':slip_proof.import_benchmark,
'bank_total_station':slip_proof.import_total_station,'bank_oven_calibration':slip_proof.import_oven_calibration,
'bank_photo':slip_proof.import_photo,'bank_field_flag':slip_proof.import_field_flag,
'bank_slip_review':slip_proof.review_segment,'bank_repair_certificate':slip_proof.issue_repair_certificate}

def audit(path):
    with closing(connect(path)) as db:
        evidence=[dict(row) for row in db.execute('SELECT kind,raw FROM evidence ORDER BY seq')]
    expected=report(path)
    with tempfile.TemporaryDirectory(prefix='canalright-audit-') as root:
        target=Path(root)/'replay.sqlite'
        for i,row in enumerate(evidence):
            file=Path(root)/f'{i}.json';file.write_bytes(row['raw'])
            HANDLERS[row['kind']](target,file)
        actual=report(target)
    keys=('issuers','rights','rotations','maintenance','calibrations','deliveries','compensation','reviews',
    'bank_sensors','bank_zeros','bank_tilts','bank_advice','bank_batches','bank_brackets','bank_temperatures',
    'bank_benchmarks','bank_total_station','bank_calibrations','bank_photos','bank_field_flags',
    'bank_slip_reviews','bank_repair_certificates')
    return {'status':'matched' if all(expected[k]==actual[k] for k in keys) else 'divergent',
            'source_count':len(evidence),'differences':[k for k in keys if expected[k]!=actual[k]]}
