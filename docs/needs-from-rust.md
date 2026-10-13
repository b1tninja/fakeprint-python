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
