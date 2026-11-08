"""USB arrivals and removals on macOS, read from the I/O Registry.

Event-driven through IOKit matching notifications on a thread of its own (no new dependency, ctypes only). If that cannot
start, a labelled fallback polls `ioreg` once a second, so its detection latency is up to that interval.

Only identifiers the OS already holds are read. This module never opens, mounts or lists anything on a device, and an
unfamiliar device is just unfamiliar. macOS may also hold back a new accessory until someone allows it, in which case
nothing appears here until they do.
"""

from __future__ import annotations

import ctypes as ct
import hashlib
import logging
import plistlib
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

log = logging.getLogger(__name__)

USB_CLASS = "IOUSBHostDevice"
IOREG = "/usr/sbin/ioreg"
POLL_INTERVAL_S = 1.0
# A device's interfaces attach just after the device itself. Waiting this long gets its class into the first event.
ENRICH_DELAY_S = 0.25

# USB-IF class codes. Input comes first: a device that is both storage and a keyboard should read as a keyboard.
_CATEGORY_ORDER: tuple[tuple[int, str], ...] = (
    (3, "input"), (11, "smart_card"), (8, "storage"), (6, "phone_or_camera"), (2, "network"), (10, "network"), (224, "wireless"), (1, "audio"), (14, "video"), (16, "video"),
    (7, "printer"), (9, "hub"), (255, "vendor_specific"),
)


@dataclass(frozen=True)
class UsbDevice:
    registry_id: int
    vendor_id: int
    product_id: int
    vendor_name: str | None = None
    product_name: str | None = None
    serial: str | None = field(default=None, repr=False)  # hashed before it goes anywhere; never logged
    device_class: int | None = None
    device_subclass: int | None = None
    device_protocol: int | None = None
    interface_classes: tuple[int, ...] = ()
    location_id: int | None = None

    def fingerprint(self, salt: bytes) -> str:
        return hashlib.sha256(salt + f"{self.vendor_id:04x}:{self.product_id:04x}:{self.serial or ''}".encode()).hexdigest()[:32]

    @property
    def category(self) -> str:
        classes = {*self.interface_classes, *({self.device_class} if self.device_class else ())}
        return next((name for code, name in _CATEGORY_ORDER if code in classes), "other" if classes else "unknown")


@dataclass(frozen=True)
class UsbChange:
    kind: Literal["added", "removed"]
    device: UsbDevice
    observed_at_ms: int
    observed_mono: float
    detection: Literal["iokit_notification", "ioreg_poll"]


OnChange = Callable[[UsbChange], None]


class UsbWatcher(Protocol):
    method: str

    def start(self, on_change: OnChange) -> list[UsbDevice]:
        """Starts watching and returns what is connected right now."""
        ...

    def stop(self) -> None: ...


# ------------------------------------------------------------------ polling fallback, also a second opinion in tests
def _text(node: dict, *keys: str) -> str | None:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value:
            return value[:120]
    return None


def devices_from_ioreg(data: bytes) -> list[UsbDevice]:
    """Every USB device in `ioreg -r -c IOUSBHostDevice -l -a` output, including those behind hubs."""
    if not data.strip():
        return []
    out: list[UsbDevice] = []

    def interfaces(node: dict) -> tuple[int, ...]:
        found = [c["bInterfaceClass"] for c in node.get("IORegistryEntryChildren", []) if isinstance(c.get("bInterfaceClass"), int)]
        return tuple(sorted(set(found)))

    def walk(node: dict) -> None:
        # Its interfaces and drivers repeat idVendor and idProduct: only the device node itself counts.
        if node.get("IOObjectClass") == USB_CLASS and isinstance(node.get("idVendor"), int) and isinstance(node.get("idProduct"), int):
            out.append(
                UsbDevice(
                    registry_id=int(node.get("IORegistryEntryID") or 0),
                    vendor_id=node["idVendor"],
                    product_id=node["idProduct"],
                    vendor_name=_text(node, "kUSBVendorString", "USB Vendor Name"),
                    product_name=_text(node, "kUSBProductString", "USB Product Name"),
                    serial=_text(node, "kUSBSerialNumberString", "USB Serial Number"),
                    device_class=node.get("bDeviceClass"),
                    device_subclass=node.get("bDeviceSubClass"),
                    device_protocol=node.get("bDeviceProtocol"),
                    interface_classes=interfaces(node),
                    location_id=node.get("locationID"),
                )
            )
        for child in node.get("IORegistryEntryChildren", []):
            walk(child)

    for root in plistlib.loads(data):
        walk(root)
    return out


