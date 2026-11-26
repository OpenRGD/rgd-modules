"""Actual TUF signatures and client refresh against ephemeral fixture metadata.

Build a real synthetic wheel via the pinned Core fixture. No production keys,
remote network, registry mutation, provider import or installation.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from securesystemslib.signer import CryptoSigner
from tuf.api.metadata import (Metadata, Root, Targets, Snapshot, Timestamp,
                              Delegations, DelegatedRole, MetaFile, TargetFile)
from proposal import encode_proposal, digest, parse
from trust_fixture import build_fixture, proposal_for
from wheel_inspection import inspect_wheel, review_target
from tools.review_trusted_candidate import review_trusted_candidate, TrustError
from rgd_registry import core as legacy

NAMESPACE="org.example"
ROLE="publisher-"+NAMESPACE
def sign(payload, signer):
    m=Metadata(payload)
    m.sign(signer)
    return m.to_bytes()

class CatalogFixture:
    def __init__(self):
        self.keys={r:CryptoSigner.generate_ed25519() for r in ("root","targets","snapshot","timestamp","publisher","other")}
        self.expiry=datetime.now(timezone.utc)+timedelta(days=2)
        root=Root(expires=self.expiry,consistent_snapshot=True)
        for r in ("root","targets","snapshot","timestamp"):
            root.add_key(self.keys[r].public_key,r)
        self.bootstrap=sign(root,self.keys["root"])
    def bundle(self,inputs,version=1,withdraw=False,omit_delegation=False,
               bad_publisher_signature=False,bad_registry_signature=False,
               shared_key=False,bypass=False,overlap=False,wrong_scope=False,
               expired=False,wrong_report=False,missing_review=False,
               missing_proposal=False,nested=False):
        proposal=proposal_for(inputs)
        report=inspect_wheel(**inputs,submitted_proposal_raw=proposal)
        review=review_target(report,NAMESPACE,"WITHDRAWN" if withdraw else "REVIEWED_CANDIDATE")
        if wrong_report:review["inspection_sha256"]="sha256:"+"f"*64
        review_raw=encode_proposal(review)
        pp=f"proposals/{report['module_id']}/{report['module_version']}.json"
        rp=f"reviews/{report['module_id']}/{report['module_version']}.json"
        pk=self.keys["targets"] if shared_key else self.keys["publisher"]
        key=pk.public_key
        scope="proposals/org.other.*/*" if wrong_scope else f"proposals/{NAMESPACE}.*/*"
        role=DelegatedRole(ROLE,[key.keyid],1,True,paths=[scope])
        roles={ROLE:role}
        if overlap:
            roles["other-matching"]=DelegatedRole("other-matching",[key.keyid],1,False,paths=[scope])
        delegates=Delegations({key.keyid:key},roles=roles)
        top_targets={} if missing_review else {rp:TargetFile.from_data(rp,review_raw)}
        if bypass:top_targets[pp]=TargetFile.from_data(pp,proposal)
        parent=Targets(version=version,expires=self.expiry,targets=top_targets,
                       delegations=None if omit_delegation else delegates)
        if nested:
            sub=Delegations({key.keyid:key},roles={"nested":DelegatedRole("nested",[key.keyid],1,True,paths=[scope])})
        else:sub=None
        child=Targets(version=version,expires=self.expiry,
                      targets={} if missing_proposal else {pp:TargetFile.from_data(pp,proposal)},delegations=sub)
        parent_raw=sign(parent,self.keys["publisher"] if bad_registry_signature else self.keys["targets"])
        child_raw=sign(child,self.keys["other"] if bad_publisher_signature else pk)
        snap=Snapshot(version=version,expires=self.expiry,meta={
            "targets.json":MetaFile.from_data(version,parent_raw,["sha256"]),
            ROLE+".json":MetaFile.from_data(version,child_raw,["sha256"])})
        snap_raw=sign(snap,self.keys["snapshot"])
        stamp=Timestamp(version=version,
                        expires=datetime.now(timezone.utc)-timedelta(days=1) if expired else self.expiry,
                        snapshot_meta=MetaFile.from_data(version,snap_raw,["sha256"]))
        return {"metadata_files":{"timestamp.json":sign(stamp,self.keys["timestamp"]),
                  f"{version}.snapshot.json":snap_raw,f"{version}.targets.json":parent_raw,
                  f"{version}.{ROLE}.json":child_raw},
                "review_raw":review_raw,"submitted_proposal_raw":proposal}

class TrustTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.built=build_fixture(cls.temp.name)
    def setUp(self):
        self.inputs=deepcopy(self.built)
        self.catalog=CatalogFixture()
        self.temp2=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp2.cleanup)
        self.cache=Path(self.temp2.name)
    def run_review(self,bundle=None,bootstrap=True,prior=None,namespace=NAMESPACE):
        if bundle is None:bundle=self.catalog.bundle(self.inputs)
        return review_trusted_candidate(**self.inputs,**bundle,namespace=namespace,
                     cache_dir=self.cache,bootstrap=self.catalog.bootstrap if bootstrap else None,
                     prior_proposal_sha256=prior)
    def test_signed_delegation_and_independent_review_actual_wheel(self):
        before=(legacy.REGISTRY_ROOT/"index.json").read_bytes()
        bundle=self.catalog.bundle(self.inputs)
        with patch.object(socket,"socket",side_effect=AssertionError("network")), \
             patch.object(subprocess,"Popen",side_effect=AssertionError("process")), \
             patch.object(legacy,"install",side_effect=AssertionError("install")):
            r=self.run_review(bundle)
        self.assertTrue(r["publisher_delegation_verified"])
        self.assertTrue(r["registry_review_target_verified"])
        self.assertEqual(r["tuf_snapshot_version"],1)
        self.assertEqual(r["inspection"]["wheel_format"],"VERIFIED_RESTRICTED_PROFILE")
        self.assertEqual(r["review_status"],"REVIEWED_CANDIDATE_ONLY")
        for k in ("installable","grants_authority","registry_publication_committed","source_commit_verified",
                  "build_provenance_verified","publisher_legal_identity_verified","global_release_uniqueness_verified"):
            self.assertIs(r[k],False)
        self.assertNotIn("openrgd_trust_probe",sys.modules)
        self.assertEqual(before,(legacy.REGISTRY_ROOT/"index.json").read_bytes())
    def test_wrong_delegated_signer(self):
        with self.assertRaisesRegex(TrustError,"TUF_VERIFICATION_REJECTED"):
            self.run_review(self.catalog.bundle(self.inputs,bad_publisher_signature=True))
    def test_wrong_registry_signer(self):
        with self.assertRaisesRegex(TrustError,"TUF_VERIFICATION_REJECTED"):
            self.run_review(self.catalog.bundle(self.inputs,bad_registry_signature=True))
    def test_independent_roles_reject_same_key(self):
        with self.assertRaisesRegex(TrustError,"ROLE_KEY_REUSE"):
            self.run_review(self.catalog.bundle(self.inputs,shared_key=True))
    def test_parent_cannot_bypass_publisher(self):
        with self.assertRaisesRegex(TrustError,"PUBLISHER_DELEGATION_BYPASS"):
            self.run_review(self.catalog.bundle(self.inputs,bypass=True))
    def test_namespace_prefix_not_identity(self):
        with self.assertRaisesRegex(TrustError,"PUBLICATION_OR_ARTIFACT_REVIEW_REJECTED"):
            self.run_review(namespace="org.exam")
    def test_wrong_scope(self):
        with self.assertRaisesRegex(TrustError,"PUBLISHER_DELEGATION_SCOPE"):
            self.run_review(self.catalog.bundle(self.inputs,wrong_scope=True))
    def test_overlapping_matching_delegations(self):
        with self.assertRaisesRegex(TrustError,"PUBLISHER_DELEGATION_AMBIGUOUS"):
            self.run_review(self.catalog.bundle(self.inputs,overlap=True))
    def test_signed_report_for_other_content_is_rejected(self):
        with self.assertRaisesRegex(TrustError,"REGISTRY_REVIEW_BINDING_MISMATCH"):
            self.run_review(self.catalog.bundle(self.inputs,wrong_report=True))
    def test_target_bytes_tamper(self):
        bundle=self.catalog.bundle(self.inputs)
        bundle["review_raw"]+=b" "
        with self.assertRaisesRegex(TrustError,"TUF_VERIFICATION_REJECTED"):
            self.run_review(bundle)
    def test_expired_timestamp(self):
        with self.assertRaisesRegex(TrustError,"TUF_VERIFICATION_REJECTED"):
            self.run_review(self.catalog.bundle(self.inputs,expired=True))
    def test_snapshot_rollback_with_persistent_cache(self):
        old=self.catalog.bundle(self.inputs,version=1)
        current=self.catalog.bundle(self.inputs,version=2)
        self.assertEqual(self.run_review(current)["tuf_snapshot_version"],2)
        with self.assertRaisesRegex(TrustError,"TUF_VERIFICATION_REJECTED"):
            self.run_review(old,bootstrap=False)
    def test_signed_withdrawal_after_success(self):
        old=self.catalog.bundle(self.inputs)
        self.run_review(old)
        withdrawn=self.catalog.bundle(self.inputs,version=2,withdraw=True)
        with self.assertRaisesRegex(TrustError,"REGISTRY_REVIEW_WITHDRAWN"):
            self.run_review(withdrawn,bootstrap=False)
        # Even after denial, cannot roll back to the earlier timestamp.
        with self.assertRaisesRegex(TrustError,"TUF_VERIFICATION_REJECTED"):
            self.run_review(old,bootstrap=False)
    def test_revoked_delegation_after_success(self):
        self.run_review()
        with self.assertRaisesRegex(TrustError,"PUBLISHER_DELEGATION_MISSING"):
            self.run_review(self.catalog.bundle(self.inputs,version=2,omit_delegation=True),bootstrap=False)
    def test_no_bootstrap_reset_after_refresh(self):
        self.run_review()
        with self.assertRaisesRegex(TrustError,"BOOTSTRAP_RESET_FORBIDDEN"):
            self.run_review()
    def test_no_implicit_bootstrap(self):
        with self.assertRaisesRegex(TrustError,"BOOTSTRAP_REQUIRED"):
            self.run_review(bootstrap=False)
    def test_missing_review_target(self):
        with self.assertRaisesRegex(TrustError,"REGISTRY_REVIEW_MISSING"):
            self.run_review(self.catalog.bundle(self.inputs,missing_review=True))
    def test_missing_proposal_target(self):
        with self.assertRaisesRegex(TrustError,"TARGET_MISSING_OR_UNSUPPORTED"):
            self.run_review(self.catalog.bundle(self.inputs,missing_proposal=True))
    def test_snapshot_hash_mismatch(self):
        bundle=self.catalog.bundle(self.inputs)
        bundle["metadata_files"]["1.snapshot.json"]+=b" "
        with self.assertRaisesRegex(TrustError,"TUF_VERIFICATION_REJECTED"):
            self.run_review(bundle)
    def test_duplicate_metadata_json_keys_rejected(self):
        bundle=self.catalog.bundle(self.inputs)
        bundle["metadata_files"]["timestamp.json"]=b'{"signed":{},"signed":{}}'
        with self.assertRaisesRegex(TrustError,"METADATA_JSON_REJECTED"):
            self.run_review(bundle)
    def test_nested_delegation_is_not_this_profile(self):
        with self.assertRaisesRegex(TrustError,"DIRECT_PUBLISHER_TARGET_REQUIRED"):
            self.run_review(self.catalog.bundle(self.inputs,nested=True))
    def test_same_key_re_signed_changed_content_conflict(self):
        old=self.catalog.bundle(self.inputs)
        first=self.run_review(old)
        self.assertTrue(self.run_review(old,bootstrap=False,prior=first["proposal_sha256"])["local_prior_commitment_matched"])
        source=parse(self.inputs["source_claim"])
        source["git_commit"]="b"*40
        self.inputs["source_claim"]=encode_proposal(source)
        new=self.catalog.bundle(self.inputs,version=2)
        with self.assertRaisesRegex(TrustError,"RELEASE_KEY_CONTENT_CONFLICT"):
            self.run_review(new,bootstrap=False,prior=first["proposal_sha256"])

if __name__=="__main__":
    unittest.main(verbosity=2)
