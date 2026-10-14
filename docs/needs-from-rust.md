# What the Python binding still needs from the Rust crate

The binding (`src/lib.rs`) is a thin owner of `fakeprint::server::serve(ServerConfig::embedded(..))`;
it depends on `../fakeprint/crates/fakeprint` as a read-only path dependency and never edits it.
Closed by the library, and therefore removed from this file: a reusable server entry point, fault
injection (now server-side rules in `POST /v1/faults`, replacing the old Python `httpx` transport),
a runtime timing API (`/v1/timing`, presets `instant`/`realistic`/`default`), 400 on an unknown
`fail_next_document`, job table, ticket/format/compression checks, 413 on big bodies, escaping,
side effects on disk, event observability.

## Open items

1. **The stored print ticket in the control plane.** `GET /v1/jobs` (`print_jobs[]`) and
   `GET /v1/jobs/{id}` carry name, user, state, reasons, documents and counters, but not the ticket
   the job was created with (copies, sides, orientation, media). The protocol has it
   (`GetJobElements(PrintTicket)`, exposed here as `Server.job_ticket(id)`), so nothing is blocked;
   a `"ticket": {...}` member on the job row would let a test read it without a SOAP round trip and
   would also show what a foreign client (for example pdfcraft's) really sent.
2. **`.meta.json` next to a capture** holds only bytes/format/ids/peer; the job name, user and ticket
   would make the inbox self-describing. Optional.

No real server bug is known at the moment.

- Driving the clock from Python (`ManualClock`, already in the crate, via `ServerConfig.clock`) is
  possible from the binding side and not exposed yet: the tests use real short timeouts
  (`document_timeout_ms=300`, `tick_ms=50`). Not a Rust request.

## Windows oracle (not a Rust need)

`python/fakeprint/winprint.py` is a Python-only optional client over `winspool.drv`; it needs nothing
from the Rust crate. It requires a human-installed Windows printer whose WSD device UUID starts with the fake prefix (`00000000-0000-4000-8000-`; names are not
used) pointing at a
running fakeprint server (the WSD/IPP add-printer step is manual and was not automated or verified).

## Multi-device and the manager (WP-P, against docs/multi-device.md in fakeprint)

What the Python side assumes from the Rust manager (M1 / M2), each item verified only when the matching
test in tests/test_multi_device_embedded.py runs un-skipped:

- M1: `ServerConfig::with_devices`, `ServerHandle::{devices, router, manager}` and `DeviceManager::world_of`
  (the binding uses them), `GET/POST /v1/devices`, `DELETE /v1/devices/{id}[?forget=true]`,
  `/d/{id}/v1/*` (status, jobs, faults, timing, profile, reset, **levels**), 404 `{"error":"unknown device"}`.
- M2 (Manager only; unit-tested against a fake daemon, `NotYet` when a route is missing):
  `POST /v1/devices/{id}/{reconfigure,installed,forget}`, `GET /v1/registry`, `POST /v1/reconcile`
  (`{printers:[{name,port,driver,id}]}` -> `{installed,stale,foreign[,offline]}`), `GET/POST /v1/queue`,
  `POST /v1/queue/{advance,skip}`, `GET /v1/profiles`, and ctl commands `devices device_add device_remove
  reconfigure installed mark_added forget reconcile levels queue queue_advance queue_skip queue_reorder
  registry profiles open_settings` on `POST /v1/ctl` and the named pipe. Assumed argument names: `device`
  selects the printer (4.7); `device_add` takes the POST /v1/devices body; `queue_reorder` takes `{order}`;
  `installed` takes `{device, windows:{...}, kind?}`; `mark_added` takes `{device}`.
- Open: no route is specified for `mark_added` (legacy `added.pnp`); the Python uses ctl `mark_added`.
- Nice to have: `DeviceInfo.config.class` is read when present and otherwise derived from the first 12 hex
  digits of `published_fingerprint`.
