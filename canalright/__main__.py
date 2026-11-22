import argparse,json
from .rights import import_right,revoke_right,import_issuer
from .rotation import import_rotation,close_canal,open_canal
from .gate import import_gate,import_calibration,review_shortage
from .tilt import import_sensor,import_sensor_zero,import_tilt
from . import slip_proof
from .store import report
from .audit import audit

def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='cmd',required=True)
    funcs={'issuer-import':import_issuer,'right-import':import_right,'right-revoke':revoke_right,'rotation-import':import_rotation,'canal-close':close_canal,'canal-open':open_canal,'calibration-import':import_calibration,'gate-import':import_gate,'shortage-review':review_shortage,
    'bank-sensor':import_sensor,'bank-zero':import_sensor_zero,'bank-tilt':import_tilt,
    'bank-batch':slip_proof.import_thermal_batch,'bank-bracket':slip_proof.import_bracket,
    'bank-temperature':slip_proof.import_temperature,'bank-benchmark':slip_proof.import_benchmark,
    'bank-total-station':slip_proof.import_total_station,'bank-oven':slip_proof.import_oven_calibration,
    'bank-photo':slip_proof.import_photo,'bank-flag':slip_proof.import_field_flag,
    'bank-review':slip_proof.review_segment,'bank-certificate':slip_proof.issue_repair_certificate}
    for name in funcs:
        x=sub.add_parser(name);x.add_argument('--db',required=True);x.add_argument('--file',required=True)
    x=sub.add_parser('report');x.add_argument('--db',required=True)
    x=sub.add_parser('audit');x.add_argument('--db',required=True)
    args=p.parse_args();print(json.dumps(funcs[args.cmd](args.db,args.file) if args.cmd in funcs else (audit(args.db) if args.cmd=='audit' else report(args.db)),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
