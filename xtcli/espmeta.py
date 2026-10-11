"""Dialogue metadata extraction from esp/esm/esl plugins.

xTranslator's API wire carries bare source strings, so `extract` output alone
says nothing about WHO speaks a line. This module walks the plugin records
directly (stdlib struct/zlib only) and builds text -> {kind, speaker, edid}
so a translating agent can pick the right speech register (e.g. a female
NPC's line must not get masculine Japanese sentence endings).

Binary layout facts verified against xEdit (wbDefinitionsTES5.pas):
  record header (24B): type[4] dataSize:u32 flags:u32 formid:u32 vc:u32
                       version:u16 unknown:u16
  GRUP header  (24B): 'GRUP' groupSize:u32(incl. header) label[4]
                       groupType:i32 stamp/unk/version/unk:u16*4
  record flags: 0x00040000 = zlib-compressed data (u32 decomp size prefix)
  TES4 flags:   0x00000080 = localized (strings are IDs -> nothing to match)
  CTDA (32B): Type:u8 unused[3] CompValue:f32(or GLOB if flag 0x04)
              Function:u16 unused[2] Param1:u32 Param2:u32 RunOn:u32
              Reference:u32 Param3:i32
    Type: high 3 bits = operator (0 eq,1 ne,2 gt,3 ge,4 lt,5 le),
          flags: 0x01 OR, 0x02 params use aliases, 0x04 use global,
          0x10 swap subject and target
    functions: GetIsSex=70 (Param1: 0=Male 1=Female), GetIsID=72,
               GetIsVoiceType=426 (Param1: VTYP or FLST formid)
  VTYP: DNAM u8 flags bit1 = Female
  NPC_: ACBS u32 flags bit0 = Female; template flags u16 at ACBS+18,
        bit0 = Use Traits (sex then comes from the TPLT template)
  in-file formids: top byte = index into the MAST list; >= len(masters)
        means the record/reference lives in this file
"""

from __future__ import annotations

import json
import struct
import zlib
from collections import Counter
from pathlib import Path

REC_HEADER = 24
FLAG_COMPRESSED = 0x00040000
FLAG_LOCALIZED = 0x00000080

FUNC_GET_IS_SEX = 70
FUNC_GET_IS_ID = 72
FUNC_GET_IS_VOICE_TYPE = 426

CTDA_FLAG_USE_ALIASES = 0x02
CTDA_FLAG_USE_GLOBAL = 0x04
CTDA_FLAG_SWAP_SUBJECT = 0x10

# subrecord -> kind. Anything not listed simply gets no metadata, which is
# safe: strings.json consumers treat the fields as optional.
_KIND_MAP = {
    ("INFO", "NAM1"): "npc_line",
    ("INFO", "RNAM"): "player_line",
    ("DIAL", "FULL"): "player_line",
    ("BOOK", "DESC"): "book",
    ("BOOK", "CNAM"): "book",
    ("QUST", "CNAM"): "system",
    ("QUST", "NNAM"): "system",
    ("MESG", "DESC"): "system",
    ("MESG", "ITXT"): "system",
    ("LSCR", "DESC"): "system",
    ("PERK", "DESC"): "system",
    ("SPEL", "DESC"): "system",
    ("MGEF", "DNAM"): "system",
    ("AVIF", "DESC"): "system",
}
_GENERIC_NAME_SUBS = {"FULL", "SHRT"}  # any record: display name

_KIND_PRIORITY = ["npc_line", "player_line", "book", "name", "system", "other"]

_CACHE_VERSION = 3


class PluginError(Exception):
    pass


def _decode(raw: bytes) -> str:
    raw = raw.split(b"\0", 1)[0]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _iter_subrecords(payload: bytes):
    """Yield (type, data); handles XXXX size extension."""
    pos, end, big = 0, len(payload), None
    while pos + 6 <= end:
        stype = payload[pos:pos + 4].decode("ascii", errors="replace")
        size = struct.unpack_from("<H", payload, pos + 4)[0]
        pos += 6
        if stype == "XXXX" and size == 4:
            big = struct.unpack_from("<I", payload, pos)[0]
            pos += 4
            continue
        if big is not None:
            size, big = big, None
        yield stype, payload[pos:pos + size]
        pos += size


