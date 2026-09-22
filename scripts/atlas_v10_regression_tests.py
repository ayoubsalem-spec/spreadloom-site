#!/usr/bin/env python3
"""BuildIQ Atlas V10 consolidated regression checks.

Runs without Flask/network dependencies by exercising pure helpers and the
shared intelligence layer against an in-memory SQLite fixture.
"""
import ast, sqlite3, sys, types, re
from datetime import date, timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
APP=(ROOT/'app.py').read_text()
TEMPLATE=(ROOT/'templates'/'assistant.html').read_text()


def extract_functions(*names):
    tree=ast.parse(APP)
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
    mod=ast.Module(body=nodes,type_ignores=[])
    ns={'re':re,'date':date,'timedelta':timedelta}
    exec(compile(mod,'app_extract','exec'),ns)
    return ns


def check(cond,msg):
    if not cond: raise AssertionError(msg)
    print('PASS:',msg)

# Natural confirmations and move scheduling are deterministic fast paths.
ns=extract_functions('_atlas_normalize_pending_reply','_atlas_classify_pending_reply','_atlas_parse_move_schedule')
for word in ['yes','yup','yep','yeah','roger that','ten four','sounds good','make it happen']:
    check(ns['_atlas_classify_pending_reply'](word,None)=='CONFIRM',f'natural confirmation {word!r}')
for word in ['cancel that','never mind','forget it']:
    check(ns['_atlas_classify_pending_reply'](word,None)=='CANCEL',f'natural cancellation {word!r}')
base=date(2026,9,22)
check(ns['_atlas_parse_move_schedule']('Move it to Red Bluff tomorrow.',base)[0]=='2026-09-23','equipment move preserves tomorrow')
check(ns['_atlas_parse_move_schedule']('Actually make it Wednesday instead.',base)[0]=='2026-09-23','equipment move resolves weekday')
check(ns['_atlas_parse_move_schedule']('Move it tomorrow at 3:30 PM.',base)==('2026-09-23','15:30'),'equipment move parses date+time')

# Conversation persistence / authoritative confirmation invariants are present in source/template.
check('session["atlas_conversation_id"] = conversation_id' in APP,'conversation id persisted in server session')
check('initial_atlas_messages=initial_messages' in APP and 'initialAtlasMessages' in TEMPLATE,'active transcript restored after navigation')
check('the request could not be verified in BuildIQ after submission' in APP,'concrete success requires authoritative read-back')
check('recent_actions' in APP and '✓ Concrete request #' in APP,'authoritative action receipts retained in conversation state/history')
check('AUTHENTICATED BUILDIQ USER (server-owned session identity)' in APP,'current authenticated user injected into Atlas prompt')
check('Never turn \'this retrieval did not return X\'' in APP,'retrieval-gap overclaim guardrail present')

