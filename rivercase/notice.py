import json
from .source import record
from .store import log

def current_case(db,case_id):
    return db.execute('SELECT * FROM incident_version WHERE case_id=? ORDER BY version DESC LIMIT 1',(case_id,)).fetchone()

def _set_status(db,notice_id,status,reason):
    old=db.execute('SELECT status,reason FROM notice_revision WHERE id=?',(notice_id,)).fetchone()
    if old and (old['status'],old['reason'])!=(status,reason):
        db.execute('UPDATE notice_revision SET status=?,reason=? WHERE id=?',(status,reason,notice_id))
        log(db,'notice',notice_id,status,reason)

def reconcile_case(db,case_id):
    current=current_case(db,case_id)
    if not current: return
    for n in db.execute('SELECT * FROM notice_revision WHERE case_id=?',(case_id,)).fetchall():
        if n['status'] in ('blocked','withdrawn'): continue
        if n['incident_version']==current['version']: continue
        for recipient in json.loads(n['recipients_json']):
            db.execute("INSERT OR IGNORE INTO notice_obligation(notice_id,intake_id,kind,status) VALUES(?,?,'withdraw','pending')",(n['id'],recipient))
        _set_status(db,n['id'],'withdraw_pending','incident evidence version changed; prior recipients need withdrawal')

def _refresh(db,notice_id):
    n=db.execute('SELECT * FROM notice_revision WHERE id=?',(notice_id,)).fetchone()
    if not n: return None
    if n['status']=='blocked': return dict(n)
    if n['status']=='withdraw_pending':
        pending=db.execute("SELECT count(*) FROM notice_obligation WHERE notice_id=? AND kind='withdraw' AND status='pending'",(notice_id,)).fetchone()[0]
        if pending==0: _set_status(db,notice_id,'withdrawn','all original recipients acknowledged withdrawal')
    else:
        pending=db.execute("SELECT count(*) FROM notice_obligation WHERE notice_id=? AND kind='deliver' AND status='pending'",(notice_id,)).fetchone()[0]
        _set_status(db,notice_id,'delivered' if pending==0 else 'partial','all recipients acknowledged' if pending==0 else f'{pending} recipients missing receipt')
    return dict(db.execute('SELECT * FROM notice_revision WHERE id=?',(notice_id,)).fetchone())

def import_data(db,data):
    out=[]
    with db:
        for n in data.get('notices',[]):
            if not record(db,n['event_id'],'notice',n):
                out.append(_refresh(db,n['id']));continue
            current=current_case(db,n['case_id'])
            wanted=set(json.loads(current['affected_json'])) if current and current['status']=='confirmed' else set()
            recipients=set(n['recipients'])
            if not current or current['status']!='confirmed':
                status,reason='blocked','incident not confirmed'
            elif n['incident_version']!=current['version'] or n['level']!=current['level']:
                status,reason='blocked','incident version or level mismatch'
            elif not wanted.issubset(recipients):
                status,reason='blocked','affected intake missing from recipients'
            else:
                status,reason='published','recipient delivery obligations opened'
            db.execute('INSERT INTO notice_revision(id,case_id,incident_version,level,recipients_json,status,reason) VALUES(?,?,?,?,?,?,?)',
                       (n['id'],n['case_id'],n['incident_version'],n['level'],json.dumps(sorted(recipients)),status,reason))
            log(db,'notice',n['id'],status,reason,n['event_id'])
            if status=='published':
                for recipient in sorted(recipients):
                    db.execute("INSERT INTO notice_obligation(notice_id,intake_id,kind,status) VALUES(?,?,'deliver','pending')",(n['id'],recipient))
            out.append(dict(db.execute('SELECT * FROM notice_revision WHERE id=?',(n['id'],)).fetchone()))
        for receipt in data.get('receipts',[]):
            if not record(db,receipt['event_id'],'receipt',receipt): continue
            obligation=db.execute('SELECT status FROM notice_obligation WHERE notice_id=? AND intake_id=? AND kind=?',
                                  (receipt['notice_id'],receipt['intake_id'],receipt['kind'])).fetchone()
            if not obligation: raise ValueError('receipt without matching recipient obligation')
            db.execute('INSERT INTO recipient_receipt(event_id,notice_id,intake_id,kind,at) VALUES(?,?,?,?,?)',
                       (receipt['event_id'],receipt['notice_id'],receipt['intake_id'],receipt['kind'],receipt['at']))
            db.execute('UPDATE notice_obligation SET status=? WHERE notice_id=? AND intake_id=? AND kind=?',
                       ('done',receipt['notice_id'],receipt['intake_id'],receipt['kind']))
        ids={n['id'] for n in data.get('notices',[])} | {r['notice_id'] for r in data.get('receipts',[])}
        out=[_refresh(db,i) for i in sorted(ids)]
    return out
