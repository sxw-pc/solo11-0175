import argparse,json
from .rights import import_right,revoke_right,import_issuer
from .rotation import import_rotation,close_canal,open_canal
from .gate import import_gate,import_calibration,review_shortage
from .stage import import_gauge,import_stage
from .overtop import import_site,import_outage,import_buffer,import_mark,import_caldev
from .store import report
from .audit import audit

def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='cmd',required=True)
    funcs={'issuer-import':import_issuer,'right-import':import_right,'right-revoke':revoke_right,'rotation-import':import_rotation,'canal-close':close_canal,'canal-open':open_canal,'calibration-import':import_calibration,'gate-import':import_gate,'shortage-review':review_shortage,'gauge-import':import_gauge,'stage-import':import_stage,'site-import':import_site,'outage-import':import_outage,'buffer-import':import_buffer,'mark-import':import_mark,'caldev-import':import_caldev}
    for name in funcs:
        x=sub.add_parser(name);x.add_argument('--db',required=True);x.add_argument('--file',required=True)
    x=sub.add_parser('report');x.add_argument('--db',required=True)
    x=sub.add_parser('audit');x.add_argument('--db',required=True)
    args=p.parse_args();print(json.dumps(funcs[args.cmd](args.db,args.file) if args.cmd in funcs else (audit(args.db) if args.cmd=='audit' else report(args.db)),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