# Shared intelligence fixtures.
import intelligence
con=sqlite3.connect(':memory:'); con.row_factory=sqlite3.Row
con.executescript('''
CREATE TABLE sitepulse_rentals (id INTEGER PRIMARY KEY,vendor TEXT,equipment_description TEXT,job_name TEXT,rate_amount REAL,rate_period TEXT,rented_date TEXT,due_date TEXT,returned_date TEXT,project_id INTEGER,created_at TEXT,updated_at TEXT);
INSERT INTO sitepulse_rentals VALUES (1,'National Construction Rentals','Portable Toilet','Peninsula',0,'Monthly','2026-06-12','2026-10-12',NULL,1,'x','x');
INSERT INTO sitepulse_rentals VALUES (2,'Gainsborough Waste TX','20 Yd Dumpster','Peninsula',0,'Monthly','2026-08-07','2026-09-04',NULL,1,'x','x');
CREATE TABLE feature_requests (id INTEGER PRIMARY KEY,requester_name TEXT,requester_email TEXT,department TEXT,original_request TEXT,status TEXT,approval_status TEXT,approval_decided_by TEXT,approval_decided_at TEXT,approval_reason TEXT,created_at TEXT,updated_at TEXT);
CREATE TABLE feature_request_intelligence (feature_request_id INTEGER,buildiq_module TEXT,internal_notes TEXT,solution_built TEXT,testing_notes TEXT,user_feedback TEXT,release_date TEXT);
CREATE TABLE feature_request_status_history (id INTEGER PRIMARY KEY,feature_request_id INTEGER,status TEXT,release_note TEXT,changed_by TEXT,changed_at TEXT);
CREATE TABLE feature_request_approvals (id INTEGER PRIMARY KEY,feature_request_id INTEGER,decision TEXT,reason TEXT,decided_by TEXT,decided_at TEXT);
CREATE TABLE roadmap_items (id INTEGER PRIMARY KEY,name TEXT,lane TEXT,note TEXT,progress_pct INTEGER,sort_order INTEGER,updated_at TEXT);
INSERT INTO feature_requests VALUES (24,'Rebecca Abi Antoun','rebecca@darycet.com','Operations','Outside rental editing','Building','Approved','system (predates approval gate)','2026-09-02T11:07:00',NULL,'2026-09-02T11:07:00','2026-09-03T09:07:00');
INSERT INTO feature_request_intelligence VALUES (24,'Equipment Center',NULL,NULL,NULL,NULL,NULL);
INSERT INTO feature_request_status_history VALUES (1,24,'Submitted',NULL,'rebecca@darycet.com','2026-09-02T11:07:00');
INSERT INTO feature_request_status_history VALUES (2,24,'Reviewing',NULL,'ayoub@darycet.com','2026-09-02T14:49:00');
INSERT INTO feature_request_status_history VALUES (3,24,'Approved',NULL,'ayoub@darycet.com','2026-09-03T09:07:00');
INSERT INTO feature_request_status_history VALUES (4,24,'Building',NULL,'ayoub@darycet.com','2026-09-03T09:07:00');
INSERT INTO feature_request_approvals VALUES (1,24,'Approved','predates gate','system (predates approval gate)','2026-09-02T11:07:00');
CREATE TABLE tracker_projects (id INTEGER PRIMARY KEY,name TEXT,client TEXT,address TEXT,status TEXT);
INSERT INTO tracker_projects VALUES (1,'Peninsula','Royal White Cement','16182 Peninsula St Houston TX 77015','Awarded');
CREATE TABLE project_deployments (id INTEGER PRIMARY KEY,project_id INTEGER,status TEXT,start_date TEXT,expected_completion_date TEXT,supervisor_name TEXT,updated_at TEXT);
CREATE TABLE project_deployment_items (id INTEGER PRIMARY KEY,deployment_id INTEGER,status TEXT,applies INTEGER);
CREATE TABLE field_reports (id INTEGER PRIMARY KEY,project_id INTEGER,report_date TEXT,status TEXT,has_issues INTEGER,issues_blockers TEXT,next_steps TEXT,created_by TEXT,last_edited_by TEXT,updated_at TEXT);
CREATE TABLE inventory_concrete_requests (id INTEGER PRIMARY KEY,project_id INTEGER,status TEXT);
CREATE TABLE inventory_purchase_requests (id INTEGER PRIMARY KEY,pr_number TEXT,request_date TEXT,job_name TEXT,project_id INTEGER,needed_on TEXT,status TEXT,vendor_company TEXT);
INSERT INTO inventory_purchase_requests VALUES (1,'09032026','2026-09-03','Peninsula',1,'2026-09-04','Scheduled','Arcosa');
INSERT INTO inventory_purchase_requests VALUES (2,'09032026-2','2026-09-03','Peninsula',1,'2026-09-08','Scheduled','Gainsborough Waste');
INSERT INTO inventory_purchase_requests VALUES (3,'08282026','2026-08-28','Peninsula',1,'2026-08-28','Scheduled','Cherry');
INSERT INTO inventory_purchase_requests VALUES (4,'08282026-2','2026-08-28','Peninsula',1,'2026-08-31','Completed','Brave Equipment');
INSERT INTO inventory_purchase_requests VALUES (5,'08252026','2026-08-25','Peninsula',1,'2026-08-25','Scheduled','Cherry');
INSERT INTO inventory_purchase_requests VALUES (6,'08252026-2','2026-08-25','Peninsula',1,'2026-08-25','Scheduled','ACT Plumbing Supply');
INSERT INTO inventory_purchase_requests VALUES (7,'08192026','2026-08-19','Peninsula',1,'2026-08-20','Scheduled','ACT Plumbing Supply');
CREATE TABLE activity_log (id INTEGER PRIMARY KEY,section TEXT,entity_type TEXT,entity_id INTEGER,action TEXT,field TEXT,old_value TEXT,new_value TEXT,user_email TEXT,created_at TEXT);
CREATE TABLE users (id INTEGER PRIMARY KEY,name TEXT,email TEXT,department TEXT);
CREATE TABLE user_roles (user_id INTEGER,role_id INTEGER);
CREATE TABLE roles (id INTEGER PRIMARY KEY,name TEXT);
CREATE TABLE permissions (id INTEGER PRIMARY KEY,key TEXT,category TEXT,label TEXT);
CREATE TABLE user_permission_overrides (user_id INTEGER,permission_id INTEGER,state TEXT);
''')

