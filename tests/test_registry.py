"""Synthetic registry tests. No network, no hardware, no real module execution."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rgd_registry import core
from rgd_registry.__main__ import main

EXPECTED_PIN = "81700401e2fa6d370a86b6c1f5a48802bc5f1fc7"


def make_fake_source(item, dest):
    dest.mkdir(parents=True)
    manifest = {
        "module_id": item["id"],
        "module_version": item["version"],
        "non_actuating": True,
        "automatic_motion_on_load": False,
        "active_calibration_supported": False,
        "registry_status": "proposal-only",
        "capabilities": list(item["capabilities"]),
    }
    (dest / "module.openrgd.json").write_text(json.dumps(manifest))
    (dest / "Cargo.lock").write_text('# synthetic-only\n[[package]]\nname = "rgd-calibration"\nversion = "0.2.0"\n')
    (dest / "Cargo.toml").write_text(
        '[package]\nname = "rgd-calibration"\nversion = "0.2.0"\n'
    )
    (dest / "src").mkdir()
    (dest / "src" / "marker.txt").write_text("synthetic only")


def make_fake_build(args, *, cwd=None):
    if args[:3] == ["cargo", "build", "--release"]:
        executable = cwd / "target" / "release" / "rgd-calibration"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"synthetic-binary-not-executable")
        return ""
    raise AssertionError(f"unexpected external command: {args}")


class RegistryContractTests(unittest.TestCase):
    def test_calibration_is_registered_at_exact_merged_commit(self):
        entries = core.catalog()
        self.assertEqual(len(entries), 1)
        item = entries[0]
        self.assertEqual(item["id"], "org.openrgd.calibration")
        self.assertEqual(item["version"], "0.2.0")
        self.assertEqual(item["source"]["commit_sha"], EXPECTED_PIN)
        self.assertEqual(item["distribution"]["kind"], "pinned_git_cargo")
        self.assertTrue(item["safety"]["non_actuating"])
        self.assertFalse(item["safety"]["automatic_motion_on_load"])
        self.assertFalse(item["safety"]["active_calibration_supported"])

    def test_catalog_is_local_read_only_and_does_not_clone_or_build(self):
        with mock.patch.object(core, "_run") as runner:
            core.catalog()
            core.entry("org.openrgd.calibration")
            runner.assert_not_called()

    def test_rejects_unknown_version_and_traversal(self):
        with self.assertRaises(core.RegistryError):
            core.entry("org.openrgd.calibration", "1000.0.0")
        with self.assertRaises(core.RegistryError):
            core._check_identity("../escape", "0.2.0")
        with self.assertRaises(core.RegistryError):
            core._safe_descriptor_path("../../../outside.json")

    def test_source_only_install_has_no_build_and_is_not_resolvable(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)
            with mock.patch.object(core, "_fetch_pinned", side_effect=make_fake_source), mock.patch.object(
                core, "_run"
            ) as execute:
                state = core.install("org.openrgd.calibration", prefix=prefix)
                execute.assert_not_called()
            self.assertEqual(state["status"], "source_only")
            self.assertEqual(
                core.status_of("org.openrgd.calibration", prefix=prefix)["status"],
                "source_only",
            )
            with self.assertRaises(core.RegistryError):
                core.resolve_binary("org.openrgd.calibration", prefix=prefix)
            with self.assertRaises(core.RegistryError):
                core.install("org.openrgd.calibration", prefix=prefix)

    def test_source_mutation_must_not_be_treated_as_healthy(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)
            with mock.patch.object(core, "_fetch_pinned", side_effect=make_fake_source):
                core.install("org.openrgd.calibration", prefix=prefix)
            source = prefix / "modules" / "org.openrgd.calibration" / "0.2.0" / "source"
            (source / "src" / "marker.txt").write_text("changed")
            self.assertEqual(
                core.status_of("org.openrgd.calibration", prefix=prefix)["status"],
                "tampered",
            )

    def test_explicit_build_produces_ready_binary_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)
            with (
                mock.patch.object(core, "_fetch_pinned", side_effect=make_fake_source),
                mock.patch.object(core, "_run", side_effect=make_fake_build),
                mock.patch.object(core, "_host", return_value="linux-x86_64"),
            ):
                state = core.install("org.openrgd.calibration", prefix=prefix, build=True)
            self.assertEqual(state["status"], "ready")
            self.assertEqual(state["module_id"], "org.openrgd.calibration")
            resolved = core.resolve_binary("org.openrgd.calibration", prefix=prefix)
            self.assertEqual(resolved["status"], "ready")
            with open(resolved["binary"], "wb") as binary:
                binary.write(b"tampered")
            self.assertEqual(
                core.status_of("org.openrgd.calibration", prefix=prefix)["status"],
                "tampered",
            )
            with self.assertRaises(core.RegistryError):
                core.resolve_binary("org.openrgd.calibration", prefix=prefix)

    def test_unsupported_build_host_is_blocked_before_download(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.object(core, "_host", return_value="win32-x86_64"), mock.patch.object(
                core, "_fetch_pinned"
            ) as fetch:
                with self.assertRaises(core.RegistryError):
                    core.install("org.openrgd.calibration", prefix=Path(folder), build=True)
                fetch.assert_not_called()

    def test_manifest_mismatch_aborts_without_installed_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def bad_source(item, dest):
                make_fake_source(item, dest)
                file = dest / "module.openrgd.json"
                data = json.loads(file.read_text())
                data["automatic_motion_on_load"] = True
                file.write_text(json.dumps(data))
            with mock.patch.object(core, "_fetch_pinned", side_effect=bad_source):
                with self.assertRaises(core.RegistryError):
                    core.install("org.openrgd.calibration", prefix=root)
            self.assertFalse(
                (root / "modules" / "org.openrgd.calibration" / "0.2.0").exists()
            )

    def test_bad_descriptor_permissions_are_rejected(self):
        descriptor = core.entry("org.openrgd.calibration").copy()
        descriptor["permissions_required"] = ["motor.write"]
        with self.assertRaises(core.RegistryError):
            core._validate_descriptor(descriptor)

    def test_cli_discovery_and_status_are_structured(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch("sys.stdout") as out:
                code = main(["catalog"])
                self.assertEqual(code, 0)
                output = "".join(c.args[0] for c in out.call_args_list)
                self.assertIn('"id": "org.openrgd.calibration"', output)
            with mock.patch("sys.stdout") as out:
                code = main(["status", "org.openrgd.calibration", "--prefix", folder])
                self.assertEqual(code, 0)
                output = "".join(c.args[0] for c in out.call_args_list)
                self.assertIn('"status": "not_installed"', output)


if __name__ == "__main__":
    unittest.main()
