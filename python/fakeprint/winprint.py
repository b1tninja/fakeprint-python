"""Optional Windows-only reference print client over the Windows spooler (``winspool.drv``).

Purpose: a real-Windows oracle for cross-client tests. A human has already added a fakeprint printer
in Windows (Add printer -> WSD / IPP); this module hands Windows a document and Windows itself does
the WSD/IPP print to it, while fakeprint captures what arrives. It is not a general printing library.

Safety (hard rules, enforced before any spooler call and covered by tests/test_winprint_unit.py):

* printing needs an explicit printer NAME; nothing here enumerates-and-prints;
* the name must start with ``fakeprint`` (case-insensitive) unless the caller passes
  ``allow_any_printer=True`` AND the environment has ``FAKEPRINT_WIN_ALLOW_ANY=1``. Using real
  hardware is the owner's decision; test harnesses must never set that variable;
* printing needs ``user_confirmed=True`` (default False), as in ``pdfcraft.wsdprint``;
* ``list_printers`` / ``job_status`` / ``wait_for_job`` only read.

Importing this module off Windows raises ``ImportError``; ``import fakeprint`` is unaffected.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass

if sys.platform != "win32":  # pragma: no cover - exercised only off Windows
    raise ImportError("fakeprint.winprint needs Windows (it drives the Windows spooler, winspool.drv)")

__all__ = [
    "ALLOW_ANY_ENV",
    "REQUIRED_PREFIX",
    "JobInfo",
    "PrinterInfo",
    "WinPrintError",
    "WinPrintRefused",
    "job_status",
    "list_printers",
    "print_image",
    "print_raw",
    "wait_for_job",
]

REQUIRED_PREFIX = "fakeprint"
ALLOW_ANY_ENV = "FAKEPRINT_WIN_ALLOW_ANY"

PRINTER_ENUM_LOCAL = 0x2
PRINTER_ENUM_CONNECTIONS = 0x4
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_INVALID_PARAMETER = 87

JOB_STATUS_PAUSED = 0x1
JOB_STATUS_ERROR = 0x2
JOB_STATUS_DELETING = 0x4
JOB_STATUS_SPOOLING = 0x8
JOB_STATUS_PRINTING = 0x10
JOB_STATUS_OFFLINE = 0x20
JOB_STATUS_PAPEROUT = 0x40
JOB_STATUS_PRINTED = 0x80
JOB_STATUS_DELETED = 0x100
JOB_STATUS_BLOCKED_DEVQ = 0x200
JOB_STATUS_USER_INTERVENTION = 0x400
JOB_STATUS_RESTART = 0x800
JOB_STATUS_COMPLETE = 0x1000
JOB_STATUS_RETAINED = 0x2000

_JOB_FLAGS = {
    JOB_STATUS_PAUSED: "paused",
    JOB_STATUS_ERROR: "error",
    JOB_STATUS_DELETING: "deleting",
    JOB_STATUS_SPOOLING: "spooling",
    JOB_STATUS_PRINTING: "printing",
    JOB_STATUS_OFFLINE: "offline",
    JOB_STATUS_PAPEROUT: "paperout",
    JOB_STATUS_PRINTED: "printed",
    JOB_STATUS_DELETED: "deleted",
    JOB_STATUS_BLOCKED_DEVQ: "blocked",
    JOB_STATUS_USER_INTERVENTION: "user-intervention",
    JOB_STATUS_RESTART: "restart",
    JOB_STATUS_COMPLETE: "complete",
    JOB_STATUS_RETAINED: "retained",
}
_FAILED = JOB_STATUS_ERROR | JOB_STATUS_DELETING | JOB_STATUS_DELETED | JOB_STATUS_OFFLINE | JOB_STATUS_PAPEROUT
_DONE = JOB_STATUS_PRINTED | JOB_STATUS_COMPLETE


class WinPrintError(OSError):
    """A spooler / GDI call failed. ``func`` names the call, ``code`` is GetLastError()."""

    def __init__(self, func: str, code: int, message: str = "") -> None:
        self.func = func
        self.code = code
        self.detail = message
        super().__init__(code, f"{func} failed (error {code}): {message}")


class WinPrintRefused(PermissionError):
    """A safety guard refused the print before any spooler call was made."""


# --- structures -----------------------------------------------------------------------------------


class DOC_INFO_1W(ctypes.Structure):
    _fields_ = [
        ("pDocName", wintypes.LPWSTR),
        ("pOutputFile", wintypes.LPWSTR),
        ("pDatatype", wintypes.LPWSTR),
    ]


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [
        ("wYear", wintypes.WORD),
        ("wMonth", wintypes.WORD),
        ("wDayOfWeek", wintypes.WORD),
        ("wDay", wintypes.WORD),
        ("wHour", wintypes.WORD),
        ("wMinute", wintypes.WORD),
        ("wSecond", wintypes.WORD),
        ("wMilliseconds", wintypes.WORD),
    ]


class PRINTER_INFO_2W(ctypes.Structure):
    _fields_ = [
        ("pServerName", wintypes.LPWSTR),
        ("pPrinterName", wintypes.LPWSTR),
        ("pShareName", wintypes.LPWSTR),
        ("pPortName", wintypes.LPWSTR),
        ("pDriverName", wintypes.LPWSTR),
        ("pComment", wintypes.LPWSTR),
        ("pLocation", wintypes.LPWSTR),
        ("pDevMode", ctypes.c_void_p),
        ("pSepFile", wintypes.LPWSTR),
        ("pPrintProcessor", wintypes.LPWSTR),
        ("pDatatype", wintypes.LPWSTR),
        ("pParameters", wintypes.LPWSTR),
        ("pSecurityDescriptor", ctypes.c_void_p),
        ("Attributes", wintypes.DWORD),
        ("Priority", wintypes.DWORD),
        ("DefaultPriority", wintypes.DWORD),
        ("StartTime", wintypes.DWORD),
        ("UntilTime", wintypes.DWORD),
        ("Status", wintypes.DWORD),
        ("cJobs", wintypes.DWORD),
        ("AveragePPM", wintypes.DWORD),
    ]


class JOB_INFO_1W(ctypes.Structure):
    _fields_ = [
        ("JobId", wintypes.DWORD),
        ("pPrinterName", wintypes.LPWSTR),
        ("pMachineName", wintypes.LPWSTR),
        ("pUserName", wintypes.LPWSTR),
        ("pDocument", wintypes.LPWSTR),
        ("pDatatype", wintypes.LPWSTR),
        ("pStatus", wintypes.LPWSTR),
        ("Status", wintypes.DWORD),
        ("Priority", wintypes.DWORD),
        ("Position", wintypes.DWORD),
        ("TotalPages", wintypes.DWORD),
        ("PagesPrinted", wintypes.DWORD),
        ("Submitted", SYSTEMTIME),
    ]


class DOCINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_int),
        ("lpszDocName", wintypes.LPCWSTR),
        ("lpszOutput", wintypes.LPCWSTR),
        ("lpszDatatype", wintypes.LPCWSTR),
        ("fwType", wintypes.DWORD),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 1)]


# --- DLL layer (replaceable in tests) -------------------------------------------------------------

_P = ctypes.POINTER


def _sig(dll: object, name: str, restype: object, *argtypes: object) -> None:
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)


def _declare_winspool(dll: object) -> object:
    H = wintypes.HANDLE
    _sig(dll, "OpenPrinterW", wintypes.BOOL, wintypes.LPWSTR, _P(wintypes.HANDLE), ctypes.c_void_p)
    _sig(dll, "ClosePrinter", wintypes.BOOL, H)
    _sig(dll, "StartDocPrinterW", wintypes.DWORD, H, wintypes.DWORD, _P(DOC_INFO_1W))
    _sig(dll, "EndDocPrinter", wintypes.BOOL, H)
    _sig(dll, "StartPagePrinter", wintypes.BOOL, H)
    _sig(dll, "EndPagePrinter", wintypes.BOOL, H)
    _sig(dll, "WritePrinter", wintypes.BOOL, H, ctypes.c_void_p, wintypes.DWORD, _P(wintypes.DWORD))
    _sig(
        dll,
        "EnumPrintersW",
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        _P(wintypes.DWORD),
        _P(wintypes.DWORD),
    )
    _sig(
        dll,
        "GetJobW",
        wintypes.BOOL,
        H,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        _P(wintypes.DWORD),
    )
    return dll


def _declare_gdi(dll: object) -> object:
    D = wintypes.HDC
    _sig(dll, "CreateDCW", D, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p)
    _sig(dll, "DeleteDC", wintypes.BOOL, D)
    _sig(dll, "StartDocW", ctypes.c_int, D, _P(DOCINFOW))
    _sig(dll, "EndDoc", ctypes.c_int, D)
    _sig(dll, "AbortDoc", ctypes.c_int, D)
    _sig(dll, "StartPage", ctypes.c_int, D)
    _sig(dll, "EndPage", ctypes.c_int, D)
    _sig(
        dll,
        "StretchDIBits",
        ctypes.c_int,
        D,
        *([ctypes.c_int] * 8),
        ctypes.c_void_p,
        _P(BITMAPINFO),
        wintypes.UINT,
        wintypes.DWORD,
    )
    return dll


# Tests replace these with fakes; production loads the real DLLs lazily on first use.
_winspool: object | None = None
_gdi: object | None = None


def _spool() -> object:
    global _winspool
    if _winspool is None:
        _winspool = _declare_winspool(ctypes.WinDLL("winspool.drv", use_last_error=True))
    return _winspool


def _gdi32() -> object:
    global _gdi
    if _gdi is None:
        _gdi = _declare_gdi(ctypes.WinDLL("gdi32", use_last_error=True))
    return _gdi


def _fail(func: str, code: int | None = None) -> WinPrintError:
    if code is None:
        code = ctypes.get_last_error()
    try:
        text = ctypes.FormatError(code).strip()
    except Exception:  # pragma: no cover
        text = ""
    return WinPrintError(func, code, text)


# --- guards ---------------------------------------------------------------------------------------


def _check_print_allowed(printer: object, user_confirmed: bool, allow_any_printer: bool) -> str:
    """Raise ``WinPrintRefused`` unless this print is allowed; runs before any DLL is touched."""
    if user_confirmed is not True:
        raise WinPrintRefused("printing needs user_confirmed=True (the person must have agreed to this print)")
    if not isinstance(printer, str) or not printer.strip():
        raise WinPrintRefused("printing needs an explicit printer name (no default printer, no enumeration)")
    if printer.lower().startswith(REQUIRED_PREFIX):
        return printer
    if allow_any_printer is True and os.environ.get(ALLOW_ANY_ENV) == "1":
        return printer
    raise WinPrintRefused(
        f"printer {printer!r} does not start with {REQUIRED_PREFIX!r}; other printers need "
        f"allow_any_printer=True and {ALLOW_ANY_ENV}=1 (the owner's decision, never set by a harness)"
    )


# --- data types -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PrinterInfo:
    name: str
    port: str
    driver: str
    status: int


@dataclass(frozen=True)
class JobInfo:
    job_id: int
    printer: str
    document: str
    user: str
    datatype: str
    status: int
    pages_total: int
    pages_printed: int

    @property
    def flags(self) -> list[str]:
        return [name for bit, name in _JOB_FLAGS.items() if self.status & bit]

    @property
    def failed(self) -> bool:
        return bool(self.status & _FAILED)

    @property
    def done(self) -> bool:
        return bool(self.status & _DONE)


# --- reads ----------------------------------------------------------------------------------------


def list_printers() -> list[PrinterInfo]:
    """Installed local and connected printers (read-only). Never prints."""
    api = _spool()
    flags = PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS
    needed, returned = wintypes.DWORD(0), wintypes.DWORD(0)
    ok = api.EnumPrintersW(flags, None, 2, None, 0, ctypes.byref(needed), ctypes.byref(returned))
    if not ok:
        err = ctypes.get_last_error()
        if err != ERROR_INSUFFICIENT_BUFFER:
            raise _fail("EnumPrintersW", err)
    if needed.value == 0:
        return []
    buf = ctypes.create_string_buffer(needed.value)
    if not api.EnumPrintersW(flags, None, 2, buf, needed.value, ctypes.byref(needed), ctypes.byref(returned)):
        raise _fail("EnumPrintersW")
    arr = ctypes.cast(buf, _P(PRINTER_INFO_2W))
    return [
        PrinterInfo(
            name=arr[i].pPrinterName or "",
            port=arr[i].pPortName or "",
            driver=arr[i].pDriverName or "",
            status=int(arr[i].Status),
        )
        for i in range(returned.value)
    ]


def _open(api: object, printer: str) -> wintypes.HANDLE:
    h = wintypes.HANDLE()
    if not api.OpenPrinterW(printer, ctypes.byref(h), None):
        raise _fail("OpenPrinterW")
    return h


def _job_from(buf: object) -> JobInfo:
    j = ctypes.cast(buf, _P(JOB_INFO_1W))[0]
    return JobInfo(
        job_id=int(j.JobId),
        printer=j.pPrinterName or "",
        document=j.pDocument or "",
        user=j.pUserName or "",
        datatype=j.pDatatype or "",
        status=int(j.Status),
        pages_total=int(j.TotalPages),
        pages_printed=int(j.PagesPrinted),
    )


def job_status(printer: str, job_id: int) -> JobInfo | None:
    """Spooler view of one job, or ``None`` when the spooler no longer knows it (finished/purged)."""
    if not isinstance(printer, str) or not printer.strip():
        raise ValueError("job_status needs an explicit printer name")
    api = _spool()
    h = _open(api, printer)
    try:
        needed = wintypes.DWORD(0)
        ok = api.GetJobW(h, job_id, 1, None, 0, ctypes.byref(needed))
        if not ok:
            err = ctypes.get_last_error()
            if err == ERROR_INVALID_PARAMETER:
                return None
            if err != ERROR_INSUFFICIENT_BUFFER:
                raise _fail("GetJobW", err)
        buf = ctypes.create_string_buffer(max(needed.value, ctypes.sizeof(JOB_INFO_1W)))
        if not api.GetJobW(h, job_id, 1, buf, len(buf), ctypes.byref(needed)):
            err = ctypes.get_last_error()
            if err == ERROR_INVALID_PARAMETER:
                return None
            raise _fail("GetJobW", err)
        return _job_from(buf)
    finally:
        api.ClosePrinter(h)


def wait_for_job(
    printer: str,
    job_id: int,
    *,
    timeout: float = 60.0,
    poll_interval: float = 0.25,
    sleep=time.sleep,
    clock=time.monotonic,
) -> JobInfo | None:
    """Poll until the job is printed/complete, failed, or gone from the spooler.

    Returns the last ``JobInfo`` (check ``.failed``), or ``None`` if the spooler dropped the job
    (the normal outcome of a successful print once the queue purges it). Raises ``TimeoutError``.
    """
    deadline = clock() + timeout
    while True:
        info = job_status(printer, job_id)
        if info is None or info.done or info.failed:
            return info
        if clock() >= deadline:
            raise TimeoutError(f"job {job_id} on {printer!r} still {info.flags} after {timeout}s")
        sleep(poll_interval)


# --- printing -------------------------------------------------------------------------------------


def print_raw(
    printer: str,
    data: bytes,
    *,
    doc_name: str = "fakeprint",
    datatype: str = "RAW",
    user_confirmed: bool = False,
    allow_any_printer: bool = False,
) -> int:
    """Send ``data`` to the named printer's spooler as one document; returns the spooler job id."""
    _check_print_allowed(printer, user_confirmed, allow_any_printer)
    payload = bytes(data)
    api = _spool()
    h = _open(api, printer)
    try:
        di = DOC_INFO_1W(doc_name, None, datatype)
        job_id = api.StartDocPrinterW(h, 1, ctypes.byref(di))
        if not job_id:
            raise _fail("StartDocPrinterW")
        try:
            if not api.StartPagePrinter(h):
                raise _fail("StartPagePrinter")
            try:
                off = 0
                while off < len(payload):
                    chunk = payload[off : off + (1 << 20)]
                    written = wintypes.DWORD(0)
                    if not api.WritePrinter(h, chunk, len(chunk), ctypes.byref(written)):
                        raise _fail("WritePrinter")
                    if written.value == 0:
                        raise WinPrintError("WritePrinter", 0, "wrote 0 bytes")
                    off += written.value
            finally:
                api.EndPagePrinter(h)
        finally:
            api.EndDocPrinter(h)
        return int(job_id)
    finally:
        api.ClosePrinter(h)


