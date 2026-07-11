"""Launches xTranslator and drives the BatchProcessor via minimal UI automation.

The batch script itself is pre-placed as UserPrefs/<game>/batchprocessor/
~processor.tmp (auto-loaded by the dialog, TESVT_commandProcessor.pas), so the
only UI actions needed are:

    1. main menu:  Wizards -> BatchProcessor
    2. dialog:     click "Run Processor"   (ModalResult = mrOK starts the batch)

Completion is signalled in-band: the last batch rule sends wire.SENTINEL_TEXT
through the fake API server. An idle timeout acts as fallback.
"""

from __future__ import annotations

import re
import subprocess
import time

from .fakeapi import ServerState

MAIN_FORM_CLASS = "TForm1"
DIALOG_CLASS = "TForm24"
DIALOG_TITLE = "BatchProcessor"
RUN_BUTTON = "Run Processor"
MIN_VERSION = (1, 4, 16)  # first release with the BatchProcessor


class DriverError(RuntimeError):
    pass


def _menu_click(main_window, *path: str) -> None:
    """Walk the native menu by caption, ignoring '&' accelerator markers."""
    def clean(s: str) -> str:
        return s.replace("&", "").strip().lower()

    items = main_window.menu().items()
    for depth, wanted in enumerate(path):
        match = next((it for it in items if clean(it.text()) == clean(wanted)), None)
        if match is None:
            found = [it.text() for it in items]
            raise DriverError(
                f"menu item {wanted!r} not found (available: {found}). "
                "An xTranslator with BatchProcessor (v1.4.16+) and English UI is required."
            )
        if depth == len(path) - 1:
            match.select()
        else:
            items = match.sub_menu().items()


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
        title = main.window_text()
        log(f"main window: {title!r}")
        _check_version(title)

        _menu_click(main, "Wizards", DIALOG_TITLE)
        dlg = app.window(class_name=DIALOG_CLASS)
        dlg.wait("ready", timeout=30)
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
