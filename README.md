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

IPP `print-scaling` (PWG 5100.13): profiles advertise it through `print.scaling` (empty list = not advertised, see `minimal-ipp`) and `default_ticket.scaling`; an IPP job's row carries `Job.ipp`, with `Job.print_scaling` (the keyword in effect) and `Job.effective_scale` (the scale the printer would apply to the stored PDF's first page, from its `/MediaBox`, the job's media and the keyword).
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

## Windows reference print client (optional, Windows only)

`fakeprint.winprint` drives the Windows spooler (`winspool.drv`, plus GDI for images) through
`ctypes`, so Windows itself does the WSD/IPP print to a fakeprint printer that a person has already
added in Windows. Use it as a real-Windows oracle in cross-client tests; fakeprint captures what
arrives. It is imported lazily: `import fakeprint` works everywhere, and `fakeprint.winprint` raises
`ImportError` off Windows. `pip install fakeprint[windows]` is only a marker (no extra dependencies).

```python
from fakeprint import winprint
winprint.list_printers()                       # read-only: [PrinterInfo(name, port, driver, status)]
job = winprint.print_raw("fakeprint test", data, doc_name="t", datatype="RAW", user_confirmed=True)
info = winprint.wait_for_job("fakeprint test", job)   # JobInfo, or None once the spooler purged it
winprint.print_image("fakeprint test", rgb, 1700, 2200, user_confirmed=True)  # GDI, 1:1 device pixels
```

Safety, enforced before any spooler call: printing needs an explicit printer name (nothing
enumerates-and-prints); the printer must be one of ours **by WSD device UUID, never by name**: its
port must be a `WSD-` port whose registry `Printer UUID` (read-only, `winprint.wsd_port_uuid`) starts
with `fakeprint.FAKE_UUID_PREFIX` (`00000000-0000-4000-8000-`; test with `fakeprint.is_fake_uuid`,
which accepts any case, braces and `urn:uuid:`). Non-WSD ports (USB, TCPMON, IPP URL ports), foreign
WSD devices and unknown names are refused unless the caller passes `allow_any_printer=True` **and**
the environment has `FAKEPRINT_WIN_ALLOW_ANY=1` (using real hardware is the owner's decision;
harnesses must never set it); and `user_confirmed=True` is required (default `False`; checked first).
Violations raise `WinPrintRefused`; spooler failures raise `WinPrintError(func, code)`. Unit tests
(`tests/test_winprint_unit.py`) mock the DLLs and the registry lookup and print nothing; the one live
test runs only when `FAKEPRINT_WIN_PRINTER` names an installed printer whose WSD UUID is in the fake
namespace. Notes: `print_raw` with datatype `RAW` sends the bytes untouched (the queue/driver
must accept them); `print_image` uses the default DEVMODE and returns no job id.

## Several printers and the device manager

One embedded server can run N printers behind one port; each has its own profile, UUIDs, endpoints,
inbox, jobs, faults, timing, levels and injection rules (docs: `fakeprint/docs/multi-device.md`).

```python
from fakeprint import Server, Device
with Server(devices=[Device("mono", "mono-laser"), Device("color", "color-inkjet-duplex")]) as srv:
    d = srv.devices["mono"]                      # the first device is the default one
    d.uuid; d.ipp_uuid; d.friendly_name; d.profile; d.inbox
    d.device_endpoint; d.service_endpoint; d.ipp_endpoint     # /d/<id>/... (default: legacy paths)
    job = d.printer().print_document(pdf, format="application/pdf")   # PrintClient on /d/mono/...
    d.jobs(); d.documents(job); d.captured(); d.job_ticket(job)
    d.faults(paper_out=True); d.inject_fault("GetPrinterElements"); d.set_levels(Black=15)
    d.set_timing("instant"); d.set_profile("generic-wsd"); d.reset(); d.live()
    srv.devices["color"].client()                # Client scoped to /d/color (faults, timing, levels)
    srv.add_device(Device("late", "generic-wsd")); srv.remove_device("late")
    srv.manager                                  # Manager bound to this server (in-memory state)
Server()                                         # unchanged: one "default" device; srv.printer() etc.
```

`Server(devices=[...])` is mutually exclusive with `device_uuid`/`ipp_uuid`/`name` (those configure the
implicit default device). Jobs, faults, levels and injection never leak between devices.

For a running **daemon** (`fakeprint --manage`, default `127.0.0.1:3921`) there are thin clients that
never write the registry file and never Add anything in Windows:

| Module | What it is |
|--------|------------|
| `fakeprint.Manager` | `devices() device() registry() queue() add() remove(forget=) reconfigure() installed() mark_added() forget() reconcile() reconcile_from_windows() levels() advance() skip() reorder() profiles() open_ui() open_settings() wait_installed() status_table() summary_level() guidance()` over HTTP or `Manager.pipe("name")` (named pipe). Typed errors: `BadRequest` 400, `Forbidden` 403, `UnknownDevice` 404, `Conflict` 409, `NotYet` 501 (also a route the build lacks); all are `ApiError`s. |
| `fakeprint.registry` | read-only `load()/parse()/from_server()` of `%LOCALAPPDATA%\fakeprint\devices.json`, the section 3.4 matching rules (`match_kind`, `match_printers_to_devices`, `reconcile`), capability classes (`capability_class`, `classes`, `select_installed`, `skip_message`) |
| `fakeprint.ui` | `StatusRow`, `summary_level` (green/amber/red like the tray), `add_guidance`, `format_status_table` |
| `fakeprint.lab` / `fakeprint-lab` | `ensure up down instructions wait`: register printers, print the ONE "Add now: ... (n of N)" text, walk the queue with a bound per printer; never Adds, never removes an installed printer |
| `fakeprint.tray` | `ensure_daemon()` attach-first (the tray lives in the daemon), `find_daemon_exe()` |
| `fakeprint.ctl` / `fakeprint-ctl` | `status devices ui profiles add remove queue levels faults reset open-settings ctl` (`--pipe NAME`) |
| `fakeprint.mcp_server` / `fakeprint-mcp` | attach-only MCP: devices, queue guidance, device-scoped faults/levels/timing/jobs/captured documents/reset; no tool adds, removes or installs anything |

Harness rule: pick an installed printer by capability class (`registry.select_installed(devices,
klass)`); when none is installed skip with `registry.skip_message(...)`, which carries the exact Add
instructions. Windows printers are matched by UUID found in the port string (or the device's exact IPP
URL), never by name; a TCPMON `host:9100` port or a port naming two UUIDs is foreign.

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

The activity feed is not exposed and neither is the clock (tests use short real timeouts). Ghost
clean-up of Windows printer objects is out of scope (deferred in the contract). The daemon-side
routes (`/v1/queue`, `/v1/registry`, `/v1/reconcile`, ctl `mark_added` ...) belong to the Rust manager
(M2); against a build without them `Manager` raises `NotYet`.

License: MIT OR Apache-2.0.
