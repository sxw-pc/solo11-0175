import tempfile
from pathlib import Path
from contextlib import closing
from .store import connect,report
from .rights import import_right,revoke_right,import_issuer
from .rotation import import_rotation,close_canal,open_canal
from .gate import import_gate,import_calibration,review_shortage

HANDLERS={'issuer':import_issuer,'right':import_right,'right_revoke':revoke_right,'rotation':import_rotation,'canal_close':close_canal,'canal_open':open_canal,'calibration':import_calibration,'gate':import_gate,'liability_review':review_shortage}

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
    keys=('issuers','rights','rotations','maintenance','calibrations','deliveries','compensation','reviews')
    return {'status':'matched' if all(expected[k]==actual[k] for k in keys) else 'divergent',
            'source_count':len(evidence),'differences':[k for k in keys if expected[k]!=actual[k]]}