def _record_payload(data: bytes, pos: int, size: int, flags: int) -> bytes:
    raw = data[pos:pos + size]
    if flags & FLAG_COMPRESSED and len(raw) >= 4:
        want = struct.unpack_from("<I", raw)[0]
        raw = zlib.decompress(raw[4:])
        if len(raw) != want:  # tolerated: some tools write sloppy sizes
            pass
    return raw


def _iter_records(data: bytes):
    """Linearly yield (rtype, flags, formid, payload_pos, payload_size).

    GRUPs are descended into (their size includes the header, so simply
    skipping the 24-byte header keeps the scan linear).
    """
    pos, end = 0, len(data)
    while pos + REC_HEADER <= end:
        rtype = data[pos:pos + 4].decode("ascii", errors="replace")
        if rtype == "GRUP":
            pos += REC_HEADER
            continue
        size, flags, formid = struct.unpack_from("<III", data, pos + 4)
        yield rtype, flags, formid, pos + REC_HEADER, size
        pos += REC_HEADER + size


class _Info:
    __slots__ = ("edid", "responses", "prompt", "speaker_fid", "conditions")

    def __init__(self):
        self.edid = ""
        self.responses: list[str] = []   # NAM1 texts
        self.prompt: str | None = None   # RNAM text
        self.speaker_fid: int | None = None  # ANAM
        self.conditions: list[tuple] = []    # (type, compval, func, param1)