def ioreg_snapshot(timeout_s: float = 5.0) -> list[UsbDevice]:
    proc = subprocess.run([IOREG, "-r", "-c", USB_CLASS, "-l", "-a"], capture_output=True, timeout=timeout_s, check=False)
    if proc.returncode != 0:
        raise OSError(f"ioreg exited with {proc.returncode}")
    return devices_from_ioreg(proc.stdout)


class PollingUsbWatcher:
    """Fallback, and labelled as such in every event: a change is seen up to POLL_INTERVAL_S late."""

    method = "ioreg_poll"

    def __init__(self, interval_s: float = POLL_INTERVAL_S, snapshot: Callable[[], list[UsbDevice]] = ioreg_snapshot):
        self.interval_s = interval_s
        self._snapshot = snapshot
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    def start(self, on_change: OnChange) -> list[UsbDevice]:
        current = {d.registry_id: d for d in self._snapshot()}
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(on_change, current), daemon=True, name="usb-poll")
        self._thread.start()
        return list(current.values())

    def _run(self, on_change: OnChange, known: dict[int, UsbDevice]) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                now = {d.registry_id: d for d in self._snapshot()}
                self.error = None
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("USB poll failed: %s", self.error)
                continue
            at_ms, mono = int(time.time() * 1000), time.monotonic()
            for rid in now.keys() - known.keys():
                on_change(UsbChange("added", now[rid], at_ms, mono, "ioreg_poll"))
            for rid in known.keys() - now.keys():
                on_change(UsbChange("removed", known[rid], at_ms, mono, "ioreg_poll"))
            known = now

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * self.interval_s + 5)


# ------------------------------------------------------------------ IOKit, event-driven
_ENCODING_UTF8 = 0x08000100
_NUMBER_SINT64 = 4
_io_object = ct.c_uint32
_kern_return = ct.c_int
_Matched = ct.CFUNCTYPE(None, ct.c_void_p, _io_object)


