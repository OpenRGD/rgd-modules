"""Registry-side integration against actual pinned Core candidate and v0.1 registry."""
from copy import deepcopy
import json
from pathlib import Path
import socket
import subprocess
import unittest
from unittest.mock import patch

from rgd_registry import core
from tools.review_publication import ReviewError, review_publication
from proposal import propose, encode_proposal
from conformance import fixture, raw, source

class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.manifest,self.descriptors,self.artifacts=fixture()
    def submit(self):
        return encode_proposal(propose(raw(self.manifest),self.descriptors,self.artifacts,source()))
    def review(self,submission=None):
        if submission is None:submission=self.submit()
        return review_publication(raw(self.manifest),self.descriptors,self.artifacts,source(),submission)
    def test_actual_legacy_catalog_unchanged(self):
        index=core.REGISTRY_ROOT/"index.json"
        before=index.read_bytes()
        old=core.entry("org.openrgd.calibration","0.2.0")
        result=self.review()
        self.assertEqual(result["review_status"],"review_required")
        self.assertEqual(result["legacy_key_status"],"absent_from_reviewed_v0_1_catalog")
        self.assertFalse(result["registry_publication_committed"])
        self.assertFalse(result["publisher_verified"])
        self.assertFalse(result["installable"])
        self.assertFalse(result["grants_authority"])
        self.assertEqual(before,index.read_bytes())
        self.assertEqual(old,core.entry("org.openrgd.calibration","0.2.0"))
        self.assertFalse(any(d["id"]==self.manifest["module_id"] for d in core.catalog()))
    def test_existing_legacy_key_is_not_overwritten(self):
        self.manifest["module_id"]="org.openrgd.calibration"
        self.manifest["module_version"]="0.2.0"
        with self.assertRaisesRegex(ReviewError,"LEGACY_RELEASE_KEY_CONFLICT"):
            self.review()
    def test_changed_artifact_after_submission_rejected(self):
        original=self.submit();self.artifacts["wheel"]+=b"changed"
        with self.assertRaisesRegex(ReviewError,"PROPOSAL_COMMITMENT_MISMATCH"):
            self.review(original)
    def test_forged_positive_publication_rejected(self):
        p=json.loads(self.submit())
        p["publication_status"]="published"
        p["installable"]=True
        with self.assertRaisesRegex(ReviewError,"PROPOSAL_COMMITMENT_MISMATCH"):
            self.review(raw(p))
    def test_invalid_legacy_snapshot_fails_closed(self):
        with patch.object(core,"catalog",side_effect=core.RegistryError("fixture")):
            with self.assertRaisesRegex(ReviewError,"LEGACY_CATALOG_INVALID"):
                self.review()
    def test_review_does_not_download_install_or_activate(self):
        with patch.object(socket,"socket",side_effect=AssertionError("network")), \
             patch.object(subprocess,"Popen",side_effect=AssertionError("process")), \
             patch.object(core,"install",side_effect=AssertionError("install")), \
             patch.object(core,"resolve_binary",side_effect=AssertionError("resolve")):
            self.review()
    def test_changed_descriptor_not_silently_recommitted(self):
        original=self.submit()
        self.descriptors["extensions/motion.json"]+=b" "
        with self.assertRaisesRegex(ReviewError,"INVALID_PROPOSAL_INPUT"):
            self.review(original)

if __name__=="__main__":
    unittest.main(verbosity=2)
