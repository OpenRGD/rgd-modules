"""SQLite reference candidate release ledger. Local trusted maintainer process only.

No catalog publication, network API, installation, production grants or provider
execution. One private database per catalog/lineage; owner supplies authority.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3

from rgd_registry import core as legacy
from ledger_contract import (decode, checked, transition, validate_evidence,
    make_receipt, LedgerContractError, digest, encode_proposal, parse, ProposalError)

MAX_EVENTS = 1024
MAX_BYTES = 65536
SCHEMA_VERSION = 1

class LedgerError(ValueError):
    """Fixed rejection diagnostic. Retry ambiguous outcomes with original ID."""

DDL = """
CREATE TABLE ledger_meta (
  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
  catalog_id TEXT NOT NULL,
  ledger_id TEXT NOT NULL,
  legacy_catalog_raw BLOB NOT NULL
);
CREATE TABLE release_events (
  seq INTEGER PRIMARY KEY CHECK(seq>0),
  request_id TEXT NOT NULL UNIQUE,
  module_id TEXT NOT NULL,
  module_version TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>0),
  request_raw BLOB NOT NULL,
  proposal_raw BLOB NOT NULL,
  evidence_raw BLOB NOT NULL,
  authority_raw BLOB NOT NULL,
  receipt_raw BLOB NOT NULL,
  receipt_sha256 TEXT NOT NULL,
  UNIQUE(module_id,module_version,revision)
);
CREATE TRIGGER meta_no_update BEFORE UPDATE ON ledger_meta BEGIN
  SELECT RAISE(ABORT,'immutable ledger metadata'); END;
CREATE TRIGGER meta_no_delete BEFORE DELETE ON ledger_meta BEGIN
  SELECT RAISE(ABORT,'immutable ledger metadata'); END;
