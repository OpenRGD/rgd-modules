# OpenRGD Console ↔ Module Registry

**Candidate integration boundary, 2026-10-21.** The Console repository `OpenRGD/openrgd-console` has not yet been created; this describes an implementable interface, not a completed Console frontend.

The planned separation is:

- **OpenRGD Canonical Core:** definition and governance of robot model; not touched by software module installs
- **OpenRGD/rgd-modules:** distribution catalog, version pins, install checks
- **OpenRGD/rgd-calibration:** passive Rust calibration library and CLI
- **OpenRGD Console:** user UI and informed installation approval
- **Node Agent (planned):** local worker invoking allowlisted module installer commands without shell concatenation

### Discovery and installation sequence

1. Read the index and descriptor JSON locally, or execute `python3 -m rgd_registry catalog`. No implicit internet access.
2. Display exact module id/version, requested capabilities/permissions, `candidate` maturity, pinned source commit, host/CPU requirements.
3. Before a build, ask the operator to explicitly authorize downloading reviewed OpenRGD source and running Rust/Cargo, whose dependencies may execute build scripts. Never install on Console start, file import, or device attach.
4. The future Node Agent calls a fixed argv array:

   `["python3","-m","rgd_registry","install","org.openrgd.calibration","--version","0.2.0","--build"]`

5. The agent runs `status` and `resolve`, accepts only `status:"ready"` after SHA-256 integrity checks, then registers the absolute binary path locally. Missing Git/Cargo, unsupported OS, network error, mismatch or tampering => fail closed.
6. On an explicit Inspect action, the Console invokes only `rgd-calibration inspect --bundle /some/local/openrgd` (plus supported typed options), and renders JSON results as nominal/declaration/historical claims, never as physically certified observations.

### Capabilities and interfaces

Seven declared capabilities include profile validation, sealing, persistence, identity inspection, passive inspection, coverage and read-only delta preview. The **current CLI** exposes only `inspect`; the other operations are Rust library API and must not be advertised as separate CLI commands.

### Zero-motion and non-silent semantics

The registry must never grant `motor.write`, `safety.override` or implicit active calibration. A registry status of `ready` proves local software readiness, not that any robot is physically calibrated, homed, in a known pose or safe to move. Console module installation must remain orthogonal to loading an RGD, physical binding and Somatic Runtime authorization.

The RGD-standard concept of modules inside `00_core` is a separate semantic structure from this software distribution registry.
