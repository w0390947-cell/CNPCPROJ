# Synthetic demonstration capability

Owner: 仿真研究维护负责人; reviewer: 设备控制维护负责人.

Public boundaries: `contracts` (versioned input/output and injected numerical/job ports),
`api` (Fleet construction and synchronized use cases). `domain` owns explicit-time
device dynamics, SOC and command lifecycle. HTTP is an injected transport adapter.
This capability has no physical device transports. Legacy AC and optimization
implementations are supplied by the registered composition adapters.

`GET /api/demo/availability` exposes the fixed 24-hour, 15-minute renewable
capability series through the injected `NetworkPort`; it does not reproduce
profile allocation rules in the HTTP or Web adapters. The response is explicitly
marked as synthetic bundle data. Current telemetry remains authoritative for
fault-adjusted availability and actual output.

Recovery authorization belongs to `domain.Fleet`. Two safe cycles and `rearm`
clear the network interlock but leave each device awaiting a fresh command.
Only a newly accepted command submitted after rearm releases its own device;
idempotent replay, rejection and commands accepted while blocked do not. Held
devices cannot restore higher historical/default P or change Q. Availability,
energy and P/Q/S limits remain authoritative, including physical reductions.
This corrects the documented behavior without changing wire schemas or receipt
history. A subsequent fault/rearm requires fresh authorization again.

`application.verify_demo` records real ACK timeout commands and actual output,
and verifies post-rearm holds and independent device release. Regression tests
are in `tests/demo/test_recovery_authorization.py`; the full verification CLI
also runs the four real optimization jobs through the existing injected port.
