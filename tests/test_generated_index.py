"""Maintainer-index generation; no install/build or trust verification."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from rgd_registry import core
from tools.generate_index import ROOT, IndexError, generate, check, write

class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/"registry"
        shutil.copytree(ROOT,self.root)
        self.cal=self.root/"modules/org.openrgd.calibration/0.2.0.json"
        self.descriptor=json.loads(self.cal.read_text())

    def put(self, value):
        path=self.root/"modules"/value["id"]/(value["version"]+".json")
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value),encoding="utf-8")
        return path

    def second(self):
        # Test-only descriptor. It is NOT published, installed or real software.
        d=deepcopy(self.descriptor)
        d["id"]="org.openrgd.fixture-probe"
        d["display_name"]="Synthetic test probe"
        return self.put(d)

    def test_current_index_reproduced_byte_for_byte(self):
        self.assertEqual(generate(self.root),(self.root/"index.json").read_bytes())
        self.assertEqual(check(self.root),generate(self.root))

    def test_second_descriptor_discovered_without_manual_index_entry(self):
        self.second()
        with self.assertRaisesRegex(IndexError,"index drift"):check(self.root)
        with patch.object(core,"_run") as runner:
            write(self.root)
            with patch.object(core,"REGISTRY_ROOT",self.root):
                ids=[x["id"] for x in core.catalog()]
            runner.assert_not_called()
        self.assertEqual(ids,["org.openrgd.calibration","org.openrgd.fixture-probe"])
        check(self.root)

    def test_generated_output_deterministic(self):
        self.second()
        first=generate(self.root)
        self.assertEqual(first,generate(self.root))
        write(self.root)
        self.assertEqual(first,generate(self.root))

    def test_multiple_versions_require_exact_selection(self):
        d=deepcopy(self.descriptor); d["version"]="0.10.0"
        self.put(d); write(self.root)
        with patch.object(core,"REGISTRY_ROOT",self.root):
            self.assertEqual(len(core.catalog()),2)
            with self.assertRaises(core.RegistryError):core.entry(d["id"])
            self.assertEqual(core.entry(d["id"],"0.2.0")["version"],"0.2.0")

    def test_no_automatic_catalog_modification(self):
        before=(self.root/"index.json").read_bytes()
        self.second()
        generate(self.root)
        self.assertEqual(before,(self.root/"index.json").read_bytes())

    def test_invalid_descriptor_keeps_index(self):
        before=(self.root/"index.json").read_bytes()
        self.descriptor["permissions_required"]=["motor.write"]
        self.put(self.descriptor)
        with self.assertRaises(IndexError):write(self.root)
        self.assertEqual(before,(self.root/"index.json").read_bytes())

    def test_wrong_identity_path(self):
        self.cal.rename(self.cal.with_name("0.2.1.json"))
        with self.assertRaisesRegex(IndexError,"path/identity"):generate(self.root)

    def test_duplicate_key_rejected(self):
        self.cal.write_text('{"id":"a","id":"b"}')
        with self.assertRaisesRegex(IndexError,"duplicate JSON key"):generate(self.root)

    def test_unknown_fields_rejected(self):
        self.descriptor["grants_authority"]=True
        self.put(self.descriptor)
        with self.assertRaisesRegex(IndexError,"schema violation"):generate(self.root)

    def test_unsupported_distribution_rejected(self):
        self.descriptor["distribution"]["kind"]="arbitrary_shell"
        self.put(self.descriptor)
        with self.assertRaises(IndexError):generate(self.root)

    def test_symlink_descriptor_rejected(self):
        outside=Path(self.temp.name)/"descriptor.json"
        self.cal.rename(outside)
        self.cal.symlink_to(outside)
        with self.assertRaises(IndexError):generate(self.root)

    def test_symlink_index_not_written(self):
        target=self.root/"index.json"
        outside=Path(self.temp.name)/"outside.json"
        target.rename(outside)
        before=outside.read_bytes()
        target.symlink_to(outside)
        with self.assertRaises(IndexError):write(self.root)
        self.assertEqual(before,outside.read_bytes())

    def test_oversized_descriptor_rejected(self):
        self.cal.write_text(" "*65537)
        with self.assertRaisesRegex(IndexError,"size limit"):generate(self.root)

    def test_unexpected_files_not_silently_omitted(self):
        self.cal.with_suffix(".bak").write_text("fixture")
        with self.assertRaisesRegex(IndexError,"unexpected descriptor"):generate(self.root)

    def test_empty_module_folder_rejected(self):
        (self.root/"modules/org.openrgd.empty").mkdir()
        with self.assertRaisesRegex(IndexError,"empty module directory"):generate(self.root)

    def test_invalid_source_pin_rejected(self):
        self.descriptor["source"]["commit_sha"]="main"
        self.put(self.descriptor)
        with self.assertRaises(IndexError):generate(self.root)
