"""Launches xTranslator and drives the BatchProcessor headlessly.

The batch script is pre-placed as UserPrefs/<game>/batchprocessor/
~processor.tmp (auto-loaded by the dialog, TESVT_commandProcessor.pas), so
only two UI actions are needed, both performed with window messages (no real
mouse/keyboard input, works without an interactive foreground window):

    1. fire the Wizards -> BatchProcessor menu item via WM_COMMAND
       (candidate ids are verified by waiting for the TForm24 dialog)
    2. press "Run Processor" via BM_CLICK (ModalResult=mrOK starts the batch)

Completion is signalled in-band: the last batch rule sends wire.SENTINEL_TEXT
through the fake API server. An idle timeout acts as fallback.
"""

from __future__ import annotations

import re
import subprocess
import time

from . import menu
from .fakeapi import ServerState

MAIN_FORM_CLASS = "TForm1"
DIALOG_CLASS = "TForm24"
WIZARDS_MENU = "wizards"
RUN_BUTTON = "Run Processor"
MIN_VERSION = (1, 4, 16)  # first release with the BatchProcessor


class DriverError(RuntimeError):
    pass


def _check_version(title: str) -> None:
    m = re.search(r"v(\d+)\.(\d+)\.?(\d*)", title)
    if not m:
        return  # unknown title format - let it proceed
    version = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    if version < MIN_VERSION:
        raise DriverError(
            f"xTranslator {'.'.join(map(str, version))} has no BatchProcessor - "
            f"version {'.'.join(map(str, MIN_VERSION))} or later is required "
            "(https://www.nexusmods.com/skyrimspecialedition/mods/134)"
        )


def _close_stray_dialogs(app, log) -> None:
    """Close any visible window that is not the main form (wrong menu item fired)."""
    for w in app.windows(visible_only=True):
        try:
            if w.class_name() not in (MAIN_FORM_CLASS, "TApplication"):
                log(f"closing stray window {w.class_name()} {w.window_text()!r}")
                w.close()
        except Exception:
            pass


def _open_batchprocessor(app, hwnd: int, log) -> object:
    """Fire menu WM_COMMANDs until the BatchProcessor dialog (TForm24) opens."""
    names = menu.top_level_menu_names(hwnd)
    log(f"menu bar: {names}")
    cleaned = [n.replace("&", "").strip().lower() for n in names]
    if WIZARDS_MENU not in cleaned:
        raise DriverError(
            f"'Wizards' menu not found (menu bar: {names}) - xTranslator with "
            "BatchProcessor (v1.4.16+) and English UI is required"
        )
    ids = menu.submenu_command_ids(hwnd, cleaned.index(WIZARDS_MENU))
    if not ids:
        raise DriverError("Wizards menu has no enabled items")
    # batchProcessor is the last item of the Wizards menu, so try ids in reverse;
    # each candidate is verified by whether TForm24 actually opens
    for cid in reversed(ids):
        log(f"posting WM_COMMAND {cid}")
        menu.post_command(hwnd, cid)
        dlg = app.window(class_name=DIALOG_CLASS)
        try:
            dlg.wait("ready", timeout=8)
            return dlg
        except Exception:
            _close_stray_dialogs(app, log)
    raise DriverError("BatchProcessor dialog (TForm24) did not open")


def run_batch(cfg, state: ServerState, verbose: bool = False) -> dict:
    """Run the pre-written batch script; returns a small report dict."""
    from pywinauto import Application  # deferred: import is slow

    def log(msg: str) -> None:
        if verbose:
            print(f"[driver] {msg}")

    t0 = time.monotonic()
    proc = subprocess.Popen([str(cfg.exe), f"-{cfg.game}"], cwd=str(cfg.install_dir))
    app = None
    try:
        app = Application(backend="win32").connect(process=proc.pid, timeout=cfg.launch_timeout)
        main = app.window(class_name=MAIN_FORM_CLASS)
        main.wait("ready", timeout=cfg.launch_timeout)
        time.sleep(1.0)  # let startup settle (menu is built during FormCreate)
        title = main.window_text()
        log(f"main window: {title!r}")
        _check_version(title)

        dlg = _open_batchprocessor(app, main.handle, log)
        log("batch processor dialog open")
        dlg.child_window(title=RUN_BUTTON, class_name="TButton").click()

        # Wait for the sentinel string (primary) or idle/overall timeout (fallback).
        stalled = False
        while True:
            if state.done.wait(timeout=1.0):
                log("sentinel received - batch finished")
                break
            if proc.poll() is not None:
                raise DriverError("xTranslator exited before the batch finished")
            now = time.monotonic()
            if now - t0 > cfg.overall_timeout:
                stalled = True
                break
            last = state.last_request or t0
            if now - last > cfg.idle_timeout:
                stalled = True
                break

        # give xTranslator a moment to run the trailing closeall/save work
        time.sleep(2.0)
        elapsed = time.monotonic() - t0
        return {
            "elapsed_sec": round(elapsed, 1),
            "requests": state.n_requests,
            "completed": state.done.is_set(),
            "stalled": stalled,
            "server_errors": list(state.errors),
        }
    finally:
        _shutdown(proc, app, log)
        try:
            cfg.batchprocessor_tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _shutdown(proc: subprocess.Popen, app, log) -> None:
    if proc.poll() is not None:
        return
    try:
        if app is not None:
            _close_stray_dialogs(app, log)
            app.window(class_name=MAIN_FORM_CLASS).close()
            proc.wait(timeout=15)
            return
    except Exception:
        pass
    log("graceful close failed - terminating process")
    proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
