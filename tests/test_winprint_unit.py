"""winprint with the DLL layer replaced by fakes: nothing here touches a real spooler or printer."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="winprint is Windows-only")

if sys.platform == "win32":
    from ctypes import wintypes

    import fakeprint
    from fakeprint import winprint as wp


class FakeSpool:
    """Stands in for winspool.drv; records calls, can fail a named function."""

    def __init__(self, fail=None, err=5, printers=(), job=None):
        self.calls: list[str] = []
        self.fail, self.err = fail, err
        self.written = b""
        self.docinfo = None
        self.opened: list[str] = []
        self.printers, self.job = printers, job
        self.closed = 0
        self.keep: list = []

    def _go(self, name: str) -> bool:
        self.calls.append(name)
        if self.fail == name:
            ctypes.set_last_error(self.err)
            return False
        return True

    def OpenPrinterW(self, name, phandle, defaults):
        self.opened.append(name)
        assert defaults is None
        if not self._go("OpenPrinterW"):
            return 0
        phandle._obj.value = 0x1234
        return 1

    def ClosePrinter(self, h):
        self.closed += 1
        self.calls.append("ClosePrinter")
        assert h.value == 0x1234
        return 1

    def StartDocPrinterW(self, h, level, pdoc):
        if not self._go("StartDocPrinterW"):
            return 0
        assert level == 1
        di = pdoc._obj
        self.docinfo = (di.pDocName, di.pOutputFile, di.pDatatype)
        return 77

    def StartPagePrinter(self, h):
        return self._go("StartPagePrinter")

    def EndPagePrinter(self, h):
        return self._go("EndPagePrinter")

    def EndDocPrinter(self, h):
        return self._go("EndDocPrinter")

    def WritePrinter(self, h, buf, n, pwritten):
        if not self._go("WritePrinter"):
            return 0
        assert len(buf) == n
        take = min(n, 3)  # force partial writes
        self.written += bytes(buf[:take])
        pwritten._obj.value = take
        return 1

    def EnumPrintersW(self, flags, name, level, buf, size, pneeded, preturned):
        self.calls.append("EnumPrintersW")
        assert level == 2 and name is None
        assert flags == wp.PRINTER_ENUM_LOCAL | wp.PRINTER_ENUM_CONNECTIONS
        pneeded._obj.value = ctypes.sizeof(wp.PRINTER_INFO_2W) * len(self.printers) + 16
        if buf is None:
            ctypes.set_last_error(wp.ERROR_INSUFFICIENT_BUFFER)
            return 0
        arr = ctypes.cast(buf, ctypes.POINTER(wp.PRINTER_INFO_2W))
        for i, (n, port, drv, st) in enumerate(self.printers):
            strs = [ctypes.create_unicode_buffer(x) for x in (n, port, drv)]
            self.keep += strs
            arr[i].pPrinterName = ctypes.cast(strs[0], wintypes.LPWSTR)
            arr[i].pPortName = ctypes.cast(strs[1], wintypes.LPWSTR)
            arr[i].pDriverName = ctypes.cast(strs[2], wintypes.LPWSTR)
            arr[i].Status = st
        preturned._obj.value = len(self.printers)
        return 1

    def GetJobW(self, h, job_id, level, buf, size, pneeded):
        self.calls.append("GetJobW")
        assert level == 1
        if self.job is None:
            ctypes.set_last_error(wp.ERROR_INVALID_PARAMETER)
            return 0
        pneeded._obj.value = ctypes.sizeof(wp.JOB_INFO_1W) + 64
        if buf is None:
            ctypes.set_last_error(wp.ERROR_INSUFFICIENT_BUFFER)
            return 0
        j = ctypes.cast(buf, ctypes.POINTER(wp.JOB_INFO_1W))[0]
        j.JobId = job_id
        j.Status = self.job
        j.TotalPages = 1
        return 1


class FakeGdi:
    def __init__(self, fail=None):
        self.calls: list[str] = []
        self.fail = fail
        self.dib = None

    def _r(self, name):
        self.calls.append(name)
        if self.fail == name:
            ctypes.set_last_error(1722)
            return 0
        return 1

    def CreateDCW(self, driver, device, port, devmode):
        self.calls.append("CreateDCW")
        self.device = device
        assert driver is None
        if self.fail == "CreateDCW":
            ctypes.set_last_error(1801)
            return None
        return 99

    def DeleteDC(self, hdc):
        self.calls.append("DeleteDC")
        return 1

    def StartDocW(self, hdc, pdi):
        self.docname = pdi._obj.lpszDocName
        self.cb = pdi._obj.cbSize
        return self._r("StartDocW")

    def StartPage(self, hdc):
        return self._r("StartPage")

    def EndPage(self, hdc):
        return self._r("EndPage")

    def EndDoc(self, hdc):
        return self._r("EndDoc")

    def AbortDoc(self, hdc):
        self.calls.append("AbortDoc")
        return 1

    def StretchDIBits(self, hdc, dx, dy, dw, dh, sx, sy, sw, sh, bits, pbmi, usage, rop):
        h = pbmi._obj.bmiHeader
        self.dib = (dx, dy, dw, dh, sx, sy, sw, sh, bytes(bits), (h.biWidth, h.biHeight, h.biPlanes, h.biBitCount), usage, rop)
        return self._r("StretchDIBits")


@pytest.fixture
def spool(monkeypatch):
    fake = FakeSpool()
    monkeypatch.setattr(wp, "_winspool", fake)
    monkeypatch.delenv(wp.ALLOW_ANY_ENV, raising=False)
    return fake


@pytest.fixture
def gdi(monkeypatch):
    fake = FakeGdi()
    monkeypatch.setattr(wp, "_gdi", fake)
    monkeypatch.delenv(wp.ALLOW_ANY_ENV, raising=False)
    return fake


@pytest.fixture
def tripwire(monkeypatch):
    """Any attempt to load a real DLL fails the test."""

    def boom(*a, **k):
        raise AssertionError("a real DLL was loaded")

    monkeypatch.setattr(wp, "_winspool", None)
    monkeypatch.setattr(wp, "_gdi", None)
    monkeypatch.setattr(wp.ctypes, "WinDLL", boom)
    monkeypatch.delenv(wp.ALLOW_ANY_ENV, raising=False)


# --- structure layout ------------------------------------------------------------------------------


def test_struct_layout():
    p = ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(wp.DOC_INFO_1W) == 3 * p
    assert ctypes.sizeof(wp.SYSTEMTIME) == 16
    assert wp.PRINTER_INFO_2W.pPortName.offset == 3 * p
    assert wp.PRINTER_INFO_2W.pDriverName.offset == 4 * p
    assert wp.PRINTER_INFO_2W.Attributes.offset == 13 * p
    assert wp.PRINTER_INFO_2W.Status.offset == 13 * p + 5 * 4
    assert wp.JOB_INFO_1W.Status.offset == 7 * p
    assert ctypes.sizeof(wp.BITMAPINFOHEADER) == 40
    assert wp.DOCINFOW.lpszDocName.offset == p  # cbSize int padded to pointer alignment


# --- guards (a tripwire proves no DLL is touched) ---------------------------------------------------


def test_refuses_without_user_confirmed(tripwire):
    with pytest.raises(wp.WinPrintRefused, match="user_confirmed"):
        wp.print_raw("fakeprint a", b"x")
    with pytest.raises(wp.WinPrintRefused, match="user_confirmed"):
        wp.print_raw("fakeprint a", b"x", user_confirmed=False)
    with pytest.raises(wp.WinPrintRefused, match="user_confirmed"):
        wp.print_image("fakeprint a", b"\0\0\0", 1, 1)


def test_user_confirmed_must_be_literal_true(tripwire):
    with pytest.raises(wp.WinPrintRefused):
        wp.print_raw("fakeprint", b"x", user_confirmed=1)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["", "   ", None, 5, b"fakeprint"])
def test_refuses_missing_or_non_string_name(tripwire, name):
    with pytest.raises(wp.WinPrintRefused, match="explicit printer name"):
        wp.print_raw(name, b"x", user_confirmed=True)


@pytest.mark.parametrize("name", ["HP LaserJet", "Microsoft Print to PDF", "my fakeprint", " fakeprint"])
def test_refuses_non_fakeprint_names(tripwire, name):
    with pytest.raises(wp.WinPrintRefused, match="fakeprint"):
        wp.print_raw(name, b"x", user_confirmed=True)
    with pytest.raises(wp.WinPrintRefused):
        wp.print_image(name, b"\0\0\0", 1, 1, user_confirmed=True)


def test_allow_any_needs_both_flag_and_env(tripwire, monkeypatch):
    with pytest.raises(wp.WinPrintRefused):
        wp.print_raw("Other", b"x", user_confirmed=True, allow_any_printer=True)  # no env
    monkeypatch.setenv(wp.ALLOW_ANY_ENV, "1")
    with pytest.raises(wp.WinPrintRefused):
        wp.print_raw("Other", b"x", user_confirmed=True)  # env but no flag
    monkeypatch.setenv(wp.ALLOW_ANY_ENV, "true")
    with pytest.raises(wp.WinPrintRefused):
        wp.print_raw("Other", b"x", user_confirmed=True, allow_any_printer=True)  # env must be exactly 1


def test_allow_any_passes_guard_with_both(monkeypatch, spool):
    monkeypatch.setenv(wp.ALLOW_ANY_ENV, "1")
    assert wp.print_raw("Other", b"abc", user_confirmed=True, allow_any_printer=True) == 77
    assert spool.opened == ["Other"]


def test_prefix_is_case_insensitive(spool):
    wp.print_raw("FakePrint Lab", b"abc", user_confirmed=True)
    assert spool.opened == ["FakePrint Lab"]


# --- print_raw -------------------------------------------------------------------------------------


def test_print_raw_call_sequence_and_docinfo(spool):
    job = wp.print_raw("fakeprint t", b"0123456789", doc_name="doc", datatype="XPS_PASS", user_confirmed=True)
    assert job == 77
    assert spool.written == b"0123456789"  # partial writes are looped
    assert spool.docinfo == ("doc", None, "XPS_PASS")
    c = spool.calls
    assert c[:3] == ["OpenPrinterW", "StartDocPrinterW", "StartPagePrinter"]
    assert c[-3:] == ["EndPagePrinter", "EndDocPrinter", "ClosePrinter"]
    assert spool.closed == 1


def test_print_raw_empty_payload(spool):
    assert wp.print_raw("fakeprint t", b"", user_confirmed=True) == 77
    assert "WritePrinter" not in spool.calls


@pytest.mark.parametrize(
    "func,code",
    [("OpenPrinterW", 1801), ("StartDocPrinterW", 5), ("StartPagePrinter", 6), ("WritePrinter", 1784)],
)
def test_print_raw_error_mapping_and_cleanup(monkeypatch, func, code):
    fake = FakeSpool(fail=func, err=code)
    monkeypatch.setattr(wp, "_winspool", fake)
    with pytest.raises(wp.WinPrintError) as ei:
        wp.print_raw("fakeprint t", b"abc", user_confirmed=True)
    assert ei.value.func == func and ei.value.code == code
    assert str(code) in str(ei.value) and ei.value.detail  # FormatError text
    assert fake.closed == (0 if func == "OpenPrinterW" else 1)  # handle never leaks
    if func == "WritePrinter":
        assert fake.calls[-3:] == ["EndPagePrinter", "EndDocPrinter", "ClosePrinter"]


# --- list_printers / job_status --------------------------------------------------------------------


def test_list_printers_read_only(spool):
    spool.printers = [("fakeprint a", "WSD-1", "Drv A", 0), ("Other", "LPT1:", "Drv B", 8)]
    out = wp.list_printers()
    assert out == [wp.PrinterInfo("fakeprint a", "WSD-1", "Drv A", 0), wp.PrinterInfo("Other", "LPT1:", "Drv B", 8)]
    assert set(spool.calls) == {"EnumPrintersW"}


def test_list_printers_empty_and_error(spool):
    assert wp.list_printers() == []  # zero printers
    spool.printers = []

    def bad(*a):
        ctypes.set_last_error(5)
        return 0

    spool.EnumPrintersW = bad
    with pytest.raises(wp.WinPrintError) as ei:
        wp.list_printers()
    assert ei.value.func == "EnumPrintersW" and ei.value.code == 5


def test_job_status_decodes_and_closes(spool):
    spool.job = wp.JOB_STATUS_PRINTING | wp.JOB_STATUS_SPOOLING
    info = wp.job_status("fakeprint t", 5)
    assert info.job_id == 5 and info.pages_total == 1
    assert info.flags == ["spooling", "printing"] and not info.done and not info.failed
    assert spool.closed == 1


def test_job_status_gone_is_none(spool):
    assert wp.job_status("fakeprint t", 5) is None
    assert spool.closed == 1


def test_job_status_needs_name(tripwire):
    with pytest.raises(ValueError):
        wp.job_status("", 1)


def test_wait_for_job_outcomes(spool):
    spool.job = None
    assert wp.wait_for_job("fakeprint t", 1) is None
    spool.job = wp.JOB_STATUS_PRINTED
    assert wp.wait_for_job("fakeprint t", 1).done
    spool.job = wp.JOB_STATUS_ERROR
    assert wp.wait_for_job("fakeprint t", 1).failed


def test_wait_for_job_times_out_with_fake_clock(spool):
    spool.job = wp.JOB_STATUS_SPOOLING
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    with pytest.raises(TimeoutError):
        wp.wait_for_job("fakeprint t", 1, timeout=1.0, poll_interval=0.5, sleep=sleep, clock=lambda: t[0])
    assert slept == [0.5, 0.5]


# --- print_image -----------------------------------------------------------------------------------


def test_print_image_dib_layout(gdi):
    rgb = bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])  # 2x2
    wp.print_image("fakeprint t", rgb, 2, 2, doc_name="img", user_confirmed=True)
    dx, dy, dw, dh, sx, sy, sw, sh, bits, hdr, usage, rop = gdi.dib
    assert (dx, dy, dw, dh, sx, sy, sw, sh) == (0, 0, 2, 2, 0, 0, 2, 2)  # 1:1, exact pixel size
    assert hdr == (2, -2, 1, 24)  # width, top-down height, planes, bpp
    assert bits == bytes([3, 2, 1, 6, 5, 4, 0, 0, 9, 8, 7, 12, 11, 10, 0, 0])  # BGR, 4-byte row pad
    assert (usage, rop) == (0, 0x00CC0020)
    assert gdi.docname == "img" and gdi.cb == ctypes.sizeof(wp.DOCINFOW) and gdi.device == "fakeprint t"
    assert gdi.calls[-3:] == ["EndPage", "EndDoc", "DeleteDC"]
    assert "AbortDoc" not in gdi.calls


def test_print_image_validates_size(gdi):
    with pytest.raises(ValueError):
        wp.print_image("fakeprint t", b"\0" * 5, 2, 2, user_confirmed=True)
    assert gdi.calls == []


@pytest.mark.parametrize("func,code", [("CreateDCW", 1801), ("StartDocW", 1722), ("StretchDIBits", 1722)])
def test_print_image_errors(monkeypatch, func, code):
    fake = FakeGdi(fail=func)
    monkeypatch.setattr(wp, "_gdi", fake)
    with pytest.raises(wp.WinPrintError) as ei:
        wp.print_image("fakeprint t", b"\0\0\0", 1, 1, user_confirmed=True)
    assert ei.value.func == func and ei.value.code == code
    assert ("AbortDoc" in fake.calls) == (func == "StretchDIBits")
    assert fake.calls.count("DeleteDC") == (0 if func == "CreateDCW" else 1)


# --- laziness / packaging --------------------------------------------------------------------------


def test_lazy_attribute_returns_module():
    assert fakeprint.winprint is wp


def test_import_fakeprint_does_not_import_winprint():
    code = "import sys, fakeprint; assert 'fakeprint.winprint' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True, env={**os.environ, "PYTHONPATH": "python"})


def test_declared_prototypes_are_explicit():
    class Fn:
        pass

    class Dll:
        def __getattr__(self, n):
            f = Fn()
            object.__setattr__(self, n, f)
            return f

    d = wp._declare_winspool(Dll())
    for name in ("OpenPrinterW", "StartDocPrinterW", "WritePrinter", "EnumPrintersW", "GetJobW", "ClosePrinter"):
        f = getattr(d, name)
        assert f.argtypes and f.restype is not None
    g = wp._declare_gdi(Dll())
    assert len(g.StretchDIBits.argtypes) == 13


# --- optional live test (a human must have installed a fakeprint* printer) ---------------------------

LIVE = os.environ.get("FAKEPRINT_WIN_PRINTER", "")


@pytest.mark.winprint_live
@pytest.mark.skipif(not LIVE.lower().startswith("fakeprint"), reason="FAKEPRINT_WIN_PRINTER is not a fakeprint* printer")
def test_live_print_to_installed_fakeprint_printer():
    if LIVE not in {p.name for p in wp.list_printers()}:
        pytest.skip(f"printer {LIVE!r} is not installed")
    job = wp.print_raw(LIVE, b"%!PS\nshowpage\n", doc_name="fakeprint-live", user_confirmed=True)
    info = wp.wait_for_job(LIVE, job, timeout=60)
    assert info is None or not info.failed
