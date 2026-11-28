"""Real signed source/build fixture -> TUF -> SQLite candidate ledger.

No production publisher or install. Test keys ephemeral, separate fixtures.
"""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from uuid import uuid4

from securesystemslib.signer import CryptoSigner
from proposal import parse, digest, encode_proposal
from provenance_fixture import build_source_fixture, verification_inputs, statement_for, sign_statement, PACKAGE
from test_trusted_review import CatalogFixture,NAMESPACE
from tools.release_ledger import CandidateLedger,LedgerError
from tools.provenance_ledger_authority import provenance_reservation_authorizer
from test_release_ledger import reserve_request,CAT
from rgd_registry import core as legacy

class ProvenanceLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build=tempfile.TemporaryDirectory();cls.addClassCleanup(cls.build.cleanup)
        cls.fixture=build_source_fixture(cls.build.name)
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/"ledger.sqlite"
        self.cache=self.root/"tuf";self.cache.mkdir()
        self.lid=str(uuid4());self.catalog=CatalogFixture()
        self.inputs=deepcopy(self.fixture["inputs"])
        self.bundle=self.catalog.bundle(self.inputs)
        self.prov=verification_inputs(self.fixture,self.inputs)
        self.r=reserve_request(self.lid,self.bundle["submitted_proposal_raw"],self.bundle["review_raw"])
        self.raw=encode_proposal(self.r)
        self.ledger=CandidateLedger.initialize(self.path,CAT,self.lid)
    def authorizer(self,**changes):
        args={**self.inputs,"catalog_id":CAT,"ledger_id":self.lid,"namespace":NAMESPACE,
              "metadata_files":self.bundle["metadata_files"],"cache_dir":self.cache,
              "bootstrap":self.catalog.bootstrap,
              "attestation_raw":self.prov["attestation_raw"],"policy_raw":self.prov["policy_raw"],
              "local_repository":self.fixture["repository"]}
        args.update(changes)
        return provenance_reservation_authorizer(**args)
    def apply(self,**changes):
        l=CandidateLedger(self.path,CAT,self.lid,authorizer=self.authorizer(**changes))
        return l.apply(self.raw,self.bundle["submitted_proposal_raw"],self.bundle["review_raw"])
    def count(self):return parse(self.ledger.checkpoint())["event_sequence"]
    def test_actual_build_tuf_and_ledger_chain(self):
        before=(legacy.REGISTRY_ROOT/"index.json").read_bytes()
        receipt=self.apply()
        self.assertEqual(receipt["state_after"],"RESERVED_CANDIDATE")
        self.assertFalse(receipt["publication_committed"]);self.assertFalse(receipt["installable"])
        with sqlite3.connect(self.path) as db:
            raw=db.execute("SELECT authority_raw FROM release_events").fetchone()[0]
        ctx=parse(raw);p=ctx["provenance_review"]
        self.assertEqual(ctx["profile"],"TUF_AND_BUILD_PROVENANCE_REVIEW_CANDIDATE")
        self.assertEqual(ctx["provenance_review_sha256"],digest(encode_proposal(p)))
        self.assertEqual(ctx["builder_policy_sha256"],digest(self.prov["policy_raw"]))
        self.assertEqual(ctx["attestation_sha256"],digest(self.prov["attestation_raw"]))
        self.assertEqual(p["proposal_sha256"],receipt["proposal_sha256"])
        self.assertEqual(ctx["tuf_review_context"]["request_sha256"],receipt["request_sha256"])
        self.assertEqual(receipt["authority_context_sha256"],digest(raw))
        self.assertTrue(ctx["builder_key_separation_verified"])
        self.assertTrue(p["source_commit_membership_verified"]);self.assertTrue(p["builder_signature_verified"])
        self.assertTrue(p["wheel_package_bytes_matched"]);self.assertFalse(p["slsa_level_verified"])
        self.assertEqual(self.count(),1)
        self.assertEqual(self.ledger.apply(self.raw,self.bundle["submitted_proposal_raw"],self.bundle["review_raw"]),receipt)
        self.assertEqual(before,(legacy.REGISTRY_ROOT/"index.json").read_bytes())
        self.assertNotIn(PACKAGE,sys.modules)
    def test_wrong_builder_signature_no_event(self):
        wrong=sign_statement(statement_for(self.fixture),CryptoSigner.generate_ed25519())
        with self.assertRaisesRegex(LedgerError,"BUILD_PROVENANCE_REJECTED"):
            self.apply(attestation_raw=wrong)
        self.assertEqual(self.count(),0)
        self.assertIsNone(self.ledger.lookup_receipt(self.r["request_id"]))
        self.assertFalse(any(self.cache.iterdir()))
    def test_wrong_recipe_no_event_even_if_tuf_is_valid(self):
        bad={**self.fixture["policy"],"recipe_sha256":"sha256:"+"f"*64}
        f={**self.fixture,"policy":bad}
        att=sign_statement(statement_for(f),self.fixture["signer"])
        with self.assertRaisesRegex(LedgerError,"BUILD_PROVENANCE_REJECTED"):
            self.apply(policy_raw=encode_proposal(bad),attestation_raw=att)
        self.assertEqual(self.count(),0)
    def test_shared_builder_and_publisher_key_rejected(self):
        signer=self.catalog.keys["publisher"]
        p={**self.fixture["policy"],"builder_keyid":signer.public_key.keyid,
           "builder_public_key":signer.public_key.keyval["public"]}
        att=sign_statement(statement_for(self.fixture),signer)
        with self.assertRaisesRegex(LedgerError,"BUILDER_ROLE_KEY_REUSE"):
            self.apply(policy_raw=encode_proposal(p),attestation_raw=att)
        self.assertEqual(self.count(),0)
    def test_shared_builder_and_registry_key_rejected(self):
        signer=self.catalog.keys["targets"]
        p={**self.fixture["policy"],"builder_keyid":signer.public_key.keyid,
           "builder_public_key":signer.public_key.keyval["public"]}
        with self.assertRaisesRegex(LedgerError,"BUILDER_ROLE_KEY_REUSE"):
            self.apply(policy_raw=encode_proposal(p),attestation_raw=sign_statement(statement_for(self.fixture),signer))
        self.assertEqual(self.count(),0)
    def test_valid_provenance_does_not_bypass_tuf(self):
        self.bundle=self.catalog.bundle(self.inputs,bad_publisher_signature=True)
        with self.assertRaisesRegex(LedgerError,"TUF_REVIEW_REJECTED"):
            self.apply()
        self.assertEqual(self.count(),0)
    def test_missing_repository_never_reserves(self):
        with self.assertRaisesRegex(LedgerError,"BUILD_PROVENANCE_REJECTED"):
            self.apply(local_repository=self.root/"missing")
        self.assertEqual(self.count(),0)

if __name__=="__main__":unittest.main(verbosity=2)
