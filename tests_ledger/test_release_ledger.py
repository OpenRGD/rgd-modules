"""Real SQLite transaction/recovery tests + pinned TUF and built wheel.

Local fixture authorizer is explicitly test-only; it is not a trust verifier.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from ledger_contract import digest, encode_proposal, parse
from trust_fixture import build_fixture, proposal_for
from wheel_inspection import inspect_wheel, review_target
from test_trusted_review import CatalogFixture, NAMESPACE
from tools.release_ledger import CandidateLedger, LedgerError
from tools.tuf_ledger_authority import tuf_reservation_authorizer
from rgd_registry import core as legacy

CAT="org.example.catalog"

def fixture_policy(request_raw,proposal_raw,evidence_raw,prior_raw):
    return encode_proposal({"profile":"TEST_ONLY_LOCAL_OWNER_POLICY",
                            "request_sha256":digest(request_raw)})

def reserve_request(lid,p,e,request_id=None,catalog=CAT):
    info=parse(p)
    return {"schema_version":"openrgd.release-ledger-request.v0.1-candidate",
        "catalog_id":catalog,"ledger_id":lid,"request_id":request_id or str(uuid4()),
        "module_id":info["module_id"],"module_version":info["module_version"],
        "operation":"RESERVE_CANDIDATE","expected_revision":0,"proposal_sha256":digest(p),
        "reason_code":"CANDIDATE_REVIEW","evidence_sha256":digest(e),"grants_authority":False}

def control(original,op,revision):
    req={**original,"request_id":str(uuid4()),"operation":op,"expected_revision":revision,
         "reason_code":"SECURITY" if op=="REVOKE" else "POLICY"}
    notice={k:v for k,v in req.items() if k!="evidence_sha256"}
    notice["schema_version"]="openrgd.release-disposition-notice.v0.1-candidate"
    evidence=encode_proposal(notice)
    req["evidence_sha256"]=digest(evidence)
    return req,evidence

CHILD = """
import json, os, sys, time
from pathlib import Path
from contextlib import contextmanager
from tools.release_ledger import CandidateLedger, LedgerError
from ledger_contract import encode_proposal
data=json.loads(Path(sys.argv[1]).read_text())
def allow(*args):return b'test-only child policy'
class CrashLedger(CandidateLedger):
    @contextmanager
    def _transaction(self,write):
        with super()._transaction(write) as db:
            yield db
            if write:os._exit(73)
ledger_type=CrashLedger if data['crash'] else CandidateLedger
ledger=ledger_type(data['db'],data['catalog'],data['lid'],authorizer=allow)
if data.get('ready'):
    Path(data['ready']).touch()
    deadline=time.monotonic()+20
    while not Path(data['start']).exists():
        if time.monotonic()>deadline:raise RuntimeError('barrier timeout')
        time.sleep(.01)
try:
    result=ledger.apply(data['request'].encode(),data['proposal'].encode(),data['evidence'].encode())
    print(json.dumps({'receipt':result}))
except LedgerError as e:
    print(json.dumps({'error':str(e)}))
