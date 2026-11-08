"""Tells its parent about application launches, as JSON lines. A process of its own because NSWorkspace delivers these on the
main thread's run loop, which the server's event loop already owns. Standard library only, so it runs isolated (python -I).
It exits when its stdin closes, so it can never outlive the backend.

Visible applications only, which is what this notification covers. It cannot see background processes.
"""

import ctypes as ct
import json
import os
import sys
import threading
import time

FRAMEWORKS = "/System/Library/Frameworks"


def say(**message) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> int:
    objc = ct.CDLL("/usr/lib/libobjc.A.dylib")
    ct.CDLL(f"{FRAMEWORKS}/Foundation.framework/Foundation")
    appkit = ct.CDLL(f"{FRAMEWORKS}/AppKit.framework/AppKit")
    cf = ct.CDLL(f"{FRAMEWORKS}/CoreFoundation.framework/CoreFoundation")
    ptr = ct.c_void_p

    objc.objc_getClass.restype, objc.objc_getClass.argtypes = ptr, [ct.c_char_p]
    objc.sel_registerName.restype, objc.sel_registerName.argtypes = ptr, [ct.c_char_p]
    objc.objc_allocateClassPair.restype, objc.objc_allocateClassPair.argtypes = ptr, [ptr, ct.c_char_p, ct.c_size_t]
    objc.objc_registerClassPair.restype, objc.objc_registerClassPair.argtypes = None, [ptr]
    objc.class_addMethod.restype, objc.class_addMethod.argtypes = ct.c_bool, [ptr, ptr, ptr, ct.c_char_p]
    objc.objc_autoreleasePoolPush.restype = ptr
    objc.objc_autoreleasePoolPop.restype, objc.objc_autoreleasePoolPop.argtypes = None, [ptr]
    cf.CFRunLoopRun.restype = None

    def send(res, *args):
        return ct.CFUNCTYPE(res, ptr, ptr, *args)(("objc_msgSend", objc))

    send_id = send(ptr)
    send_text = send(ct.c_char_p)
    send_int = send(ct.c_int32)
    send_double = send(ct.c_double)
    send_id_id = send(ptr, ptr)
    send_observe = send(None, ptr, ptr, ptr, ptr)

    def sel(name: str):
        return objc.sel_registerName(name.encode())

    def text(obj) -> str | None:
        if not obj:
            return None
        raw = send_text(obj, sel("UTF8String"))
        return raw.decode("utf-8", "replace") if raw else None

    def url_path(url) -> str | None:
        return text(send_id(url, sel("path"))) if url else None

    def describe(app) -> dict:
        date = send_id(app, sel("launchDate"))
        return {
            "name": text(send_id(app, sel("localizedName"))),
            "bundle_id": text(send_id(app, sel("bundleIdentifier"))),
            "executable_path": url_path(send_id(app, sel("executableURL"))),
            "bundle_path": url_path(send_id(app, sel("bundleURL"))),
            "pid": send_int(app, sel("processIdentifier")),
            "launched_at": send_double(date, sel("timeIntervalSince1970")) if date else None,
        }

    app_key = ptr.in_dll(appkit, "NSWorkspaceApplicationKey").value

    def on_launch(_self, _cmd, notification) -> None:
        pool = objc.objc_autoreleasePoolPush()
        try:
            at = time.time()
            app = send_id_id(send_id(notification, sel("userInfo")), sel("objectForKey:"), app_key)
            say(type="launched", observed_at=at, **describe(app))
        except Exception as exc:
            say(type="error", error=f"{type(exc).__name__}: {exc}"[:200])
        finally:
            objc.objc_autoreleasePoolPop(pool)

    handler = ct.CFUNCTYPE(None, ptr, ptr, ptr)(on_launch)
    observer_class = objc.objc_allocateClassPair(objc.objc_getClass(b"NSObject"), b"LockiLaunchObserver", 0)
    objc.class_addMethod(observer_class, sel("launched:"), ct.cast(handler, ptr), b"v@:@")
    objc.objc_registerClassPair(observer_class)
    observer = send_id(send_id(observer_class, sel("alloc")), sel("init"))

    workspace = send_id(objc.objc_getClass(b"NSWorkspace"), sel("sharedWorkspace"))
    center = send_id(workspace, sel("notificationCenter"))
    name = ptr.in_dll(appkit, "NSWorkspaceDidLaunchApplicationNotification").value
    send_observe(center, sel("addObserver:selector:name:object:"), observer, sel("launched:"), name, None)

    front = send_id(workspace, sel("frontmostApplication"))
    say(type="ready", helper_pid=os.getpid(), frontmost=describe(front) if front else None)

    def exit_with_parent() -> None:
        sys.stdin.buffer.read()
        os._exit(0)

    threading.Thread(target=exit_with_parent, daemon=True).start()
    cf.CFRunLoopRun()
    return 0


if __name__ == "__main__":
    sys.exit(main())
