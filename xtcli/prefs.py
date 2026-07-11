"""Editing of xTranslator preference files.

All edits are done at byte level (line-wise) so the original encoding of the
files - Delphi writes them in the system ANSI codepage unless they carry a BOM -
is never disturbed. Only pure-ASCII keys/values are written by xtcli.

A one-time backup `<name>.xtcli-bak` is created next to each file before the
first modification; `restore_all` copies the backups back.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .wire import QUERY_PREF

BAK_SUFFIX = ".xtcli-bak"

MR_YES = 6  # Delphi mrYes; pre-answers the finalize confirmation (askPrefsArray[0])
ASK_PREFS_SLOTS = 16  # tPrefsArray = array [0..15] (TESVT_Const.pas)


def _split_lines(data: bytes) -> tuple[bytes, list[bytes], bytes]:
    """Return (bom, lines-without-endings, newline bytes)."""
    bom = b""
    for candidate in (b"\xef\xbb\xbf",):
        if data.startswith(candidate):
            bom, data = candidate, data[len(candidate):]
    nl = b"\r\n" if b"\r\n" in data or b"\n" not in data else b"\n"
    lines = data.split(b"\r\n") if nl == b"\r\n" else data.split(b"\n")
    # normalize stray \n inside \r\n files
    if nl == b"\r\n":
        lines = [piece for line in lines for piece in line.split(b"\n")]
    return bom, lines, nl


def set_values(path: Path, values: dict[str, str]) -> None:
    """Set name=value pairs in a Delphi TStringList-style file (case-insensitive names)."""
    data = path.read_bytes() if path.is_file() else b""
    bom, lines, nl = _split_lines(data)
    pending = {k.lower(): (k, v) for k, v in values.items()}
    out: list[bytes] = []
    for line in lines:
        name = line.split(b"=", 1)[0].strip().lower().decode("ascii", "replace")
        if b"=" in line and name in pending:
            key, val = pending.pop(name)
            out.append(f"{key}={val}".encode("ascii"))
        else:
            out.append(line)
    while out and out[-1] == b"":
        out.pop()
    for key, val in pending.values():
        out.append(f"{key}={val}".encode("ascii"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bom + nl.join(out) + nl)


def get_value(path: Path, name: str) -> str | None:
    if not path.is_file():
        return None
    _, lines, _ = _split_lines(path.read_bytes())
    for line in lines:
        if b"=" in line:
            k, v = line.split(b"=", 1)
            if k.strip().lower().decode("ascii", "replace") == name.lower():
                return v.decode("ascii", "replace")
    return None


def backup_once(path: Path) -> None:
    bak = path.with_name(path.name + BAK_SUFFIX)
    if path.is_file() and not bak.exists():
        shutil.copy2(path, bak)


def apply_setup(cfg) -> list[str]:
    """Patch xTranslator prefs to route OpenAI translation to the local server."""
    done = []

    # 1. commonApiPrefs.ini: route the "OpenAI" API to the local fake server.
    #    OpenAI_Url/Model/Query override Misc/ApiTranslator.txt at runtime
    #    (TESVT_TranslatorApi.pas TOpenAITranslator.Translate); a non-empty
    #    OpenAI_Key is required for apiEnabled[7].
    backup_once(cfg.api_prefs)
    set_values(cfg.api_prefs, {
        "OpenAI_Key": "xtcli-local",
        "OpenAI_Url": cfg.api_url,
        "OpenAI_Model": "xtcli",
        "OpenAI_Query": QUERY_PREF,
    })
    done.append(f"patched {cfg.api_prefs}")

    # 2. Misc/ApiTranslator.txt: one string per request (removes array/multiline
    #    ambiguity), no throttling (server is localhost).
    backup_once(cfg.api_translator_txt)
    set_values(cfg.api_translator_txt, {
        "OpenAI_enabled": "true",
        "OpenAI_ArrayLimit": "1",
        "OpenAI_ArrayTimePause": "0",
        "OpenAI_SingleTimePause": "0",
        "OpenAI_ArrayMaxCharPerMin": "999999999",
        "OpenAI_CharLimit": "60000",
    })
    done.append(f"patched {cfg.api_translator_txt}")

    # 3. commonPrefs.ini: fixed English UI (stable captions for automation) and
    #    remembered "Yes" for the finalize confirmation (ArgsPref slot 0).
    backup_once(cfg.common_prefs)
    slots = [0] * ASK_PREFS_SLOTS
    existing = get_value(cfg.common_prefs, "ArgsPref")
    if existing:
        for i, tok in enumerate(existing.split(",")[:ASK_PREFS_SLOTS]):
            try:
                slots[i] = int(tok)
            except ValueError:
                pass
    slots[0] = MR_YES
    set_values(cfg.common_prefs, {
        "UILang": "English",
        "ArgsPref": ",".join(str(s) for s in slots),
    })
    done.append(f"patched {cfg.common_prefs}")
    return done


def restore_all(cfg) -> list[str]:
    done = []
    for path in (cfg.api_prefs, cfg.api_translator_txt, cfg.common_prefs):
        bak = path.with_name(path.name + BAK_SUFFIX)
        if bak.is_file():
            shutil.copy2(bak, path)
            done.append(f"restored {path}")
    return done
