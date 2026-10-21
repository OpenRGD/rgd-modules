"""JSON-only command interface for the future OpenRGD Console and operators."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import RegistryError, catalog, entry, install, resolve_binary, status_of


def _parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="rgd-module-registry",
        description="Candidate OpenRGD module catalog and opt-in source installer",
    )
    subs = root.add_subparsers(dest="command", required=True)
    subs.add_parser("catalog", help="List registered versions without network access")
    show = subs.add_parser("show", help="Show one module descriptor")
    status = subs.add_parser("status", help="Inspect installed state without executing modules")
    resolve = subs.add_parser("resolve", help="Verify and return a ready module binary path")
    setup = subs.add_parser("install", help="Install pinned source or explicitly compile executable")
    for cmd in (show, status, resolve, setup):
        cmd.add_argument("module_id", help="Exact registry module id")
        cmd.add_argument("--version", help="Pinned semver (optional for unique entries)")
    for cmd in (status, resolve, setup):
        cmd.add_argument("--prefix", type=Path, help="OpenRGD data root (default XDG_DATA_HOME/openrgd)")
    setup.add_argument(
        "--build",
        action="store_true",
        help="Explicitly authorize Cargo build (compilation may run dependency build scripts)",
    )
    return root


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "catalog":
            result = {
                "registry_contract": "0.1.0-candidate",
                "modules": catalog(),
            }
        elif args.command == "show":
            result = entry(args.module_id, args.version)
        elif args.command == "status":
            result = status_of(args.module_id, version=args.version, prefix=args.prefix)
        elif args.command == "resolve":
            result = resolve_binary(args.module_id, version=args.version, prefix=args.prefix)
        elif args.command == "install":
            result = install(
                args.module_id,
                version=args.version,
                prefix=args.prefix,
                build=args.build,
            )
        else:
            raise RegistryError("unsupported registry command")
    except RegistryError as exc:
        print(json.dumps({"error": str(exc), "ok": False}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
