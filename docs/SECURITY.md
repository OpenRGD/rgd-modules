# Module distribution security — Bootstrap v0.1

- **Source pin:** module descriptor references an exact Git object SHA, fetched from an allowlisted OpenRGD HTTPS repository. Installer checks resulting HEAD and the source's own manifest ID, version, safety declarations and capabilities; Cargo.lock is required.
- **Per-user isolation:** write only under a selected data prefix (default XDG). Do not overwrite existing version directories or follow manifest paths outside the registry.
- **No auto-execution:** catalog, status and resolve are read-only. Install without `--build` only stages source. Install with `--build` requires an explicit operator action and may run Rust build scripts.
- **Tampering checks:** recorded source-tree and executable digests help detect local changes. SHA-256 alone is not a signature or independent supply-chain attestation.
- **No hardware rights:** no motor capability, driver, privileged service, robot motion, camera access or homing is granted.
- **Untrusted runtime risk:** Cargo dependencies are downloaded and compiled on first build; before broader product release, define signed binaries, trust roots, rollback, permission policy and supply-chain attestations.
- **Status semantics:** installed binaries must pass local digest verification before the Console accepts them as ready. The Console must still apply interface-specific permissions and strict argv allowlists.

CI uses synthetic unit fixtures and a pinned-source installation smoke test (where CI networking permits). Test data are not measurements from a physical robot.