"""

class LedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build=tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.build.cleanup)
        cls.inputs=build_fixture(cls.build.name)
        cls.p=proposal_for(cls.inputs)
        report=inspect_wheel(**cls.inputs,submitted_proposal_raw=cls.p)
        cls.e=encode_proposal(review_target(report,NAMESPACE))
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir=Path(self.tmp.name)
        self.path=self.dir/"ledger.sqlite"
        self.lid=str(uuid4())
        self.r=reserve_request(self.lid,self.p,self.e)
        self.raw=encode_proposal(self.r)
        self.ledger=CandidateLedger.initialize(self.path,CAT,self.lid,authorizer=fixture_policy)
        self.key=self.r["module_id"],self.r["module_version"]
    def reopen(self,authorizer=fixture_policy,checkpoint=None):
        return CandidateLedger(self.path,CAT,self.lid,authorizer=authorizer,minimum_checkpoint=checkpoint)
    def count(self):
        return parse(self.ledger.checkpoint())["event_sequence"]
    def commit(self):
        return self.ledger.apply(self.raw,self.p,self.e)
    def control(self,op,rev):
        req,e=control(self.r,op,rev)
        return self.ledger.apply(encode_proposal(req),self.p,e)
    def changed(self):
        inputs=deepcopy(self.inputs)
        source=parse(inputs["source_claim"]);source["git_commit"]="b"*40
        inputs["source_claim"]=encode_proposal(source)
        p=proposal_for(inputs)
        e=encode_proposal(review_target(inspect_wheel(**inputs,submitted_proposal_raw=p),NAMESPACE))
        return reserve_request(self.lid,p,e),p,e
    def test_explicit_init_and_durable_reopen(self):
        first=self.commit()
        other=self.reopen(authorizer=None)
        self.assertEqual(first,other.lookup_receipt(self.r["request_id"]))
        self.assertEqual(first,other.current(*self.key))
        self.assertEqual(first,other.apply(self.raw,self.p,self.e))
        self.assertEqual(self.count(),1)
        for k in ("publication_committed","installable","grants_authority"):
            self.assertIs(first[k],False)
    def test_no_silent_bootstrap_or_reinit(self):
        missing=self.dir/"missing.sqlite"
        with self.assertRaisesRegex(LedgerError,"LEDGER_FILE_REQUIRED"):
            CandidateLedger(missing,CAT,self.lid)
        self.assertFalse(missing.exists())
        with self.assertRaisesRegex(LedgerError,"LEDGER_ALREADY_EXISTS"):
            CandidateLedger.initialize(self.path,CAT,self.lid)
        with self.assertRaisesRegex(LedgerError,"LEDGER_SCOPE_MISMATCH"):
            CandidateLedger(self.path,CAT,str(uuid4()))
    def test_retry_after_revoke_is_historical_only(self):
        first=self.commit()
        self.control("WITHDRAW",1)
        revoked=self.control("REVOKE",2)
        self.assertEqual(revoked["release_revision"],3)
        self.assertEqual(self.reopen(None).apply(self.raw,self.p,self.e),first)
        self.assertEqual(self.reopen(None).current(*self.key)["state_after"],"REVOKED")
        self.assertEqual(self.count(),3)
    def test_same_id_different_exact_request_conflict(self):
        self.commit()
        with self.assertRaisesRegex(LedgerError,"IDEMPOTENCY_CONFLICT"):
            self.ledger.apply(self.raw+b" ",self.p,self.e)
        self.assertEqual(self.count(),1)
    def test_new_request_same_release_refused(self):
        self.commit()
        req={**self.r,"request_id":str(uuid4())}
        with self.assertRaisesRegex(LedgerError,"RELEASE_ALREADY_RESERVED"):
            self.ledger.apply(encode_proposal(req),self.p,self.e)
    def test_new_content_even_after_withdraw_refused(self):
        self.commit();self.control("WITHDRAW",1)
        req,p,e=self.changed()
        with self.assertRaisesRegex(LedgerError,"RELEASE_KEY_CONTENT_CONFLICT"):
            self.ledger.apply(encode_proposal(req),p,e)
        self.assertEqual(self.count(),2)
    def test_revoked_terminal_and_stale_controls(self):
        self.commit()
        self.control("REVOKE",1)
        for op,rev,code in (("WITHDRAW",1,"STALE_RELEASE_REVISION"),("WITHDRAW",2,"TERMINAL_RELEASE_STATE"),
                            ("REVOKE",2,"TERMINAL_RELEASE_STATE")):
            with self.subTest(op=op,rev=rev),self.assertRaisesRegex(LedgerError,code):
                self.control(op,rev)
    def test_unknown_control_rejected(self):
        with self.assertRaisesRegex(LedgerError,"RELEASE_UNKNOWN"):
            self.control("REVOKE",1)
    def test_no_authority_no_write_then_retry(self):
        denied=self.reopen(None)
        with self.assertRaisesRegex(LedgerError,"OWNER_AUTHORITY_REQUIRED"):
            denied.apply(self.raw,self.p,self.e)
        self.assertIsNone(self.ledger.lookup_receipt(self.r["request_id"]))
        self.assertEqual(self.count(),0)
        self.commit()
    def test_authorizer_failure_and_retry_no_partial_state(self):
        def deny(*args):raise LedgerError("FIXTURE_DENIED")
        other=self.reopen(deny)
        with self.assertRaisesRegex(LedgerError,"FIXTURE_DENIED"):
            other.apply(self.raw,self.p,self.e)
        self.assertEqual(self.count(),0)
        first=self.commit()
        self.assertEqual(other.apply(self.raw,self.p,self.e),first)
    def test_legacy_release_key_conflict(self):
        p=parse(self.p);p.update(module_id="org.openrgd.calibration",module_version="0.2.0")
        p=encode_proposal(p)
        e=parse(self.e);e.update(module_id="org.openrgd.calibration",module_version="0.2.0",
                                publisher_namespace="org.openrgd",proposal_sha256=digest(p))
        e=encode_proposal(e)
        req=reserve_request(self.lid,p,e)
        with self.assertRaisesRegex(LedgerError,"LEGACY_RELEASE_KEY_CONFLICT"):
            self.ledger.apply(encode_proposal(req),p,e)
        self.assertEqual(self.count(),0)
    def test_legacy_snapshot_drift_blocks_open_and_existing_connection(self):
        rows=deepcopy(legacy.catalog());rows[0]["summary"]+=" changed"
        with patch.object(legacy,"catalog",return_value=rows):
            with self.assertRaisesRegex(LedgerError,"LEGACY_CATALOG_DRIFT"):
                self.reopen()
            with self.assertRaisesRegex(LedgerError,"LEGACY_CATALOG_DRIFT"):
                self.commit()
        self.assertEqual(self.count(),0)
    def test_receipt_chain_and_checkpoint_prefix(self):
        first=self.commit();check=self.ledger.checkpoint()
        second=self.control("WITHDRAW",1)
        self.assertEqual(second["previous_event_sha256"],digest(encode_proposal(first)))
        self.reopen(checkpoint=check).verify_checkpoint(check)
        bad={**parse(check),"event_sha256":"sha256:"+"f"*64}
        with self.assertRaisesRegex(LedgerError,"CHECKPOINT_PREFIX_CONFLICT"):
            self.reopen(checkpoint=encode_proposal(bad))
    def test_restored_old_database_detected_by_retained_checkpoint(self):
        self.commit()
        backup=self.dir/"old.sqlite";shutil.copy2(self.path,backup)
        self.control("REVOKE",1)
        check=self.ledger.checkpoint()
        # Local internally consistent backup, but older than external checkpoint.
        with self.assertRaisesRegex(LedgerError,"CHECKPOINT_ROLLBACK"):
            CandidateLedger(backup,CAT,self.lid,minimum_checkpoint=check)
    def test_append_only_triggers_block_update_delete_replace(self):
        self.commit()
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA recursive_triggers=ON")
            for sql in ("UPDATE release_events SET revision=9","DELETE FROM release_events",
                        "INSERT OR REPLACE INTO release_events SELECT * FROM release_events",
                        "UPDATE ledger_meta SET ledger_id='wrong'"):
                with self.subTest(sql=sql),self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)
        self.assertEqual(self.count(),1)
    def test_tampered_receipt_detected_even_with_trigger_recreated(self):
        self.commit()
        # Simulate privileged corruption; this is not a hostile-admin defense.
        with sqlite3.connect(self.path) as db:
            ddl=db.execute("SELECT sql FROM sqlite_master WHERE name='events_no_update'").fetchone()[0]
            db.execute("DROP TRIGGER events_no_update")
            db.execute("UPDATE release_events SET receipt_raw=?",(b"{}",))
            db.execute(ddl)
        with self.assertRaisesRegex(LedgerError,"LEDGER_RECEIPT_MISMATCH"):
            self.reopen()
    def test_capacity_is_fail_closed_not_eviction(self):
        self.commit()
        with patch("tools.release_ledger.MAX_EVENTS",1):
            with self.assertRaisesRegex(LedgerError,"LEDGER_CAPACITY_REACHED"):
                self.control("REVOKE",1)
            self.assertEqual(self.count(),1)
            self.assertEqual(self.ledger.apply(self.raw,self.p,self.e)["release_revision"],1)
    def test_two_independent_threads_exact_retry(self):
        barrier=threading.Barrier(2,timeout=10)
        def worker():
            ledger=self.reopen()
            barrier.wait()
            return ledger.apply(self.raw,self.p,self.e)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(worker) for _ in range(2)]
            a,b=[f.result(timeout=20) for f in futures]
        self.assertEqual(a,b)
        self.assertEqual(self.count(),1)
    def spawn(self,label,req,p,e,*,crash=False,start=None):
        path=self.dir/(label+".json")
        ready=str(self.dir/(label+".ready")) if start is not None else None
        data={"db":str(self.path),"catalog":CAT,"lid":self.lid,"request":encode_proposal(req).decode(),
              "proposal":p.decode(),"evidence":e.decode(),"crash":crash,"ready":ready,"start":start}
        path.write_text(json.dumps(data),encoding="utf-8")
        child=subprocess.Popen([sys.executable,"-c",CHILD,str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def cleanup():
            if child.poll() is None:child.kill()
            child.communicate()
        self.addCleanup(cleanup)
        return child,ready
    def test_two_independent_processes_conflicting_content(self):
        req,p,e=self.changed()
        start=str(self.dir/"start")
        a,ar=self.spawn("a",self.r,self.p,self.e,start=start)
        b,br=self.spawn("b",req,p,e,start=start)
        deadline=time.monotonic()+20
        while not (Path(ar).exists() and Path(br).exists()):
            if time.monotonic()>deadline:self.fail("child barrier timeout")
            time.sleep(.02)
        Path(start).touch()
        results=[]
        for child in (a,b):
            out,err=child.communicate(timeout=20)
            self.assertEqual(child.returncode,0,err)
            results.append(json.loads(out))
        self.assertEqual(sum("receipt" in x for x in results),1)
        self.assertIn({"error":"RELEASE_KEY_CONTENT_CONFLICT"},results)
        self.assertEqual(self.count(),1)
    def test_process_crash_after_insert_before_commit_rolls_back(self):
        child,_=self.spawn("crash",self.r,self.p,self.e,crash=True)
        out,err=child.communicate(timeout=20)
        self.assertEqual(child.returncode,73,err)
        self.assertEqual(self.reopen().current(*self.key),None)
        self.assertEqual(self.count(),0)
        self.commit()
        self.assertEqual(self.count(),1)
    def test_real_tuf_wheel_review_reserved_atomically(self):
        catalog=CatalogFixture()
        bundle=catalog.bundle(self.inputs)
        cache=self.dir/"tuf";cache.mkdir()
        r=reserve_request(self.lid,bundle["submitted_proposal_raw"],bundle["review_raw"])
        auth=tuf_reservation_authorizer(**self.inputs,catalog_id=CAT,ledger_id=self.lid,
             namespace=NAMESPACE,metadata_files=bundle["metadata_files"],cache_dir=cache,bootstrap=catalog.bootstrap)
        ledger=self.reopen(auth)
        receipt=ledger.apply(encode_proposal(r),bundle["submitted_proposal_raw"],bundle["review_raw"])
        self.assertEqual(receipt["state_after"],"RESERVED_CANDIDATE")
        with sqlite3.connect(self.path) as db:
            context=parse(db.execute("SELECT authority_raw FROM release_events").fetchone()[0])
        self.assertEqual(context["profile"],"TUF_OFFLINE_REVIEW_CANDIDATE")
        self.assertEqual(context["snapshot_version"],1)
        self.assertEqual(context["request_sha256"],receipt["request_sha256"])
        self.assertEqual(receipt["authority_context_sha256"],digest(encode_proposal(context)))
        self.assertNotIn("openrgd_trust_probe",sys.modules)
        # TUF reservation adapter is not a revocation signer.
        w,notice=control(r,"REVOKE",1)
        with self.assertRaisesRegex(LedgerError,"TUF_RESERVATION_SCOPE_ONLY"):
            ledger.apply(encode_proposal(w),bundle["submitted_proposal_raw"],notice)
        self.assertEqual(self.count(),1)
    def test_bad_tuf_signature_never_reserves_key(self):
        catalog=CatalogFixture();bundle=catalog.bundle(self.inputs,bad_publisher_signature=True)
        cache=self.dir/"bad";cache.mkdir()
        r=reserve_request(self.lid,bundle["submitted_proposal_raw"],bundle["review_raw"])
        auth=tuf_reservation_authorizer(**self.inputs,catalog_id=CAT,ledger_id=self.lid,
             namespace=NAMESPACE,metadata_files=bundle["metadata_files"],cache_dir=cache,bootstrap=catalog.bootstrap)
        with self.assertRaisesRegex(LedgerError,"TUF_REVIEW_REJECTED"):
            self.reopen(auth).apply(encode_proposal(r),bundle["submitted_proposal_raw"],bundle["review_raw"])
        self.assertEqual(self.count(),0)

if __name__=="__main__":
    unittest.main(verbosity=2)