class _IOKit:
    """The few IOKit and CoreFoundation calls this needs, with exact signatures."""

    def __init__(self) -> None:
        io = ct.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        cf = ct.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ptr, u32, boolean = ct.c_void_p, ct.c_uint32, ct.c_bool

        def sig(lib, name, res, *args):
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = res, list(args)
            return fn

        self.string_create = sig(cf, "CFStringCreateWithCString", ptr, ptr, ct.c_char_p, u32)
        self.string_get = sig(cf, "CFStringGetCString", boolean, ptr, ct.c_char_p, ct.c_long, u32)
        self.number_get = sig(cf, "CFNumberGetValue", boolean, ptr, ct.c_long, ptr)
        self.type_id = sig(cf, "CFGetTypeID", ct.c_ulong, ptr)
        self.string_type = sig(cf, "CFStringGetTypeID", ct.c_ulong)()
        self.number_type = sig(cf, "CFNumberGetTypeID", ct.c_ulong)()
        self.release = sig(cf, "CFRelease", None, ptr)
        self.loop_current = sig(cf, "CFRunLoopGetCurrent", ptr)
        self.loop_add = sig(cf, "CFRunLoopAddSource", None, ptr, ptr, ptr)
        self.loop_run = sig(cf, "CFRunLoopRun", None)
        self.loop_stop = sig(cf, "CFRunLoopStop", None, ptr)
        self.default_mode = ct.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
        self.matching = sig(io, "IOServiceMatching", ptr, ct.c_char_p)
        self.matching_services = sig(io, "IOServiceGetMatchingServices", _kern_return, u32, ptr, ct.POINTER(_io_object))
        self.port_create = sig(io, "IONotificationPortCreate", ptr, u32)
        self.port_source = sig(io, "IONotificationPortGetRunLoopSource", ptr, ptr)
        self.port_destroy = sig(io, "IONotificationPortDestroy", None, ptr)
        self.add_notification = sig(io, "IOServiceAddMatchingNotification", _kern_return, ptr, ct.c_char_p, ptr, _Matched, ptr, ct.POINTER(_io_object))
        self.iterator_next = sig(io, "IOIteratorNext", _io_object, _io_object)
        self.object_release = sig(io, "IOObjectRelease", _kern_return, _io_object)
        self.entry_property = sig(io, "IORegistryEntryCreateCFProperty", ptr, _io_object, ptr, ptr, u32)
        self.entry_id = sig(io, "IORegistryEntryGetRegistryEntryID", _kern_return, _io_object, ct.POINTER(ct.c_uint64))
        self.child_iterator = sig(io, "IORegistryEntryGetChildIterator", _kern_return, _io_object, ct.c_char_p, ct.POINTER(_io_object))

    def prop(self, entry: int, name: str) -> int | str | None:
        key = self.string_create(None, name.encode(), _ENCODING_UTF8)
        if not key:
            return None
        try:
            value = self.entry_property(entry, key, None, 0)
        finally:
            self.release(key)
        if not value:
            return None
        try:
            kind = self.type_id(value)
            if kind == self.number_type:
                out = ct.c_int64()
                return out.value if self.number_get(value, _NUMBER_SINT64, ct.byref(out)) else None
            if kind == self.string_type:
                buf = ct.create_string_buffer(512)
                return buf.value.decode("utf-8", "replace") if self.string_get(value, buf, len(buf), _ENCODING_UTF8) else None
            return None
        finally:
            self.release(value)

    def registry_id(self, entry: int) -> int:
        out = ct.c_uint64()
        return out.value if self.entry_id(entry, ct.byref(out)) == 0 else 0

    def interfaces(self, entry: int) -> tuple[int, ...]:
        it = _io_object()
        if self.child_iterator(entry, b"IOService", ct.byref(it)) != 0:
            return ()
        found: set[int] = set()
        try:
            while child := self.iterator_next(it):
                try:
                    code = self.prop(child, "bInterfaceClass")
                    if isinstance(code, int):
                        found.add(code)
                finally:
                    self.object_release(child)
        finally:
            self.object_release(it)
        return tuple(sorted(found))

    def text(self, entry: int, *names: str) -> str | None:
        for name in names:
            value = self.prop(entry, name)
            if isinstance(value, str) and value:
                return value[:120]
        return None

    def number(self, entry: int, name: str) -> int | None:
        value = self.prop(entry, name)
        return value if isinstance(value, int) else None

    def read_device(self, entry: int) -> UsbDevice | None:
        vendor, product = self.number(entry, "idVendor"), self.number(entry, "idProduct")
        if vendor is None or product is None:
            return None
        return UsbDevice(
            registry_id=self.registry_id(entry),
            vendor_id=vendor,
            product_id=product,
            vendor_name=self.text(entry, "kUSBVendorString", "USB Vendor Name"),
            product_name=self.text(entry, "kUSBProductString", "USB Product Name"),
            serial=self.text(entry, "kUSBSerialNumberString", "USB Serial Number"),
            device_class=self.number(entry, "bDeviceClass"),
            device_subclass=self.number(entry, "bDeviceSubClass"),
            device_protocol=self.number(entry, "bDeviceProtocol"),
            interface_classes=self.interfaces(entry),
            location_id=self.number(entry, "locationID"),
        )

    def snapshot(self) -> list[UsbDevice]:
        it = _io_object()
        if self.matching_services(0, self.matching(USB_CLASS.encode()), ct.byref(it)) != 0:
            raise OSError("IOServiceGetMatchingServices failed")
        out: list[UsbDevice] = []
        try:
            while entry := self.iterator_next(it):
                try:
                    device = self.read_device(entry)
                    if device is not None:
                        out.append(device)
                finally:
                    self.object_release(entry)
        finally:
            self.object_release(it)
        return out