CREATE TRIGGER events_no_update BEFORE UPDATE ON release_events BEGIN
  SELECT RAISE(ABORT,'append-only release events'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON release_events BEGIN
  SELECT RAISE(ABORT,'append-only release events'); END;
"""
REQUIRED_TRIGGERS={"meta_no_update","meta_no_delete","events_no_update","events_no_delete"}

def require(ok,code):
    if not ok:
        raise LedgerError(code)

def legacy_snapshot():
    try:
        rows=legacy.catalog()
        data=encode_proposal(rows)
        require(len(data)<=MAX_BYTES and len(rows)<=MAX_EVENTS,"LEGACY_CATALOG_LIMIT")
        return data,{(x["id"],x["version"]) for x in rows}
    except (legacy.RegistryError,ValueError,TypeError,KeyError) as e:
        if isinstance(e,LedgerError):raise
        raise LedgerError("LEGACY_CATALOG_INVALID") from e

def scope_checkpoint(catalog_id,ledger_id,legacy_raw,seq=0,event_sha=None):
    return checked("ReleaseLedgerCheckpoint",{
        "schema_version":"openrgd.release-ledger-checkpoint.v0.1-candidate",
        "catalog_id":catalog_id,"ledger_id":ledger_id,"legacy_catalog_sha256":digest(legacy_raw),
        "event_sequence":seq,"event_sha256":event_sha,"grants_authority":False})

def connect(path):
    path=Path(path)
    require(path.is_file() and not path.is_symlink(),"LEDGER_FILE_REQUIRED")
    db=sqlite3.connect(path.resolve().as_uri()+"?mode=rw",uri=True,timeout=5.0,isolation_level=None)
    db.row_factory=sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA recursive_triggers=ON")
    return db

class CandidateLedger:
    """Explicit initialization/open; authorizer is trusted local code, not JSON.

    authorizer(request_raw, proposal_raw, evidence_raw, prior_receipt_raw)
    must independently check the current owner policy and evidence, return a
    bounded exact verification-context byte string, or raise. It is executed
    inside the serialized writer transaction for NEW requests only. No default
    positive authorizer. Callers must not expose this interface to untrusted
    code or use a fixture policy in production.
    """
    def __init__(self,path,catalog_id,ledger_id,*,authorizer=None,minimum_checkpoint=None):
        self.path=Path(path).absolute()
        self.catalog_id=catalog_id
        self.ledger_id=ledger_id
        self._authorizer=authorizer
        self.legacy_raw,self._legacy_keys=legacy_snapshot()
        scope_checkpoint(catalog_id,ledger_id,self.legacy_raw)
        with self._transaction(False) as db:
            _,seq,last=self._audit(db)
            if minimum_checkpoint is not None:
                self._verify_checkpoint(db,minimum_checkpoint,seq)
    @classmethod
    def initialize(cls,path,catalog_id,ledger_id,*,authorizer=None):
        """Only explicit exclusive creation. Never recreate missing/corrupt state."""
        p=Path(path).absolute()
        raw,_=legacy_snapshot()
        scope_checkpoint(catalog_id,ledger_id,raw)
        try:
            fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            os.close(fd)
        except FileExistsError as e:
            raise LedgerError("LEDGER_ALREADY_EXISTS") from e
        except OSError as e:
            raise LedgerError("LEDGER_CREATE_FAILED") from e
        # Keep an incomplete file on failure (fail closed); no auto-retry/reset.
        db=None
        try:
            db=connect(p)
            require(db.execute("PRAGMA journal_mode=DELETE").fetchone()[0]=="delete","LEDGER_JOURNAL_MODE")
            db.executescript("BEGIN IMMEDIATE;\n"+DDL)
            db.execute("INSERT INTO ledger_meta VALUES (1,?,?,?)",(catalog_id,ledger_id,raw))
            db.execute("PRAGMA user_version=1")
            db.commit()
        except (sqlite3.Error,OSError) as e:
            if db is not None:db.rollback()
            raise LedgerError("LEDGER_CREATE_FAILED") from e
        finally:
            if db is not None:db.close()
        return cls(p,catalog_id,ledger_id,authorizer=authorizer)

    @contextmanager
    def _transaction(self,write):
        db=None
        try:
            db=connect(self.path)
            require(db.execute("PRAGMA journal_mode").fetchone()[0]=="delete","LEDGER_JOURNAL_MODE")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except (sqlite3.Error,OSError) as e:
            if db is not None:db.rollback()
            raise LedgerError("LEDGER_STORAGE_FAILURE_RETRY_OR_RECOVER") from e
        except BaseException:
            if db is not None:db.rollback()
            raise
        finally:
            if db is not None:db.close()

    def _meta(self,db):
        require(db.execute("PRAGMA user_version").fetchone()[0]==SCHEMA_VERSION,"LEDGER_SCHEMA_VERSION")
        triggers={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        require(REQUIRED_TRIGGERS.issubset(triggers),"LEDGER_SCHEMA_INCOMPLETE")
        rows=db.execute("SELECT * FROM ledger_meta LIMIT 2").fetchall()
        require(len(rows)==1 and rows[0]["singleton"]==1,"LEDGER_METADATA_MISSING")
        meta=rows[0]
        require((meta["catalog_id"],meta["ledger_id"])==(self.catalog_id,self.ledger_id),"LEDGER_SCOPE_MISMATCH")
        current,_=legacy_snapshot()
        require(meta["legacy_catalog_raw"]==self.legacy_raw==current,"LEGACY_CATALOG_DRIFT")

    def _scope(self,req):
        require((req["catalog_id"],req["ledger_id"])==(self.catalog_id,self.ledger_id),"LEDGER_SCOPE_MISMATCH")
        require((req["module_id"],req["module_version"]) not in self._legacy_keys,"LEGACY_RELEASE_KEY_CONFLICT")

    def _audit(self,db):
        """Bounded full replay from durable bytes, not trust authentication."""
        self._meta(db)
        states={}
        seq=0
        last=None
        for row in db.execute("SELECT * FROM release_events ORDER BY seq LIMIT ?",(MAX_EVENTS+1,)):
            seq+=1
            require(seq<=MAX_EVENTS and row["seq"]==seq,"LEDGER_SEQUENCE_INVALID")
            try:
                req=decode("ReleaseLedgerRequest",row["request_raw"])
                self._scope(req)
                key=req["module_id"],req["module_version"]
                require(row["request_id"]==req["request_id"] and
                        (row["module_id"],row["module_version"])==key,"LEDGER_ROW_KEY_MISMATCH")
                validate_evidence(req,row["proposal_raw"],row["evidence_raw"])
                prior=states.get(key)
                expected=make_receipt(req,row["request_raw"],row["authority_raw"],seq,last,prior)
                raw=encode_proposal(expected)
                require(row["revision"]==expected["release_revision"] and
                        raw==row["receipt_raw"] and digest(raw)==row["receipt_sha256"],
                        "LEDGER_RECEIPT_MISMATCH")
            except LedgerContractError as e:
                raise LedgerError("LEDGER_HISTORY_INVALID") from e
            last=digest(raw)
            states[key]=expected
        return states,seq,last

    def _verify_checkpoint(self,db,raw,seq):
        check=decode("ReleaseLedgerCheckpoint",raw)
        require((check["catalog_id"],check["ledger_id"],check["legacy_catalog_sha256"])==
                (self.catalog_id,self.ledger_id,digest(self.legacy_raw)),"CHECKPOINT_SCOPE_MISMATCH")
        n=check["event_sequence"]
        require(n<=seq,"CHECKPOINT_ROLLBACK")
        if n:
            row=db.execute("SELECT receipt_sha256 FROM release_events WHERE seq=?",(n,)).fetchone()
            require(row is not None and row[0]==check["event_sha256"],"CHECKPOINT_PREFIX_CONFLICT")

    def checkpoint(self):
        with self._transaction(False) as db:
            _,seq,last=self._audit(db)
            return encode_proposal(scope_checkpoint(self.catalog_id,self.ledger_id,self.legacy_raw,seq,last))

    def verify_checkpoint(self,raw):
        with self._transaction(False) as db:
            _,seq,_=self._audit(db)
            self._verify_checkpoint(db,raw,seq)

    def lookup_receipt(self,request_id):
        checked("Uuid",request_id)
        with self._transaction(False) as db:
            self._audit(db)
            row=db.execute("SELECT receipt_raw FROM release_events WHERE request_id=?",(request_id,)).fetchone()
            return None if row is None else decode("ReleaseLedgerReceipt",row[0])

    def current(self,module_id,module_version):
        # No raw input becomes SQL or a filesystem locator.
        with self._transaction(False) as db:
            states,_,_=self._audit(db)
            return states.get((module_id,module_version))

    def apply(self,request_raw,proposal_raw,evidence_raw):
        """Atomic candidate transition or exact successful retry."""
        try:
            req=decode("ReleaseLedgerRequest",request_raw)
            self._scope(req)
            validate_evidence(req,proposal_raw,evidence_raw)
            with self._transaction(True) as db:
                states,seq,last=self._audit(db)
                retry=db.execute("SELECT request_raw,proposal_raw,evidence_raw,receipt_raw "
                    "FROM release_events WHERE request_id=?",(req["request_id"],)).fetchone()
                if retry is not None:
                    require((retry["request_raw"],retry["proposal_raw"],retry["evidence_raw"])==
                            (request_raw,proposal_raw,evidence_raw),"IDEMPOTENCY_CONFLICT")
                    return decode("ReleaseLedgerReceipt",retry["receipt_raw"])
                key=req["module_id"],req["module_version"]
                prior=states.get(key)
                transition(req,prior)  # conflict/stale/terminal before authorizer
                require(seq<MAX_EVENTS,"LEDGER_CAPACITY_REACHED")
                require(callable(self._authorizer),"OWNER_AUTHORITY_REQUIRED")
                auth=self._authorizer(request_raw,proposal_raw,evidence_raw,
                                      None if prior is None else encode_proposal(prior))
                require(type(auth) is bytes and 0<len(auth)<=MAX_BYTES,"OWNER_AUTHORITY_CONTEXT_REQUIRED")
                # Treat context as opaque evidence; the callback is the trust
                # boundary, not a producer-supplied boolean or self-declared proof.
                receipt=make_receipt(req,request_raw,auth,seq+1,last,prior)
                raw=encode_proposal(receipt)
                db.execute("INSERT INTO release_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (seq+1,req["request_id"],*key,receipt["release_revision"],request_raw,
                     proposal_raw,evidence_raw,auth,raw,digest(raw)))
                # Commit is performed by the context manager before returning.
                return receipt
        except LedgerContractError as e:
            raise LedgerError(str(e)) from e
