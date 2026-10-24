# RFC — IMU observation module for OpenRGD Console (2026-10-24)

**Status: DESIGN ONLY / NOT INSTALLABLE.** This is a proposal for module ownership, descriptor evolution and permissions. It does not register an implementation, create a canonical RGD sensor schema, change installed Calibration, grant access to /dev, or authorize motion.

## Why a generic module
The initial Paky-01 torso sensor produces checksum-valid WIT/JY901 serial frames over CH340 USB via a powered hub. The physical driver is machine-specific, but IMU observation types (accelerometer SI XYZ, gyro rad/s XYZ, orientation with explicit source frame, provenance, clock quality, validity and freshness) should be hardware-agnostic.

## Ownership
- Proposed future source repo: `OpenRGD/rgd-imu` (NOT YET CREATED). It would publish a bounded, read-only sensor observation contract, normalization and health diagnostics independent of Hiwonder/WIT hardware.
- `italiarobotica/paky-01`: actual USB driver, USB physical path, mount metadata and calibration references. Driver currently implemented as `scripts/imu_publish.py` in feature branch `feat/imu-console-bridge`, using the existing WIT decoder.
- Console local source v0.4D: candidate v0.5 read-only HTTP ingress + authenticated local sender, WebSocket fan-out and /live IMU panel. Until a generic module is published and installable, the UI bridge is *bundled*, not a dynamically loaded module.
- `OpenRGD/rgd-calibration`: pending sensor-to-torso transform and calibration revision, not fabricated in ingest.
- OpenRGD Canonical Core: no new fields or authority are asserted by this RFC.

## Candidate capabilities and permissions (subject to explicit review)
Future module ID `org.openrgd.imu` (candidate, not registered):
- Capabilities: `sensor.imu.observe`, `sensor.imu.health.inspect`, `sensor.imu.stream.read`.
- Permissions for a host driver: narrowly scoped read of an explicitly selected USB-serial device and send to loopback observation ingress; **never** motor/CAN write, firmware flash, root commands or `safety.override`.
- Trust: source and host receipt clocks distinct; frames explicitly in IMU sensor axes; heading uncalibrated; `decision_eligible=false` until independently validated geometry, calibration, synchronization, freshness and safety policies exist.

## Registry blocker
Current `registry/schemas/module.v0.1.schema.json` only supports pinned Git/Cargo distribution and `command_line_json` `inspect`; it does NOT describe a long-running, device-attached, Python host publisher or browser-rendered plugin. We must not publish a fake `0.1.0.json` in `registry/index.json` before its source repo, executable, permissions and deployment lifecycle exist and are tested. A future candidate registry v0.2 should define **host_driver** vs **passive_library** vs **ui_panel**, required user approval, per-robot grants, explicit device identity, reboot/restart lifecycle, revocation and source/attestation checks.

## Initial protocol candidate
Host pushes `openrgd.imu-sample.v0.1-candidate` to a Console **loopback-only**, token-protected endpoint. Console emits `console.imu.v0.1-candidate` via its existing WebSocket, keeps only latest per physical robot in memory, expires after 3 seconds, and does not change `console.unbound.v1` status. This is Console transport, **not** a Chronon, world model, Canonical RGD sensor record or motion authority.

## Sensor-health policy caution
`OpenRGD/openrgd/spec/01_foundation/sensor_health_monitor.jsonc` declares torso_imu `no_data_timeout_s_float: 0.1`. The currently observed device emits around 10Hz (period roughly 100ms) with host/USB jitter. Therefore a literal 100ms failure cutoff would risk false faults. Do NOT turn this declarative value into a runtime stop or balance command until actual sample cadence, latency and timeout budget are measured and the safety gate independently validated.

## Promotion gates
1. Physical device and sensor frame confirmed; calibrated mount transform and traceable evidence.
2. Generic module repo and manifest published, with pinned release, installable and reviewable permissions, no default auto-start.
3. Read-only physical observation tests, stale/replay checks, disconnection and clock-jump behavior.
4. Optional Cognition/Somatic producer consumes only validated state-estimator output, never raw IMU packets as direct actuation instructions.
