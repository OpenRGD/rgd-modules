# OpenRGD Module Registry

**Candidate v0.1** — optional OpenRGD software module discovery and explicit installation. This distribution catalog is **not** a new Canonical RGD domain, a Body Adapter registry, or the source of motion/safety authority.

## Available module

| Module | Version | Source | Install support |
| --- | --- | --- | --- |
| `org.openrgd.calibration` | `0.2.0` | [OpenRGD/rgd-calibration](https://github.com/OpenRGD/rgd-calibration) | Pinned source + optional Cargo build, Linux x86_64 |

The published descriptor is pinned to commit `81700401e2fa6d370a86b6c1f5a48802bc5f1fc7` (CAL-0.2 merged). Not a floating GitHub branch, GitHub Release, prebuilt binary or signed package.

## Maintaining the official catalog (candidate)

Reviewed v0.1 release descriptors under `registry/modules/<module-id>/<version>.json` are the authoritative inputs of the **generated** index. Add a reviewed descriptor once; do not maintain a second list in Console or RGD-OS.

For maintainers only, using the test dependency:

```bash
python3 -m pip install jsonschema==4.23.0
python3 -m tools.generate_index --write
python3 -m tools.generate_index --check
```

The tool validates the existing descriptor/index schemas and registry checks, rejects malformed layouts, and writes only the local index after successful validation. CI requires exact index reproduction. It does not approve, fetch or install a release, verify publisher trust, or grant any permission. Submit both the descriptor and generated index through review; no automatic publication. Existing v0.1 installs still use the pinned snapshot. A future manifest-to-release generator and authenticated snapshot protocol need separate reviewed changes.

## Offline discovery (no install / no execution)

```bash
python3 -m rgd_registry catalog
python3 -m rgd_registry show org.openrgd.calibration
python3 -m rgd_registry status org.openrgd.calibration
```

Registry is a versioned, local JSON index:

- `registry/index.json` — module references
- `registry/modules/org.openrgd.calibration/0.2.0.json` — version, source pin, capabilities, permissions, installation strategy
- `registry/schemas/module.v0.1.schema.json` — descriptor contract (candidate)

These operations do **not** download, compile or execute modules.

## Opt-in installation on Ubuntu / Fedora (x86_64)

Requires Python 3.10+, Git and (for `--build`) Rust/Cargo plus network access during the initial installation. No root/sudo required.

```bash
# SOURCE ONLY: pulls checked pinned source without compilation.
python3 -m rgd_registry install org.openrgd.calibration

# Or build an executable by explicitly accepting Cargo compilation:
python3 -m rgd_registry install org.openrgd.calibration --build

# Verify the installed binary SHA-256 and retrieve its absolute path:
python3 -m rgd_registry resolve org.openrgd.calibration
```

Choose **one** of the two installation forms; another `install` never overwrites an existing version. Source-only is not executable (state `source_only`). The `--build` mode can run Rust dependency build scripts: Console must request user approval. All git/cargo subprocesses are invoked with structured argument arrays and `shell=False`. The fetched Git commit, upstream module manifest and Cargo.lock are validated.

The local data root defaults to `$XDG_DATA_HOME/openrgd` or `~/.local/share/openrgd`, with versions in `modules/<module-id>/<version>/`. Override with `--prefix PATH`.

## Using Calibration

After a ready installation, invoke the resolved `rgd-calibration` binary with `inspect --bundle /path/to/openrgd`. It only produces JSON reports from local RGD files: it cannot access motors or prove that a physical robot is calibrated.

## Important limitations

- The registry protocol and Calibration module descriptor are **candidate** and do not establish any accepted Canonical Core contract.
- [OpenRGD Console](https://github.com/OpenRGD/rgd-console) v0.4.3 consumes a pinned registry snapshot through its Module Agent; catalog refresh/publication and general lifecycle support are separate future work.
- No GitHub Release or signed prebuilt binaries; installation compiles a pinned source commit, not an attested artifact.
- No auto-install, updates, auto-execution or motor capabilities. Future high-risk modules need a separate permission/sandbox policy.
- Installing a module never changes an RGD standard/profile or certifies real observations.

See [Console integration](docs/CONSOLE_INTEGRATION.md) and [security model](docs/SECURITY.md).