class PluginScan:
    """One parsed plugin: masters, gender sources and dialogue texts."""

    def __init__(self, path: Path, records_only: bool = False):
        self.path = Path(path)
        self.masters: list[str] = []
        self.localized = False
        self.vtyp_female: dict[int, bool] = {}
        self.flst_members: dict[int, list[int]] = {}
        # fid -> (is_female, use_traits, template_fid|None)
        self.npc_raw: dict[int, tuple[bool, bool, int | None]] = {}
        self.infos: list[tuple[str, _Info]] = []   # (dial_edid, info)
        self.texts: list[tuple[str, str, str, str]] = []  # (rtype, stype, text, edid)
        self._scan(records_only)

    # ---- scanning --------------------------------------------------------
    def _scan(self, records_only: bool) -> None:
        data = self.path.read_bytes()
        if data[:4] != b"TES4":
            raise PluginError(f"not a plugin file: {self.path}")
        tes4_size, tes4_flags = struct.unpack_from("<II", data, 4)
        self.localized = bool(tes4_flags & FLAG_LOCALIZED)
        for stype, sub in _iter_subrecords(
                _record_payload(data, REC_HEADER, tes4_size, tes4_flags)):
            if stype == "MAST":
                self.masters.append(_decode(sub).lower())
        if self.localized and not records_only:
            # strings are numeric IDs, nothing to text-match. records_only
            # scans continue: VTYP flags, FLST members and NPC_ ACBS are
            # binary fields, readable even in localized masters.
            return

        wanted = {"VTYP", "FLST", "NPC_"}
        cur_dial_edid = ""
        for rtype, flags, formid, ppos, psize in _iter_records(data):
            if rtype == "TES4":
                continue
            if records_only and rtype not in wanted:
                continue
            if rtype == "VTYP":
                self._scan_vtyp(formid, _record_payload(data, ppos, psize, flags))
            elif rtype == "FLST":
                self._scan_flst(formid, _record_payload(data, ppos, psize, flags))
            elif rtype == "NPC_":
                self._scan_npc(formid, _record_payload(data, ppos, psize, flags))
            elif records_only:
                continue
            elif rtype == "DIAL":
                cur_dial_edid = self._scan_dial(
                    _record_payload(data, ppos, psize, flags))
            elif rtype == "INFO":
                self._scan_info(cur_dial_edid,
                                _record_payload(data, ppos, psize, flags))
            else:
                self._scan_generic(rtype, _record_payload(data, ppos, psize, flags))

    def _scan_vtyp(self, fid: int, payload: bytes) -> None:
        female = False
        for stype, sub in _iter_subrecords(payload):
            if stype == "DNAM" and sub:
                female = bool(sub[0] & 0x02)
        self.vtyp_female[fid] = female

    def _scan_flst(self, fid: int, payload: bytes) -> None:
        members = []
        for stype, sub in _iter_subrecords(payload):
            if stype == "LNAM" and len(sub) >= 4:
                members.append(struct.unpack_from("<I", sub)[0])
        self.flst_members[fid] = members

    def _scan_npc(self, fid: int, payload: bytes) -> None:
        female = use_traits = False
        template = None
        edid = ""
        for stype, sub in _iter_subrecords(payload):
            if stype == "EDID":
                edid = _decode(sub)
            elif stype == "ACBS" and len(sub) >= 20:
                female = bool(struct.unpack_from("<I", sub)[0] & 0x01)
                use_traits = bool(struct.unpack_from("<H", sub, 18)[0] & 0x01)
            elif stype == "TPLT" and len(sub) >= 4:
                template = struct.unpack_from("<I", sub)[0]
            elif stype in ("FULL", "SHRT") and not self.localized:
                self.texts.append(("NPC_", stype, _decode(sub), edid))
        self.npc_raw[fid] = (female, use_traits, template)

    def _scan_dial(self, payload: bytes) -> str:
        edid = ""
        for stype, sub in _iter_subrecords(payload):
            if stype == "EDID":
                edid = _decode(sub)
            elif stype == "FULL":
                self.texts.append(("DIAL", "FULL", _decode(sub), edid))
        return edid

    def _scan_info(self, dial_edid: str, payload: bytes) -> None:
        info = _Info()
        for stype, sub in _iter_subrecords(payload):
            if stype == "EDID":
                info.edid = _decode(sub)
            elif stype == "NAM1":
                info.responses.append(_decode(sub))
            elif stype == "RNAM":
                info.prompt = _decode(sub)
            elif stype == "ANAM" and len(sub) >= 4:
                info.speaker_fid = struct.unpack_from("<I", sub)[0]
            elif stype == "CTDA" and len(sub) >= 24:
                ctype = sub[0]
                compval = struct.unpack_from("<f", sub, 4)[0]
                func = struct.unpack_from("<H", sub, 8)[0]
                param1 = struct.unpack_from("<I", sub, 12)[0]
                run_on = struct.unpack_from("<I", sub, 20)[0] if len(sub) >= 24 else 0
                if run_on == 0 and not ctype & CTDA_FLAG_SWAP_SUBJECT:
                    info.conditions.append((ctype, compval, func, param1))
        self.infos.append((dial_edid, info))

    def _scan_generic(self, rtype: str, payload: bytes) -> None:
        edid = ""
        for stype, sub in _iter_subrecords(payload):
            if stype == "EDID":
                edid = _decode(sub)
            elif (rtype, stype) in _KIND_MAP or stype in _GENERIC_NAME_SUBS:
                self.texts.append((rtype, stype, _decode(sub), edid))

    # ---- formid resolution ----------------------------------------------
    def owner_of(self, fid: int) -> tuple[str | None, int]:
        """-> (master_name|None, masked_fid); None master = this file."""
        idx = fid >> 24
        if idx < len(self.masters):
            return self.masters[idx], fid & 0x00FFFFFF
        return None, fid


