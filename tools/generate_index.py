"""Maintainer-only deterministic projection of reviewed v0.1 release descriptors.

No downloads, installation, publication, runtime selection or authority. Run from
a clean reviewed source checkout; this is not a verifier for a hostile filesystem.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from jsonschema import Draft202012Validator
from rgd_registry import core

ROOT = Path(__file__).resolve().parents[1] / "registry"
MAX_BYTES = 65536
MAX_ENTRIES = 1024
MAX_INDEX_BYTES = 524288

class IndexError(ValueError):
    """Catalog maintenance validation failure."""

def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IndexError("duplicate JSON key")
        result[key] = value
    return result

def _constant(_):
    raise IndexError("nonfinite JSON")

def _integer(value):
    if len(value) > 32:
        raise IndexError("numeric limit")
    return int(value)

def _read(path, limit=MAX_BYTES):
    if path.is_symlink() or not path.is_file():
        raise IndexError("expected regular non-symlink file")
    with path.open("rb") as f:
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise IndexError("file size limit")
    return raw

def _json(path):
    try:
        return json.loads(_read(path).decode("utf-8"), object_pairs_hook=_object,
                          parse_constant=_constant, parse_int=_integer)
    except (UnicodeError, RecursionError, json.JSONDecodeError, OSError):
        raise IndexError("invalid descriptor/schema JSON") from None

def _children(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise IndexError("expected non-symlink directory")
    items = []
    for path in directory.iterdir():
        if len(items) >= MAX_ENTRIES:
            raise IndexError("directory entry limit")
        items.append(path)
    return sorted(items, key=lambda p:p.name)

def _validator(path):
    schema = _json(path)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)

def generate(root=ROOT):
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or (root/"schemas").is_symlink():
        raise IndexError("invalid registry directory")
    descriptor_validator = _validator(root/"schemas/module.v0.1.schema.json")
    index_validator = _validator(root/"schemas/index.v0.1.schema.json")
    rows, seen = [], set()
    for folder in _children(root/"modules"):
        # Every descriptor is at modules/<module-id>/<version>.json. Do not
        # accept aliases, extra files, path escapes, or silently omitted entries.
        if folder.is_symlink() or not folder.is_dir():
            raise IndexError("unexpected modules entry")
        files = _children(folder)
        if not files:
            raise IndexError("empty module directory")
        for path in files:
            if len(rows) >= MAX_ENTRIES:
                raise IndexError("catalog entry limit")
            if path.suffix != ".json":
                raise IndexError("unexpected descriptor file")
            descriptor = _json(path)
            if not descriptor_validator.is_valid(descriptor):
                raise IndexError("descriptor schema violation")
            try:
                core._validate_descriptor(descriptor)
            except core.RegistryError:
                raise IndexError("descriptor incompatible with existing v0.1 engine") from None
            identity = (descriptor["id"],descriptor["version"])
            if folder.name != identity[0] or path.name != identity[1]+".json":
                raise IndexError("descriptor path/identity mismatch")
            if identity in seen:
                raise IndexError("duplicate module release")
            seen.add(identity)
            rows.append({"id":identity[0],"version":identity[1],
                         "path":path.relative_to(root).as_posix()})
    # Lexical ordering is only deterministic serialization, NOT release selection.
    rows.sort(key=lambda row:(row["id"],row["version"]))
    value={"registry_schema_version":core.CONTRACT_VERSION,"modules":rows}
    index_validator.validate(value)
    result=(json.dumps(value,indent=2,ensure_ascii=True)+"\n").encode("utf-8")
    if len(result)>MAX_INDEX_BYTES:
        raise IndexError("index size limit")
    return result

def check(root=ROOT):
    root=Path(root)
    expected=generate(root)
    if _read(root/"index.json", MAX_INDEX_BYTES)!=expected:
        raise IndexError("index drift: regenerate from reviewed descriptors")
    return expected

def write(root=ROOT):
    root=Path(root)
    # Complete validation before touching the old index.
    result=generate(root)
    target=root/"index.json"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise IndexError("invalid index target")
    mode=stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
    fd, tmp=tempfile.mkstemp(prefix=".index-",dir=root)
    try:
        with os.fdopen(fd,"wb") as stream:
            stream.write(result)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp,mode)
        os.replace(tmp,target)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return result

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    group=ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--check",action="store_true",help="Check exact index bytes")
    group.add_argument("--write",action="store_true",help="Regenerate local index atomically; not publication")
    args=ap.parse_args(argv)
    try:
        if args.check:
            check()
        else:
            write()
    except (IndexError, OSError) as exc:
        print(f"INDEX_REJECTED: {exc}",file=sys.stderr)
        return 2
    print("INDEX_CONSISTENT" if args.check else "INDEX_GENERATED")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