rent=intelligence._pi_rentals(con,1,'Peninsula')
check(rent['open_count']==2 and rent['overdue_count']==1,'rental retrieval includes overdue open rental')
dump=next(x for x in rent['items'] if x['equipment_description']=='20 Yd Dumpster')
check(dump['vendor']=='Gainsborough Waste TX' and dump['rented_date']=='2026-08-07','rental retrieval includes vendor/start fields')

fake=types.ModuleType('app')
fake.get_db=lambda:con
fake.user_has_permission=lambda user,key: True
class U: id=1; name='Ayoub Salem'; email='ayoub@darycet.com'
fake.current_user=U()
class Rule:
    def __init__(self,rule): self.rule=rule
class URLMap:
    def iter_rules(self): return [Rule('/cashflow'),Rule('/cashflow/invoices/new'),Rule('/assistant'),Rule('/tracker/'),Rule('/deployment'),Rule('/inventory/concrete'),Rule('/sitepulse/'),Rule('/requests'),Rule('/admin/product-intelligence')]
class App: url_map=URLMap()
fake.current_app=App()
sys.modules['app']=fake

pi=intelligence._tool_get_buildiq_product_intelligence(U(),'requests')
r=pi['requests'][0]
check(r['approval_decided_at']=='2026-09-02T11:07:00' and len(r['status_history'])==4,'request approval/status audit history retrieved')
check(r['status_history'][-1]['changed_by']=='ayoub@darycet.com','request status actor retrieved')

si=intelligence._tool_get_buildiq_system_intelligence(U(),'modules')
cf=next(x for x in si['modules'] if x['name']=='CashFlow')
check(cf['deployed'] is True,'live module registry recognizes CashFlow')
check(si['authenticated_user']['email']=='ayoub@darycet.com','system intelligence exposes server authenticated user')
si2=intelligence._tool_get_buildiq_system_intelligence(U(),'sitepulse')
proj=si2['sitepulse_projects'][0]
check(proj['open_purchase_count']==6 and proj['completed_purchase_count']==1 and proj['total_purchase_count']==7,'purchase counts are deterministic 6 scheduled/open + 1 completed')
check(si2['purchase_status_counts_by_project']['1']['by_status']=={'Scheduled':6,'Completed':1},'purchase status breakdown by project is exact')


# Authoritative confirm_write test: success is returned only after the inserted
# concrete row can be read back from the DB.
tree=ast.parse(APP)
node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='assistant_confirm_write')
node.decorator_list=[]
confirm_ns={}
import threading, time as _time
class Req:
    def get_json(self,silent=True): return {'token':'tok'}