# --------------------------------------------------------------------- masters
class MasterStore:
    """Lazily scanned VTYP/FLST/NPC_ gender data for master plugins.

    Masters are searched in `search_dirs` (the target plugin's folder first,
    then the game Data folder). Scans are cached as JSON in `cache_dir`
    because walking Skyrim.esm takes a few seconds. A bundled snapshot of the
    vanilla voice types (data/vanilla_voicetypes.json) serves as fallback
    when the esm itself cannot be found.
    """

    def __init__(self, search_dirs: list[Path], cache_dir: Path | None):
        self.search_dirs = [Path(d) for d in search_dirs if d]
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._loaded: dict[str, dict | None] = {}
        self._fallback: dict | None = None

    # data shape per master: {"vtyp": {fid: bool}, "flst": {fid: [genders]},
    #                         "npc": {fid: "female"|"male"|None}}
    def _fallback_data(self, name: str) -> dict | None:
        if self._fallback is None:
            p = Path(__file__).parent / "data" / "vanilla_voicetypes.json"
            try:
                self._fallback = json.loads(p.read_text(encoding="utf-8"))
            except OSError:
                self._fallback = {}
        d = self._fallback.get(name)
        if d is None:
            return None
        return {
            "vtyp": {int(k): v for k, v in d.get("vtyp", {}).items()},
            "flst": {int(k): v for k, v in d.get("flst", {}).items()},
            "npc": {},
        }

    def _find(self, name: str) -> Path | None:
        for d in self.search_dirs:
            p = d / name
            if p.is_file():
                return p
        return None

    def _cache_path(self, src: Path) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / (src.name.lower() + ".espmeta.json")

    def _build(self, name: str, src: Path) -> dict:
        # keys are stored masked (fid & 0xFFFFFF) because that is how a
        # dependent plugin's references look after owner_of() strips the
        # master index byte (which differs per file)
        scan = PluginScan(src, records_only=True)
        npc: dict[int, str | None] = {}
        for fid, (female, use_traits, template) in scan.npc_raw.items():
            # traits inherited from a template (possibly a leveled list) -
            # resolving those is out of scope, mark as unknown
            npc[fid & 0xFFFFFF] = None if (use_traits and template) else (
                "female" if female else "male")
        flst: dict[int, list[str]] = {}
        for fid, members in scan.flst_members.items():
            genders = set()
            for m in members:
                owner, masked = scan.owner_of(m)
                if owner is None:
                    if m in scan.vtyp_female:
                        genders.add("female" if scan.vtyp_female[m] else "male")
                else:
                    parent = self.load(owner)
                    if parent and masked in parent["vtyp"]:
                        genders.add("female" if parent["vtyp"][masked] else "male")
            flst[fid & 0xFFFFFF] = sorted(genders)
        return {"vtyp": {f & 0xFFFFFF: v for f, v in scan.vtyp_female.items()},
                "flst": flst, "npc": npc}

    def load(self, name: str) -> dict | None:
        name = name.lower()
        if name in self._loaded:
            return self._loaded[name]
        self._loaded[name] = None  # guard against master cycles
        src = self._find(name)
        if src is None:
            data = self._fallback_data(name)
            self._loaded[name] = data
            return data
        cache = self._cache_path(src)
        stat = src.stat()
        if cache and cache.is_file():
            try:
                c = json.loads(cache.read_text(encoding="utf-8"))
                if (c.get("v") == _CACHE_VERSION and c.get("size") == stat.st_size
                        and c.get("mtime") == int(stat.st_mtime)):
                    data = {
                        "vtyp": {int(k): v for k, v in c["vtyp"].items()},
                        "flst": {int(k): v for k, v in c["flst"].items()},
                        "npc": {int(k): v for k, v in c["npc"].items()},
                    }
                    self._loaded[name] = data
                    return data
            except (OSError, ValueError, KeyError):
                pass
        data = self._build(name, src)
        self._loaded[name] = data
        if cache:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({
                    "v": _CACHE_VERSION, "size": stat.st_size,
                    "mtime": int(stat.st_mtime),
                    "vtyp": {str(k): v for k, v in data["vtyp"].items()},
                    "flst": {str(k): v for k, v in data["flst"].items()},
                    "npc": {str(k): v for k, v in data["npc"].items()},
                }, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return data


# ------------------------------------------------------------------ inference
def _condition_is_positive(ctype: int, compval: float) -> bool | None:
    """Does this condition require the check to be TRUE? None = can't tell."""
    if ctype & CTDA_FLAG_USE_GLOBAL:
        return None
    op = ctype >> 5
    if op == 0:   # ==
        return compval >= 0.5
    if op == 1:   # !=
        return compval < 0.5
    if op in (2, 3):   # > / >=
        return True
    if op in (4, 5):   # < / <=
        return False
    return None


def _gender_of_vtyp_ref(scan: PluginScan, store: MasterStore, fid: int) -> set[str]:
    owner, masked = scan.owner_of(fid)
    if owner is None:
        if fid in scan.vtyp_female:
            return {"female" if scan.vtyp_female[fid] else "male"}
        members = scan.flst_members.get(fid)
        if members is not None:
            out: set[str] = set()
            for m in members:
                out |= _gender_of_vtyp_ref(scan, store, m)
            return out
        return set()
    data = store.load(owner)
    if not data:
        return set()
    if masked in data["vtyp"]:
        return {"female" if data["vtyp"][masked] else "male"}
    if masked in data["flst"]:
        return set(data["flst"][masked])
    return set()


def _gender_of_npc(scan: PluginScan, store: MasterStore, fid: int) -> set[str]:
    seen = set()
    while fid is not None and fid not in seen:
        seen.add(fid)
        owner, masked = scan.owner_of(fid)
        if owner is None:
            raw = scan.npc_raw.get(fid)
            if raw is None:
                return set()
            female, use_traits, template = raw
            if use_traits and template:
                fid = template
                continue
            return {"female" if female else "male"}
        data = store.load(owner)
        if not data:
            return set()
        g = data["npc"].get(masked)
        return {g} if g else set()
    return set()


def _speaker_of_info(scan: PluginScan, store: MasterStore, info: _Info) -> set[str]:
    genders: set[str] = set()
    for ctype, compval, func, param1 in info.conditions:
        positive = _condition_is_positive(ctype, compval)
        if positive is None:
            continue
        if func == FUNC_GET_IS_SEX:
            sex = "female" if param1 == 1 else "male"
            if not positive:
                sex = "male" if sex == "female" else "female"
            genders.add(sex)
        elif func == FUNC_GET_IS_VOICE_TYPE and positive:
            genders |= _gender_of_vtyp_ref(scan, store, param1)
        elif func == FUNC_GET_IS_ID and positive \
                and not ctype & CTDA_FLAG_USE_ALIASES:
            genders |= _gender_of_npc(scan, store, param1)
    if info.speaker_fid:
        genders |= _gender_of_npc(scan, store, info.speaker_fid)
    return genders


# ------------------------------------------------------------------ public API
def collect_meta(files: list[Path | str], data_dir: str | Path | None = None,
                 cache_dir: str | Path | None = None) -> dict[str, dict]:
    """text -> {"kind": ..., "speaker": ..., "edid": ...} for plugin files.

    speaker is only attached to npc_line entries: female / male / mixed
    (several genders can say the same line) / unknown (no usable evidence -
    fall back to per-mod knowledge). Lookups should try the exact captured
    string first, then a whitespace-stripped fallback.
    """
    agg: dict[str, dict] = {}

    def add(text: str, kind: str, speakers: set[str], edid: str) -> None:
        if not text or not text.strip():
            return
        e = agg.setdefault(text, {"kinds": Counter(), "speakers": set(), "edid": ""})
        e["kinds"][kind] += 1
        e["speakers"] |= speakers
        if edid and not e["edid"]:
            e["edid"] = edid

    for f in files:
        f = Path(f)
        if f.suffix.lower() not in (".esp", ".esm", ".esl"):
            continue
        scan = PluginScan(f)
        if scan.localized:
            continue
        store = MasterStore([f.parent, data_dir] if data_dir else [f.parent],
                            cache_dir)
        for rtype, stype, text, edid in scan.texts:
            kind = _KIND_MAP.get((rtype, stype))
            if kind is None and stype in _GENERIC_NAME_SUBS:
                kind = "name"
            add(text, kind or "other", set(), edid)
        for dial_edid, info in scan.infos:
            speakers = None
            edid = info.edid or dial_edid
            if info.prompt is not None:
                add(info.prompt, "player_line", set(), edid)
            for resp in info.responses:
                if speakers is None:  # resolve once per INFO
                    speakers = _speaker_of_info(scan, store, info)
                add(resp, "npc_line", speakers, edid)

    out: dict[str, dict] = {}
    for text, e in agg.items():
        kind = next((k for k in _KIND_PRIORITY if k in e["kinds"]), "other")
        meta = {"kind": kind}
        if kind == "npc_line":
            s = e["speakers"]
            meta["speaker"] = (sorted(s)[0] if len(s) == 1
                               else "mixed" if len(s) > 1 else "unknown")
        if e["edid"]:
            meta["edid"] = e["edid"][:64]
        out[text] = meta
    return out


def build_lookup(meta: dict[str, dict]):
    """-> function(captured_text) -> meta|None with stripped-text fallback."""
    stripped = {}
    for text, m in meta.items():
        stripped.setdefault(text.strip(), m)

    def lookup(s: str):
        hit = meta.get(s)
        if hit is None:
            hit = stripped.get(s.strip())
        return hit

    return lookup
