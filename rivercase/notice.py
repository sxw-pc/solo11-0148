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

# ---------------------------------------------------------------- public notices

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

# ------------------------------------------------- intake duty-operations contract

def _refresh_task(db,task_id):
    t=db.execute('SELECT * FROM duty_task WHERE id=?',(task_id,)).fetchone()
    if not t: return None
    if t['status']=='release_pending':
        pending=db.execute("SELECT count(*) FROM duty_obligation WHERE task_id=? AND kind='release' AND status='pending'",(task_id,)).fetchone()[0]
        if pending==0:
            db.execute("UPDATE duty_task SET status='released',reason='duty officer acknowledged release; precautionary task closed' WHERE id=?",(task_id,))
            log(db,'duty_task',task_id,'released','release receipts complete')
    elif t['status'] in ('open','active'):
        pending=db.execute("SELECT count(*) FROM duty_obligation WHERE task_id=? AND kind='deliver' AND status='pending'",(task_id,)).fetchone()[0]
        if pending==0 and t['status']=='open':
            db.execute("UPDATE duty_task SET status='active' WHERE id=?",(task_id,))
            log(db,'duty_task',task_id,'active','duty officer received task')
    return dict(db.execute('SELECT * FROM duty_task WHERE id=?',(task_id,)).fetchone())

def reconcile_duty(db,case_id):
    """Open/release precautionary duty tasks from the current incident version.

    A task is opened while at least one jointly feasible explanation can
    carry the plume to the intake before its reserved shutdown time; the
    public notice contract is deliberately stricter and handled elsewhere.
    Duty receipts never settle notice obligations.
    """
    current=current_case(db,case_id)
    if not current: return
    version=current['version']
    definite=set(json.loads(current['affected_json']))
    possible=set(json.loads(current['possible_json']))
    windows=json.loads(current['windows_json'])
    eligible={}
    for intake in db.execute('SELECT * FROM intake ORDER BY id').fetchall():
        iid=intake['id']
        if iid not in definite and iid not in possible: continue
        if not intake['shutdown_at']: continue
        win=windows.get(iid)
        if win and win['start'] < intake['shutdown_at']:
            eligible[iid]=win
    open_tasks={t['intake_id']:t for t in db.execute(
        "SELECT * FROM duty_task WHERE case_id=? AND status IN ('open','active','release_pending')",(case_id,)).fetchall()}
    for iid,win in eligible.items():
        if iid in open_tasks: continue
        task_id=f'D-{case_id}-v{version}-{iid}'
        db.execute('INSERT INTO duty_task(id,case_id,incident_version,intake_id,window_start,window_end,unverified_gates_json,status,reason) VALUES(?,?,?,?,?,?,?,?,?)',
                   (task_id,case_id,version,iid,win['start'],win['end'],
                    json.dumps(win['unverified_gates']),'open',
                    'credible reachable explanation before reserved shutdown: temporary stop and resampling required'))
        db.execute("INSERT INTO duty_obligation(task_id,intake_id,kind,status) VALUES(?,?,'deliver','pending')",(task_id,iid))
        log(db,'duty_task',task_id,'open','earliest possible arrival precedes shutdown time; unverified gates noted')
    for iid,task in open_tasks.items():
        if iid in eligible: continue
        if task['status']=='release_pending': continue
        db.execute("UPDATE duty_task SET status='release_pending',reason=? WHERE id=?",
                   (f'new incident version {version}: intake no longer credibly reachable before shutdown; release the precautionary task',task['id']))
        db.execute("INSERT OR IGNORE INTO duty_obligation(task_id,intake_id,kind,status) VALUES(?,?,'release','pending')",
                   (task['id'],iid))
        log(db,'duty_task',task['id'],'release_pending','feasible explanations no longer reach intake before shutdown')
        _refresh_task(db,task['id'])
    for t in db.execute("SELECT id FROM duty_task WHERE case_id=?",(case_id,)).fetchall():
        _refresh_task(db,t['id'])

def _import_duty_receipts(db,receipts):
    for receipt in receipts:
        if not record(db,receipt['event_id'],'duty_receipt',receipt): continue
        obligation=db.execute('SELECT status FROM duty_obligation WHERE task_id=? AND intake_id=? AND kind=?',
                              (receipt['task_id'],receipt['intake_id'],receipt['kind'])).fetchone()
        if not obligation: raise ValueError('duty receipt without matching task obligation')
        db.execute('INSERT INTO duty_receipt(event_id,task_id,intake_id,kind,at) VALUES(?,?,?,?,?)',
                   (receipt['event_id'],receipt['task_id'],receipt['intake_id'],receipt['kind'],receipt['at']))
        db.execute('UPDATE duty_obligation SET status=? WHERE task_id=? AND intake_id=? AND kind=?',
                   ('done',receipt['task_id'],receipt['intake_id'],receipt['kind']))
        _refresh_task(db,receipt['task_id'])

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
            elif not recipients.issubset(wanted):
                status,reason='blocked','possible or unreachable intake cannot receive a confirmed public notice'
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
        _import_duty_receipts(db,data.get('duty_receipts',[]))
        ids={n['id'] for n in data.get('notices',[])} | {r['notice_id'] for r in data.get('receipts',[])}
        out=[_refresh(db,i) for i in sorted(ids)]
        out += [_refresh_task(db,row['id']) for row in db.execute(
            'SELECT id FROM duty_task ORDER BY id').fetchall()]
        out=[x for x in out if x]
    return out