def print_image(
    printer: str,
    rgb: bytes,
    width: int,
    height: int,
    *,
    doc_name: str = "fakeprint image",
    user_confirmed: bool = False,
    allow_any_printer: bool = False,
) -> None:
    """Print a top-down 24-bit RGB image through GDI at exactly ``width`` x ``height`` device pixels.

    The destination rectangle is 1:1 with the source (no scaling) at the page origin, so the pixel
    size the driver sees is the one given; physical size depends on the driver's DPI. GDI returns no
    spooler job id; look the job up with ``job_status`` if needed. Uses the default DEVMODE.
    """
    _check_print_allowed(printer, user_confirmed, allow_any_printer)
    if width <= 0 or height <= 0 or len(rgb) != width * height * 3:
        raise ValueError("rgb must be width*height*3 bytes of top-down RGB")
    stride = (width * 3 + 3) & ~3
    rows = bytearray(stride * height)
    for y in range(height):  # BGR order, rows padded to 4 bytes
        src = rgb[y * width * 3 : (y + 1) * width * 3]
        row = bytearray(src)
        row[0::3], row[2::3] = src[2::3], src[0::3]
        rows[y * stride : y * stride + width * 3] = row
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 24
    bmi.bmiHeader.biCompression = 0  # BI_RGB
    bmi.bmiHeader.biSizeImage = len(rows)
    gdi = _gdi32()
    hdc = gdi.CreateDCW(None, printer, None, None)
    if not hdc:
        raise _fail("CreateDCW")
    started = False
    try:
        di = DOCINFOW(ctypes.sizeof(DOCINFOW), doc_name, None, None, 0)
        if gdi.StartDocW(hdc, ctypes.byref(di)) <= 0:
            raise _fail("StartDocW")
        started = True
        if gdi.StartPage(hdc) <= 0:
            raise _fail("StartPage")
        buf = (ctypes.c_char * len(rows)).from_buffer(rows)
        # SRCCOPY = 0x00CC0020, DIB_RGB_COLORS = 0
        rc = gdi.StretchDIBits(hdc, 0, 0, width, height, 0, 0, width, height, buf, ctypes.byref(bmi), 0, 0x00CC0020)
        if rc <= 0:
            raise _fail("StretchDIBits")
        if gdi.EndPage(hdc) <= 0:
            raise _fail("EndPage")
        started = False
        if gdi.EndDoc(hdc) <= 0:
            raise _fail("EndDoc")
    except BaseException:
        if started:
            gdi.AbortDoc(hdc)
        raise
    finally:
        gdi.DeleteDC(hdc)