class IOKitUsbWatcher:
    """A CFRunLoop on its own thread receives matching notifications; a second thread reads each new device's details and
    reports it, in the order things happened."""

    method = "iokit_notification"

    def __init__(self, enrich_delay_s: float = ENRICH_DELAY_S):
        self.enrich_delay_s = enrich_delay_s
        self._io: _IOKit | None = None
        self._loop = None
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._ready = threading.Event()
        self._jobs: queue.Queue = queue.Queue()
        self._known: dict[int, UsbDevice] = {}
        self._error: str | None = None
        self._baseline: list[UsbDevice] = []
        self._callbacks: list = []  # ctypes callbacks must outlive the notifications

    def start(self, on_change: OnChange) -> list[UsbDevice]:
        if sys.platform != "darwin":
            raise OSError("IOKit needs macOS")
        self._io = _IOKit()
        self._ready.clear()
        self._worker = threading.Thread(target=self._work, args=(on_change,), daemon=True, name="usb-details")
        self._worker.start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="usb-iokit")
        self._thread.start()
        if not self._ready.wait(5.0):
            raise OSError("IOKit notifications did not start in time")
        if self._error:
            raise OSError(self._error)
        return list(self._baseline)

    def _run(self) -> None:
        io = self._io
        port = io.port_create(0)
        if not port:
            self._error = "IONotificationPortCreate failed"
            self._ready.set()
            return
        iterators = [_io_object(), _io_object()]

        def added(_refcon, iterator) -> None:
            self._drain_added(iterator)

        def removed(_refcon, iterator) -> None:
            self._drain_removed(iterator)

        on_added, on_removed = _Matched(added), _Matched(removed)
        self._callbacks = [on_added, on_removed]
        try:
            self._loop = io.loop_current()
            io.loop_add(self._loop, io.port_source(port), io.default_mode)
            if io.add_notification(port, b"IOServiceFirstMatch", io.matching(USB_CLASS.encode()), on_added, None, ct.byref(iterators[0])) != 0:
                raise OSError("could not register for USB arrivals")
            if io.add_notification(port, b"IOServiceTerminate", io.matching(USB_CLASS.encode()), on_removed, None, ct.byref(iterators[1])) != 0:
                raise OSError("could not register for USB removals")
            # Reading both iterators to the end arms them; what the first holds is what is connected right now.
            self._baseline = self._drain_baseline(iterators[0])
            self._drain_removed(iterators[1])
            self._ready.set()
            io.loop_run()
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"[:200]
            self._ready.set()
        finally:
            for it in iterators:
                if it.value:
                    io.object_release(it)
            io.port_destroy(port)

    def _drain_baseline(self, iterator) -> list[UsbDevice]:
        io = self._io
        found: list[UsbDevice] = []
        while entry := io.iterator_next(iterator):
            try:
                device = io.read_device(entry)
                if device is not None:
                    found.append(device)
                    self._known[device.registry_id] = device
            finally:
                io.object_release(entry)
        return found

    def _drain_added(self, iterator) -> None:
        io = self._io
        while entry := io.iterator_next(iterator):
            # The entry stays retained until its details have been read on the other thread.
            self._jobs.put(("added", entry, int(time.time() * 1000), time.monotonic()))

    def _drain_removed(self, iterator) -> None:
        io = self._io
        while entry := io.iterator_next(iterator):
            rid = io.registry_id(entry)
            io.object_release(entry)
            self._jobs.put(("removed", rid, int(time.time() * 1000), time.monotonic()))

    def _work(self, on_change: OnChange) -> None:
        io = self._io
        while True:
            job = self._jobs.get()
            if job is None:
                return
            kind, handle, at_ms, mono = job
            try:
                if kind == "added":
                    wait = self.enrich_delay_s - (time.monotonic() - mono)
                    if wait > 0:
                        time.sleep(wait)
                    try:
                        device = io.read_device(handle)
                    finally:
                        io.object_release(handle)
                    if device is not None:
                        self._known[device.registry_id] = device
                        on_change(UsbChange("added", device, at_ms, mono, "iokit_notification"))
                else:
                    device = self._known.pop(handle, None)
                    if device is not None:
                        on_change(UsbChange("removed", device, at_ms, mono, "iokit_notification"))
            except Exception:
                log.exception("handling a USB %s failed", kind)

    def stop(self) -> None:
        if self._io is not None and self._loop:
            self._io.loop_stop(self._loop)
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._jobs.put(None)
        if self._worker is not None:
            self._worker.join(timeout=3)
        self._loop = None


def create_usb_watcher(prefer: str = "iokit") -> UsbWatcher:
    return PollingUsbWatcher() if prefer == "poll" or sys.platform != "darwin" else IOKitUsbWatcher()


def iokit_snapshot() -> list[UsbDevice]:
    return _IOKit().snapshot()
