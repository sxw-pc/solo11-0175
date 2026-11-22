import argparse,json
from .rights import import_right,revoke_right,import_issuer
from .rotation import import_rotation,close_canal,open_canal
from .gate import import_gate,import_calibration,review_shortage
from .sonar import import_sonar,import_echo,import_pack
from .net_migration import import_video,import_pit,run_net,run_pit_net
from .store import report
from .audit import audit

def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='cmd',required=True)
    funcs={'issuer-import':import_issuer,'right-import':import_right,'right-revoke':revoke_right,'rotation-import':import_rotation,'canal-close':close_canal,'canal-open':open_canal,'calibration-import':import_calibration,'gate-import':import_gate,'shortage-review':review_shortage,'sonar-import':import_sonar,'echo-import':import_echo,'pack-import':import_pack,'video-import':import_video,'pit-import':import_pit}
    for name in funcs:
        x=sub.add_parser(name);x.add_argument('--db',required=True);x.add_argument('--file',required=True)
    for name in ('net-run','pit-run'):
        x=sub.add_parser(name);x.add_argument('--db',required=True);x.add_argument('--canal',required=True);x.add_argument('--start',required=True);x.add_argument('--end',required=True)
    x=sub.add_parser('report');x.add_argument('--db',required=True)
    x=sub.add_parser('audit');x.add_argument('--db',required=True)
    args=p.parse_args()
    if args.cmd in funcs:out=funcs[args.cmd](args.db,args.file)
    elif args.cmd=='net-run':out=run_net(args.db,args.canal,args.start,args.end)
    elif args.cmd=='pit-run':out=run_pit_net(args.db,args.canal,args.start,args.end)
    elif args.cmd=='audit':out=audit(args.db)
    else:out=report(args.db)
    print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
