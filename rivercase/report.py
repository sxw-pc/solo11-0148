import hashlib,json
from .store import connect

TABLES=['evidence','station_decision','sample_decision','incident_version','affected_intake',
        'notice_revision','notice_obligation','recipient_receipt','decision_log']
def build(db):
    out={table:[dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1')] for table in TABLES}
    for e in out['evidence']: e['raw']=e['raw'].decode('utf8')
    return out
def audit(db):
    from . import station,sample,incident,notice
    handlers={'station-import':station.import_data,'sample-import':sample.import_data,
              'incident-evaluate':incident.import_data,'notice-publish':notice.import_data}
    entries=db.execute('SELECT * FROM journal ORDER BY id').fetchall()
    if not entries: return {'journal_count':0,'replay_ok':False,'reason':'no CLI inputs recorded'}
    rebuilt=connect(':memory:')
    try:
        for entry in entries:
            if hashlib.sha256(entry['raw']).hexdigest()!=entry['sha']:
                return {'journal_count':len(entries),'replay_ok':False,'reason':'raw source hash mismatch'}
            handlers[entry['command']](rebuilt,json.loads(entry['raw']))
        compared=['station_decision','sample_decision','incident_version','affected_intake','notice_revision','notice_obligation']
        mismatches=[]
        for table in compared:
            left=[tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY 1')]
            right=[tuple(row) for row in rebuilt.execute(f'SELECT * FROM {table} ORDER BY 1')]
            if left!=right: mismatches.append(table)
        return {'journal_count':len(entries),'replay_ok':not mismatches,'mismatches':mismatches}
    finally: rebuilt.close()
def render(db): return json.dumps(build(db),ensure_ascii=False,indent=2)