class User: id=99; name='Ayoub Salem'; email='ayoub@darycet.com'
confirm_db=sqlite3.connect(':memory:'); confirm_db.row_factory=sqlite3.Row
confirm_db.execute('CREATE TABLE inventory_concrete_requests (id INTEGER PRIMARY KEY, project TEXT, area_description TEXT, pour_date TEXT, pour_time TEXT, status TEXT)')
confirm_db.commit()
class Result:
    def __init__(self,success,data=None,error=None): self.success=success; self.data=data or {}; self.error=error

def fake_execute(tool, params, user, confirmed=False, session_context=None):
    cur=confirm_db.execute('INSERT INTO inventory_concrete_requests(project,area_description,pour_date,pour_time,status) VALUES (?,?,?,?,?)',
                           (params['project'],params.get('area_description'),params['pour_date'],params.get('pour_time'),'Submitted'))
    confirm_db.commit(); return Result(True,{'id':cur.lastrowid,'submitted_id':cur.lastrowid})

def remember(draft,val,aliases=None): return {'canonical_value':val,'label':val}
confirm_draft={'pending_write':{'token':'tok','tool_name':'create_concrete_request','params':{'project':'Peninsula','area_description':'South slab','pour_date':'2026-09-23','pour_time':'07:00'},'issued_at':_time.time(),'action_context':{}},'pending_submit':{},'project_context':{},'active_context':{},'history':[],'conversation_id':None}
confirm_ns.update({'app':types.SimpleNamespace(route=lambda *a,**k:(lambda f:f)),'login_required':lambda f:f,'is_atlas_allowed':lambda:True,
                   'session':{'atlas_token':'s'},'request':Req(),'ATLAS_SESSIONS':{'s':confirm_draft},'ATLAS_WRITE_CONFIRM_LOCK':threading.Lock(),
                   'PENDING_WRITE_TTL_SECONDS':120,'time':_time,'execute_tool':fake_execute,'current_user':User(),'get_db':lambda:confirm_db,
                   'log_activity':lambda *a,**k:None,'_atlas_remember_location':remember,'_append_atlas_message_owned':lambda *a,**k:None,
                   'datetime':__import__('datetime').datetime,'json':__import__('json')})
exec(compile(ast.Module(body=[node],type_ignores=[]),'confirm_extract','exec'),confirm_ns)
confirm_result=confirm_ns['assistant_confirm_write']()
check(confirm_result['success'] is True and confirm_result['submitted_id']==1,'confirm_write verifies real concrete row before success')
check(confirm_draft['active_context']['last_action']=='create_concrete_request','verified concrete write stored as completed action context')

# False-success prevention: a tool claiming success without an authoritative row must fail.
confirm_db.execute('DELETE FROM inventory_concrete_requests'); confirm_db.commit()
def fake_execute_no_write(tool, params, user, confirmed=False, session_context=None): return Result(True,{'id':77,'submitted_id':77})
confirm_ns['execute_tool']=fake_execute_no_write
exec(compile(ast.Module(body=[node],type_ignores=[]),'confirm_extract2','exec'),confirm_ns)
confirm_ns['ATLAS_SESSIONS']['s']={'pending_write':{'token':'tok','tool_name':'create_concrete_request','params':{'project':'Peninsula','area_description':'South slab','pour_date':'2026-09-23','pour_time':'07:00'},'issued_at':_time.time(),'action_context':{}},'pending_submit':{},'project_context':{},'active_context':{},'history':[],'conversation_id':None}
fail_result=confirm_ns['assistant_confirm_write']()
check(fail_result['success'] is False and 'could not be verified' in fail_result['error'],'false concrete success receipt is blocked when row is absent')

print('\nALL V10 CONSOLIDATED REGRESSION CHECKS PASSED')
