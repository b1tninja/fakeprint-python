# fakeprint-python

Python side of [fakeprint](../fakeprint), the fake WSD / WS-Print printer: an **in-process native
server** (PyO3, mirroring [fakescan-python](../fakescan-python)), a control-plane client, a WS-Print
SOAP client for tests, a typed `PrintTicket` builder, and the WS-Print schema model + reference-doc
generator.

| Part | What it is |
|------|-----------|
| `fakeprint.Server` | The Rust library's own server (`fakeprint::server::serve`, embedded mode) running in this process on `127.0.0.1:<port>`; context manager |
| `fakeprint.PrintClient` (`soap.py`) | WS-Print client: GetPrinterElements, CreatePrintJob, SendDocument (inline or MTOM), GetJobElements, GetActiveJobs, GetJobHistory, CancelJob, SetEventRate, WS-Transfer Get / GetMetadata, WS-Eventing Subscribe/Renew/GetStatus/Unsubscribe; faults become `SoapFault` with `code`/`subcode` |
| `fakeprint.PrintTicket` | Typed ticket builder (job description, copies, sides, media, resolution, MustHonor) |
| `fakeprint.Client` (`client.py`) | HTTP client for a running daemon or embedded server: `/health`, `/v1/status`, `/v1/jobs` (+ `print_jobs`, `/v1/jobs/{id}`), `/v1/reset`, `/v1/profile` (id or inline profile), `/v1/ctl`, `/v1/faults` |
| `fakeprint.wsd_schema`, `wsd_docs`, `services` | Schema model, instance validator, reference-page generator (`fakeprint-refs`) |
| `fakeprint.profiles()` / `fakeprint.start(profile)` | Shipped profile ids; start a server on any of them |
| `src/lib.rs` | The `_native` module (`Server`): `serve()` + in-process calls into the library's own `/v1` router; `_native.pyi` has the stubs |

## Build and test

```powershell
cd D:\code\fakeprint-python
$env:CARGO_TARGET_DIR = "D:/temp/fakeprint-target-py"   # keep cargo output out of the repo
$env:VIRTUAL_ENV = "D:\code\pdfcraft-python\.venv"
maturin develop                                         # builds src/lib.rs against ../fakeprint
python -m pytest                                        # native tests skip when _native is not built
```

The crate depends on `../fakeprint/crates/fakeprint` by path and never edits it. The emulator's
profiles are read from `../fakeprint/profiles` (`FAKEPRINT_PROFILES` or `FAKEPRINT_REPO` override).
What the Rust library lacks is listed in [docs/needs-from-rust.md](docs/needs-from-rust.md).

## Use

```python
import fakeprint
from fakeprint import Server, PrintTicket

with Server() as srv:                                   # loopback only, ephemeral port
    pc = srv.printer()                                  # PrintClient on /WebServices/PrinterService
    pc.get_printer_elements().printer_state             # 'Idle'
    job = pc.create_print_job(PrintTicket(job_name="report", copies=2, sides="OneSided"))
    pc.send_document(job, pdf_bytes, format="application/pdf", mtom=True)
    pc.get_job_status(job).state                        # 'Completed'
    srv.documents(job) == [pdf_bytes]                   # byte-identical capture
    srv.captured(), srv.jobs()                          # inbox rows; job table (active + history)
    srv.validation_problem_count()                      # 0: every reply was checked against the schema

fakeprint.profiles()                                    # ['color-inkjet-duplex', 'generic-wsd', 'label-a4', ...]
with fakeprint.start("label-a4") as srv: ...            # any shipped profile
Server(timing={"processing_base_ms": 500},              # overrides (default: instant); or timing="realistic"
       limits={"max_document": 1 << 20, "history_cap": 10},
       event_rate_secs=1, tick_ms=50, validate_replies=True)
```

Two kinds of fault, deliberately separate:

