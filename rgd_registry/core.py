"""OpenRGD module registry prototype: read-only discovery and explicit local installs.

No automatic execution on discovery, no shell=True, no motor commands.
The registry is a candidate distribution contract, not the Canonical RGD spec.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REGISTRY_ROOT = Path(__file__).resolve().parent.parent / "registry"
CONTRACT_VERSION = "0.1.0-candidate"
_ID = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9-]*)+$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_OPENRGD_GIT = re.compile(r"^https://github\.com/OpenRGD/[a-z0-9-]+\.git$")


class RegistryError(Exception):
    """Registry/catalog/install failure that must not be silently downgraded."""


def default_prefix() -> Path:
    data = os.environ.get("XDG_DATA_HOME")
    if data:
        return Path(data).expanduser().resolve() / "openrgd"
    return (Path.home() / ".local" / "share" / "openrgd").resolve()


def _safe_descriptor_path(relative: str) -> Path:
    if not isinstance(relative, str) or not relative.startswith("modules/"):
        raise RegistryError("catalog descriptor must live under registry/modules/")
    candidate = (REGISTRY_ROOT / relative).resolve()
    if not candidate.is_relative_to((REGISTRY_ROOT / "modules").resolve()):
        raise RegistryError("catalog path escaped registry/modules")
    return candidate


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise RegistryError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RegistryError(f"JSON object required in {path}")
    return value


def catalog() -> list[dict]:
    index = _read_json(REGISTRY_ROOT / "index.json")
    if index.get("registry_schema_version") != CONTRACT_VERSION:
        raise RegistryError("unknown registry schema version")
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in index.get("modules", []):
        if not isinstance(row, dict):
            raise RegistryError("invalid index entry")
        module_id, version = row.get("id"), row.get("version")
        _check_identity(module_id, version)
        key = (module_id, version)
        if key in seen:
            raise RegistryError("duplicate module/version in index")
        seen.add(key)
        descriptor = _read_json(_safe_descriptor_path(row.get("path")))
        _validate_descriptor(descriptor)
        if (descriptor["id"], descriptor["version"]) != key:
            raise RegistryError("index identity does not match descriptor")
        result.append(descriptor)
    return sorted(result, key=lambda x: (x["id"], x["version"]))


def _check_identity(module_id: object, version: object) -> None:
    if not isinstance(module_id, str) or not _ID.fullmatch(module_id):
        raise RegistryError("invalid module id")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise RegistryError("invalid semantic version")


def _validate_descriptor(item: dict) -> None:
    _check_identity(item.get("id"), item.get("version"))
    if item.get("schema_version") != CONTRACT_VERSION:
        raise RegistryError("unsupported descriptor schema")
    if item.get("maturity") != "candidate":
        raise RegistryError("only candidate descriptors supported in registry v0.1")
    source, distribution, safety = (
        item.get("source"),
        item.get("distribution"),
        item.get("safety"),
    )
    if not all(isinstance(x, dict) for x in (source, distribution, safety)):
        raise RegistryError("source, distribution and safety are required objects")
    if not _OPENRGD_GIT.fullmatch(source.get("git_url", "")):
        raise RegistryError("unapproved git origin; only OpenRGD HTTPS repos allowed")
    if not _GIT_SHA.fullmatch(source.get("commit_sha", "")):
        raise RegistryError("source must pin an immutable 40-hex commit SHA")
    if source.get("module_manifest") != "module.openrgd.json":
        raise RegistryError("unrecognized module manifest")
    if distribution.get("kind") != "pinned_git_cargo":
        raise RegistryError("unsupported distribution kind")
    if distribution.get("package") != "rgd-calibration":
        raise RegistryError("unapproved Rust package in bootstrap registry")
    if distribution.get("binary") != "rgd-calibration":
        raise RegistryError("unapproved executable name")
    hosts = distribution.get("hosts", [])
    if hosts != ["linux-x86_64"]:
        raise RegistryError("registry v0.1 supports linux-x86_64 only")
    if safety != {
        "non_actuating": True,
        "automatic_motion_on_load": False,
        "active_calibration_supported": False,
    }:
        raise RegistryError("bootstrap registry refuses unapproved safety flags")
    if item.get("permissions_required") != []:
        raise RegistryError("calibration must request no host permissions")
    caps = item.get("capabilities")
    if not isinstance(caps, list) or not caps or any(
        not isinstance(x, str) or not x for x in caps
    ):
        raise RegistryError("nonempty capability list required")


def entry(module_id: str, version: str | None = None) -> dict:
    hits = [x for x in catalog() if x["id"] == module_id]
    if version:
        hits = [x for x in hits if x["version"] == version]
    if not hits:
        raise RegistryError("module/version not registered")
    if len(hits) != 1:
        raise RegistryError("version required when multiple releases exist")
    return hits[0]


def _install_location(item: dict, prefix: Path) -> Path:
    # Validated identifiers cannot contain '/' or '..' path traversal segments.
    _check_identity(item["id"], item["version"])
    return prefix / "modules" / item["id"] / item["version"]


def _digest(path: Path) -> str:
    hash_ = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hash_.update(chunk)
    return "sha256:" + hash_.hexdigest()


def _run(argv: list[str], *, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1200,
        )
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", None)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        raise RegistryError(
            f"command {argv[0]!r} failed: {(detail or str(exc))[-1500:]}"
        ) from exc
    return result.stdout.strip()


def _fetch_pinned(item: dict, dest: Path) -> None:
    git_url = item["source"]["git_url"]
    commit = item["source"]["commit_sha"]
    _run(["git", "init", "--quiet", str(dest)])
    _run(["git", "-C", str(dest), "remote", "add", "origin", git_url])
    _run(["git", "-C", str(dest), "fetch", "--depth=1", "origin", commit])
    _run(["git", "-C", str(dest), "checkout", "--detach", "FETCH_HEAD"])
    head = _run(["git", "-C", str(dest), "rev-parse", "HEAD"])
    if head != commit:
        raise RegistryError("downloaded git commit differs from registry pin")


def _verify_upstream_manifest(source: Path, item: dict) -> None:
    data = _read_json(source / item["source"]["module_manifest"])
    required = {
        "module_id": item["id"],
        "module_version": item["version"],
        "non_actuating": True,
        "automatic_motion_on_load": False,
        "active_calibration_supported": False,
        "registry_status": "proposal-only",
    }
    for key, expected in required.items():
        if data.get(key) != expected:
            raise RegistryError(f"upstream module manifest mismatch: {key}")
    if set(data.get("capabilities", [])) != set(item["capabilities"]):
        raise RegistryError("upstream declared capabilities differ from registry")
    if not (source / "Cargo.lock").is_file():
        raise RegistryError("source is missing Cargo.lock")


def _host() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        machine = "x86_64"
    return f"{sys.platform}-{machine}"


def _install_metadata(destination: Path) -> dict:
    metadata_file = destination / "installed.json"
    if metadata_file.is_symlink() or not metadata_file.is_file():
        raise RegistryError("installation metadata missing or unsafe")
    return _read_json(metadata_file)


def install(
    module_id: str,
    *,
    version: str | None = None,
    prefix: Path | None = None,
    build: bool = False,
) -> dict:
    item = entry(module_id, version)
    if build and _host() not in item["distribution"]["hosts"]:
        raise RegistryError(f"unsupported build host: {_host()}")
    home = (prefix or default_prefix()).expanduser().resolve()
    final = _install_location(item, home)
    if final.exists() or final.is_symlink():
        raise RegistryError("module already installed; no overwrites or implicit upgrades")
    final.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".rgd-install-", dir=final.parent) as temporary:
        stage = Path(temporary)
        src = stage / "source"
        _fetch_pinned(item, src)
        _verify_upstream_manifest(src, item)
        binary_name = item["distribution"]["binary"]
        binary_sha = None
        if build:
            # Compiling Rust dependencies may execute build scripts. The Console
            # MUST require a user action before passing build=True.
            _run(
                ["cargo", "build", "--release", "--locked", "--bin", binary_name],
                cwd=src,
            )
            output_binary = src / "target" / "release" / binary_name
            if not output_binary.is_file() or output_binary.is_symlink():
                raise RegistryError("cargo finished but expected executable is missing")
            (stage / "bin").mkdir()
            copy = stage / "bin" / binary_name
            shutil.copy2(output_binary, copy)
            copy.chmod(copy.stat().st_mode | 0o100)
            binary_sha = _digest(copy)
            shutil.rmtree(src / "target", ignore_errors=True)
        # Git metadata and remote origin are not needed for offline installed use.
        shutil.rmtree(src / ".git", ignore_errors=True)
        status = "ready" if build else "source_only"
        info = {
            "registry_contract": CONTRACT_VERSION,
            "module_id": item["id"],
            "version": item["version"],
            "source_commit": item["source"]["commit_sha"],
            "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": status,
            "binary_sha256": binary_sha,
        }
        (stage / "installed.json").write_text(
            json.dumps(info, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if final.exists() or final.is_symlink():
            raise RegistryError("destination appeared during installation")
        stage.rename(final)
    return status_of(module_id, version=item["version"], prefix=home)


def status_of(
    module_id: str, *, version: str | None = None, prefix: Path | None = None
) -> dict:
    item = entry(module_id, version)
    home = (prefix or default_prefix()).expanduser().resolve()
    final = _install_location(item, home)
    if final.is_symlink():
        return {"module_id": item["id"], "version": item["version"], "status": "tampered"}
    if not final.is_dir():
        return {"module_id": item["id"], "version": item["version"], "status": "not_installed"}
    try:
        recorded = _install_metadata(final)
        if any(
            recorded.get(k) != v
            for k, v in {
                "module_id": item["id"],
                "version": item["version"],
                "source_commit": item["source"]["commit_sha"],
                "registry_contract": CONTRACT_VERSION,
            }.items()
        ):
            raise RegistryError("installed metadata disagrees with catalog")
        mode = recorded.get("status")
        if mode == "ready":
            executable = final / "bin" / item["distribution"]["binary"]
            if executable.is_symlink() or not executable.is_file():
                raise RegistryError("binary is missing or a symbolic link")
            if _digest(executable) != recorded.get("binary_sha256"):
                raise RegistryError("installed executable integrity check failed")
            return {
                "module_id": item["id"],
                "version": item["version"],
                "status": "ready",
                "binary": str(executable.resolve()),
                "binary_sha256": recorded["binary_sha256"],
                "source_commit": recorded["source_commit"],
            }
        if mode == "source_only":
            _verify_upstream_manifest(final / "source", item)
            return {"module_id": item["id"], "version": item["version"], "status": "source_only"}
        raise RegistryError("unknown installed state")
    except (RegistryError, OSError, ValueError):
        return {"module_id": item["id"], "version": item["version"], "status": "tampered"}


def resolve_binary(
    module_id: str, *, version: str | None = None, prefix: Path | None = None
) -> dict:
    result = status_of(module_id, version=version, prefix=prefix)
    if result["status"] != "ready":
        raise RegistryError(f"module cannot be launched: {result['status']}")
    # A resolve check is not a sandbox. The Console must use a restricted API.
    return result
