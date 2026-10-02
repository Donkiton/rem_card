from __future__ import annotations
import sqlite3
import sys
import json
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path: sys.path.insert(0,str(ROOT.parent))
from rem_card.app.unified_db_schema import ensure_unified_schema
from rem_card.app.operblock_schema import _apply_operblock_schema
from rem_card.services.operblock_service import OperBlockService
from rem_card.services.concurrency import DataConflictError

class Db:
    db_path=""
    def __init__(self):
        self.conn=sqlite3.connect(":memory:"); self.conn.row_factory=sqlite3.Row
        ensure_unified_schema(self.conn); _apply_operblock_schema(self.conn.cursor()); self.conn.commit()
    def run_write_operation(self, fn, source=""):
        try: r=fn(self.conn.cursor()); self.conn.commit(); return r
        except Exception: self.conn.rollback(); raise
    def fetch_all_remcard(self,q,p=()): return self.conn.execute(q,p).fetchall()

def seed(db, status="closed"):
    c=db.conn.cursor(); c.execute("INSERT INTO patients(full_name,birth_date) VALUES('Иванов И.И.','1980-01-01')"); p=c.lastrowid
    c.execute("INSERT INTO admissions(patient_id,bed_number,history_number,admission_datetime,diagnosis_text,unit_scope,is_active) VALUES(?,1,'1','2026-09-28T08:00:00','Диагноз','operblock',0)",(p,)); a=c.lastrowid
    c.execute("INSERT INTO operation_cases(patient_id,admission_id,table_code,status,started_at,ended_at,planned_operation_name,revision) VALUES(?,?, 'emergency', ?, '2026-09-28T08:00:00','2026-09-28T09:00:00','До',0)",(p,a,status)); db.conn.commit(); return c.lastrowid

def test_closed_archive_edit_keeps_case_closed_and_records_history():
    db=Db(); case=seed(db); svc=OperBlockService(db)
    result=svc.update_archived_operation_case_form_data(case,{"planned_operation_name":"После","diagnosis_text":"Уточнён"},expected_operation_case_revision=0,expected_admission_revision=0)
    row=db.conn.execute("SELECT status,planned_operation_name,revision FROM operation_cases WHERE id=?",(case,)).fetchone()
    assert result["revision"]==1 and tuple(row)==("closed","После",1)
    assert db.conn.execute("SELECT COUNT(*) FROM operation_table_assignments WHERE operation_case_id=?",(case,)).fetchone()[0]==0
    history=svc.list_archived_operation_case_edit_history(case)
    assert len(history)==1
    assert {"case","patient","admission"}.issubset(json.loads(history[0]["after_json"]))

def test_archive_edit_uses_optimistic_revision_and_rejects_active_case():
    db=Db(); case=seed(db); svc=OperBlockService(db)
    svc.update_archived_operation_case_form_data(case,{"planned_operation_name":"После"},expected_operation_case_revision=0,expected_admission_revision=0)
    with pytest.raises(DataConflictError): svc.update_archived_operation_case_form_data(case,{"planned_operation_name":"Гонка"},expected_operation_case_revision=0,expected_admission_revision=1)
    active=seed(db,"active")
    with pytest.raises(Exception, match="закрытый"):
        svc.update_archived_operation_case_form_data(active,{"planned_operation_name":"X"},expected_operation_case_revision=0,expected_admission_revision=0)