```python
# 1. The printer's own fault plan (the library's /v1/faults): the server answers like the device would.
srv.faults(paper_out=True)                              # Stopped, jobs held until cleared
srv.faults(document_active=True)                        # SendDocument -> ServerErrorDocumentActive
srv.faults(temporary_error=True)                        # also not_accepting_jobs, internal_error
srv.faults(fail_next_document="Format")                 # next job aborts with DocumentFormatError
srv.raise_condition("CoverOpen"); srv.clear_condition("CoverOpen")
srv.faults(reset=True); srv.faults()                    # {"pending": ..., "active": ...}

# 2. Injection rules (server side, POST /v1/faults "inject"): any SOAP code/subcode, a bare HTTP
#    status, a dropped connection, or a delay, for EVERY client (this package's, pdfcraft's, ...).
srv.inject_fault("SendDocument", subcode="wprt:ServerErrorTemporaryError", reason="busy")
srv.inject_fault("SendDocument", skip=1, count=2, code="Sender", subcode="wprt:ClientErrorInvalidArgs")
srv.inject_fault("*", http_status=503, count=None)      # bare HTTP 503, until clear_faults()
srv.inject_fault("CancelJob", close=True)               # connection dropped without a reply
srv.inject_delay("GetActiveJobs", 500)                  # slow printer (a shorter client timeout raises)
srv.inject({"op": "http://schemas.microsoft.com/windows/2006/08/wdp/print/SendDocument",
            "endpoint": "print_service", "effect": {"type": "pass"}, "delay_ms": 250, "count": None})
srv.pending_faults(); srv.pending_rules(); srv.inject_stats()   # what is armed / hit
srv.clear_faults()                                      # injection rules and the printer fault plan

# 3. Job timing at run time (POST /v1/timing), for documents arriving afterwards.
Server(timing="realistic")                              # preset: instant | realistic | default
srv.set_timing("instant"); srv.timing(processing_base_ms=800); srv.timing()
```

- WS-Discovery, JetDirect `:9100`, the Windows named pipe and the instance lock are never started
  (`ServerConfig::embedded`); non-loopback `host` is refused. Nothing here talks to a real printer.
- Captured documents go to a private temp inbox that `stop()` deletes (pass `inbox=` to keep them).
- Injection rules live in the server (`fakeprint` `docs/injection.md`): the first matching rule decides,
  `skip` lets requests through first, `count` (default 1, `None` = forever) bounds the effect, and a
  faulted `SendDocument` never reaches the job engine. `/health`, `/` and `/v1/*` are never touched.
  `Server.inject`, `Client.inject` and the `inject_*` helpers all post the same rule JSON. The old
  client-side `httpx` transport (`fakeprint.faultnet`) is gone.
- `Server.control(method, path, body)` calls the library's own `/v1` router in-process (same JSON as
  over HTTP); `Server.client()` is the same API over loopback HTTP, which also works against the
  daemon.
- `Server.device_cancel(job)` cancels as the panel would (no WS-Eventing event).
- Events: `pc.subscribe(notify_to_url, actions=["JobEndStateEvent"])` against a loopback listener
  (see `tests/test_library_api.py::Recorder`); the default address policy only accepts loopback and
  private `NotifyTo` targets.
- The tests that used to be `xfail` while the library caught up all pass; there are none pinned now.

## Layout

```text
parent/
  fakeprint/          # Rust emulator, owns docs/reference/{schema,upstream}
  fakeprint-python/   # this repo
```

`FAKEPRINT_REPO` overrides the sibling checkout. The Microsoft schema package is only ever **read**
from there (fetch it with `fakeprint/scripts/Get-WsdSpecs.ps1`); this repo contains no Microsoft
files and its `.gitignore` refuses `*.xsd`, `*.wsdl`, `*.zip`, `*.doc*`. The SOAP client and ticket
builder are written from the spec notes, not copied from it.

```powershell
python -m fakeprint.wsd_docs                  # writes into fakeprint\docs\reference\schema (ignored)
python -m fakeprint.wsd_docs --service scan   # same code, scan package
```

## Not done here (compared with fakescan-python)

No `manager`/`registry`/`ctl`/`lab`/`mcp_server` equivalents: fakeprint has a single device per
process and no multi-device daemon API, device registry CRUD or activity feed yet. The clock is not
exposed (tests use short real timeouts).

License: MIT OR Apache-2.0.
