"""xtcli command line interface.

Commands:
    setup    register the xTranslator install and patch its prefs
    extract  pull translatable strings out of esp/esm/esl/pex/MCM files -> JSON
    apply    write translations from JSON back and produce finalized files
    run      execute a hand-written BatchProcessor script headlessly
    doctor   sanity-check the environment
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__, batchgen, prefs
from .config import Config, GAMES, load_config, save_config
from .fakeapi import FakeApiServer

AGENT_INSTRUCTIONS = (
    "Fill in every empty 'trans' with the translation of 'orig'. Keep placeholder "
    "tokens exactly as they are: '<a_A>' (alias), standalone numbers like '999', "
    "'<Alias=...>' tags, '%s'-style format codes and any <...> markup. Keep the "
    "same number of line breaks. Leave 'orig' untouched."
)

PLUGIN_EXTS = {".esp", ".esm", ".esl"}


# --------------------------------------------------------------------------- helpers

def _ensure_setup(cfg: Config) -> None:
    """Re-assert the prefs overrides (idempotent, cheap) before every run."""
    cfg.validate()
    if prefs.get_value(cfg.api_prefs, "OpenAI_Url") != cfg.api_url \
            or prefs.get_value(cfg.api_translator_txt, "OpenAI_ArrayLimit") != "1":
        for line in prefs.apply_setup(cfg):
            print(line)


def _abs_files(names: list[str]) -> list[Path]:
    out = []
    for n in names:
        p = Path(n).resolve()
        if not p.is_file():
            raise SystemExit(f"input file not found: {p}")
        out.append(p)
    return out


def _print_report(report: dict) -> None:
    status = "completed" if report["completed"] else ("stalled/timeout" if report["stalled"] else "unknown")
    print(f"batch {status}: {report['requests']} API request(s) in {report['elapsed_sec']}s")
    for err in report["server_errors"]:
        print(f"  server error: {err}", file=sys.stderr)


def _run_batch(cfg: Config, script: str, server: FakeApiServer, verbose: bool) -> dict:
    from .driver import DriverError, run_batch
    batchgen.write_script(cfg, script)
    try:
        with server:
            report = run_batch(cfg, server.state, verbose=verbose)
    except DriverError as e:
        raise SystemExit(f"error: {e}")
    _print_report(report)
    return report


# --------------------------------------------------------------------------- commands

def cmd_setup(args) -> int:
    cfg = load_config()
    if args.exe:
        cfg.xtranslator_exe = str(Path(args.exe).resolve())
    if args.port:
        cfg.port = args.port
    if args.game:
        cfg.game = args.game
    cfg.validate()
    if args.restore:
        for line in prefs.restore_all(cfg):
            print(line)
        return 0
    path = save_config(cfg)
    print(f"config written: {path}")
    for line in prefs.apply_setup(cfg):
        print(line)
    print("setup complete. Launch xTranslator once manually if this is a fresh "
          "install (first-run game path dialogs cannot be automated).")
    return 0


def cmd_extract(args) -> int:
    cfg = load_config()
    _ensure_setup(cfg)
    files = _abs_files(args.files)
    ssts = _abs_files(args.sst) if args.sst else []
    source, dest = args.source or cfg.source_lang, args.dest or cfg.dest_lang

    workdir = Path(tempfile.mkdtemp(prefix="xtcli-extract-"))
    try:
        sentinel = batchgen.write_sentinel_mcm(workdir, source)
        script = batchgen.build_script(
            files, source, dest, ssts=ssts, sst_params=(args.sst_p1, args.sst_p2),
            finalize=False, sentinel_file=sentinel,
        )
        server = FakeApiServer(cfg.port, "capture")
        report = _run_batch(cfg, script, server, args.verbose)

        strings = [{"orig": s, "trans": ""} for s in server.state.captured]
        out = Path(args.output) if args.output else (
            files[0].with_suffix(files[0].suffix + ".strings.json") if len(files) == 1
            else Path("strings.json")
        )
        out.write_text(json.dumps({
            "tool": f"xtcli {__version__}",
            "source_lang": source,
            "dest_lang": dest,
            "files": [str(f) for f in files],
            "instructions": AGENT_INSTRUCTIONS,
            "strings": strings,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{len(strings)} string(s) -> {out}")
        return 0 if report["completed"] else 2
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _stage_inputs(files: list[Path], staging: Path) -> dict[Path, Path]:
    """Copy inputs (plus Strings sidecar folders for plugins) into staging."""
    mapping = {}
    for i, f in enumerate(files):
        sub = staging / f"in{i}"
        sub.mkdir(parents=True)
        staged = sub / f.name
        shutil.copy2(f, staged)
        if f.suffix.lower() in PLUGIN_EXTS:
            sidecar = f.parent / "Strings"
            if sidecar.is_dir():
                shutil.copytree(sidecar, sub / "Strings")
        mapping[f] = staged
    return mapping


def _harvest_outputs(mapping: dict[Path, Path], outdir: Path) -> list[Path]:
    """Collect finalized files: xtcli_out subfolders + staged copies changed in place."""
    outdir.mkdir(parents=True, exist_ok=True)
    results = []
    for orig, staged in mapping.items():
        exported = staged.parent / batchgen.EXPORT_SUBFOLDER
        if exported.is_dir():
            for p in exported.rglob("*"):
                if p.is_file():
                    target = outdir / p.relative_to(exported)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, target)
                    results.append(target)
        # pex finalize writes next to the loaded file instead of the export folder
        if staged.is_file() and staged.read_bytes() != orig.read_bytes():
            target = outdir / staged.name
            shutil.copy2(staged, target)
            results.append(target)
    return results


def cmd_apply(args) -> int:
    cfg = load_config()
    _ensure_setup(cfg)
    files = _abs_files(args.files)
    ssts = _abs_files(args.sst) if args.sst else []
    data = json.loads(Path(args.translations).read_text(encoding="utf-8"))
    table = {e["orig"]: e["trans"] for e in data["strings"] if e.get("trans")}
    source = args.source or data.get("source_lang") or cfg.source_lang
    dest = args.dest or data.get("dest_lang") or cfg.dest_lang
    outdir = Path(args.output).resolve()
    print(f"{len(table)} translation(s) loaded from {args.translations}")

    staging = Path(tempfile.mkdtemp(prefix="xtcli-apply-"))
    try:
        mapping = _stage_inputs(files, staging)
        sentinel = batchgen.write_sentinel_mcm(staging, source)
        script = batchgen.build_script(
            list(mapping.values()), source, dest,
            ssts=ssts, sst_params=(args.sst_p1, args.sst_p2),
            finalize=True, sentinel_file=sentinel,
        )
        server = FakeApiServer(cfg.port, "serve", table)
        report = _run_batch(cfg, script, server, args.verbose)

        results = _harvest_outputs(mapping, outdir)
        state = server.state
        print(f"served {len(state.served)} translation(s), "
              f"{len(state.missing)} string(s) had no translation")
        if state.missing and args.missing_out:
            Path(args.missing_out).write_text(json.dumps({
                "source_lang": source, "dest_lang": dest,
                "instructions": AGENT_INSTRUCTIONS,
                "strings": [{"orig": s, "trans": ""} for s in state.missing],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"missing strings -> {args.missing_out}")
        if not results:
            print("warning: no finalized output files were produced", file=sys.stderr)
        for r in results:
            print(f"output: {r}")
        return 0 if report["completed"] and results else 2
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def cmd_run(args) -> int:
    cfg = load_config()
    _ensure_setup(cfg)
    script = Path(args.script).read_text(encoding="utf-8-sig")
    table = {}
    if args.serve:
        data = json.loads(Path(args.serve).read_text(encoding="utf-8"))
        table = {e["orig"]: e["trans"] for e in data["strings"] if e.get("trans")}
    server = FakeApiServer(cfg.port, "serve" if args.serve else "capture", table)
    report = _run_batch(cfg, script, server, args.verbose)
    if args.output:
        Path(args.output).write_text(json.dumps(
            {"strings": [{"orig": s, "trans": ""} for s in server.state.captured]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"captured strings -> {args.output}")
    return 0 if report["completed"] else 2


def cmd_doctor(args) -> int:
    cfg = load_config()
    ok = True

    def check(label: str, good: bool, hint: str = "") -> None:
        nonlocal ok
        print(f"[{'ok' if good else 'NG'}] {label}" + (f"  ({hint})" if hint and not good else ""))
        ok = ok and good

    check("config file", cfg.path is not None, "run: xtcli setup --exe <path>")
    check("xTranslator exe", bool(cfg.xtranslator_exe) and cfg.exe.is_file(), cfg.xtranslator_exe or "not set")
    if cfg.xtranslator_exe and cfg.exe.is_file():
        check("Misc/ApiTranslator.txt patched",
              prefs.get_value(cfg.api_translator_txt, "OpenAI_ArrayLimit") == "1", "run: xtcli setup")
        check("commonApiPrefs.ini patched",
              prefs.get_value(cfg.api_prefs, "OpenAI_Url") == cfg.api_url, "run: xtcli setup")
        check("BatchProcessor available (v1.4.16+)", True,
              "cannot verify version automatically - check Help/About in the GUI")
    try:
        import pywinauto  # noqa: F401
        check("pywinauto installed", True)
    except ImportError:
        check("pywinauto installed", False, "pip install pywinauto")
    try:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", cfg.port))
        check(f"port {cfg.port} free", True)
    except OSError:
        check(f"port {cfg.port} free", False, "another process is using it (or a previous run is stuck)")
    return 0 if ok else 1


# --------------------------------------------------------------------------- parser

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="xtcli", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"xtcli {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup", help="register the xTranslator install and patch prefs")
    p.add_argument("--exe", help="path to xTranslator.exe")
    p.add_argument("--port", type=int, help="local API port (default 8737)")
    p.add_argument("--game", choices=sorted(GAMES), help="target game workspace")
    p.add_argument("--restore", action="store_true", help="restore original pref files from backups")
    p.set_defaults(func=cmd_setup)

    def common(p):
        p.add_argument("--from", dest="source", help="source language (default: config)")
        p.add_argument("--to", dest="dest", help="destination language (default: config)")
        p.add_argument("--sst", action="append", help="SST dictionary to apply first (repeatable)")
        p.add_argument("--sst-p1", type=int, default=0, help="importsst compare option (default 0)")
        p.add_argument("--sst-p2", type=int, default=1, help="importsst apply mode 0-3 (default 1)")
        p.add_argument("-v", "--verbose", action="store_true")

    p = sub.add_parser("extract", help="extract translatable strings to JSON")
    p.add_argument("files", nargs="+", help="esp/esm/esl/pex/MCM txt files")
    p.add_argument("-o", "--output", help="output JSON path")
    common(p)
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("apply", help="apply translations from JSON and finalize")
    p.add_argument("files", nargs="+")
    p.add_argument("-t", "--translations", required=True, help="strings JSON from 'extract'")
    p.add_argument("-o", "--output", required=True, help="output directory for finalized files")
    p.add_argument("--missing-out", help="write strings that had no translation to this JSON")
    common(p)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("run", help="run a raw BatchProcessor script headlessly")
    p.add_argument("script", help="BatchProcessor script (txt)")
    p.add_argument("--serve", help="strings JSON to serve as translations")
    p.add_argument("-o", "--output", help="dump captured strings to JSON")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("doctor", help="check the environment")
    p.set_defaults(func=cmd_doctor)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
