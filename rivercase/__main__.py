import argparse,json
from pathlib import Path
from .store import connect
from .source import read,file_record
from . import station,sample,incident,notice,report

HANDLERS={'station-import':station.import_data,'sample-import':sample.import_data,
          'incident-evaluate':incident.import_data,'notice-publish':notice.import_data}
DEMO=[('station-import','station_valid.json'),('sample-import','sample_valid.json'),
      ('incident-evaluate','incident_valid.json'),('notice-publish','notice_valid.json')]
def main():
    parser=argparse.ArgumentParser(prog='rivercase')
    parser.add_argument('command',choices=[*HANDLERS,'demo','report','audit'])
    parser.add_argument('--db',default='state.sqlite')
    parser.add_argument('--input')
    args=parser.parse_args()
    db=connect(args.db)
    if args.command=='report': print(report.render(db));return
    if args.command=='audit': print(json.dumps(report.audit(db),ensure_ascii=False,indent=2));return
    if args.command=='demo':
        root=Path(__file__).resolve().parents[1]/'fixtures'
        steps=[]
        for cmd,file in DEMO:
            data,raw=read(root/file)
            result=HANDLERS[cmd](db,data)
            file_record(db,cmd,raw)
            steps.append({'command':cmd,'result':result})
        print(json.dumps({'steps':steps,'audit':report.audit(db),'report':report.build(db)},ensure_ascii=False,indent=2));return
    if not args.input: parser.error('--input is required')
    data,raw=read(args.input)
    result=HANDLERS[args.command](db,data)
    file_record(db,args.command,raw)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
