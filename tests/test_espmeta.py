"""espmeta builds its input from raw record bytes, so the tests assemble
minimal but structurally correct plugins in memory (layouts match the
comments at the top of espmeta.py)."""

import os
import struct
import zlib

from xtcli import espmeta

FLAG_COMPRESSED = 0x00040000
FLAG_LOCALIZED = 0x00000080

GET_IS_SEX = 70
GET_IS_ID = 72
GET_IS_VOICE_TYPE = 426


def sub(stype: str, payload: bytes) -> bytes:
    return stype.encode() + struct.pack("<H", len(payload)) + payload


def zstr(s: str) -> bytes:
    return s.encode("cp1252") + b"\0"


def rec(rtype: str, formid: int, subs: list[bytes], flags: int = 0) -> bytes:
    payload = b"".join(subs)
    if flags & FLAG_COMPRESSED:
        payload = struct.pack("<I", len(payload)) + zlib.compress(payload)
    return (rtype.encode()
            + struct.pack("<IIIIHH", len(payload), flags, formid, 0, 44, 0)
            + payload)


def grup(content: bytes, label: bytes = b"\0\0\0\0", gtype: int = 7) -> bytes:
    return (b"GRUP" + struct.pack("<I", 24 + len(content)) + label
            + struct.pack("<iHHHH", gtype, 0, 0, 0, 0) + content)


def tes4(masters: tuple[str, ...] = (), flags: int = 0) -> bytes:
    subs = [sub("HEDR", struct.pack("<fII", 1.7, 0, 0x800))]
    for m in masters:
        subs.append(sub("MAST", zstr(m)))
        subs.append(sub("DATA", struct.pack("<Q", 0)))
    return rec("TES4", 0, subs, flags=flags)


def ctda(func: int, param1: int, op: int = 0, compval: float = 1.0,
         flags: int = 0, run_on: int = 0) -> bytes:
    t = (op << 5) | flags
    return sub("CTDA", struct.pack("<B3xfH2xIIIIi",
                                   t, compval, func, param1, 0, run_on, 0, -1))


def info(formid: int, responses: list[str], conditions: list[bytes] = (),
         prompt: str | None = None, speaker: int | None = None,
         edid: str = "") -> bytes:
    subs = []
    if edid:
        subs.append(sub("EDID", zstr(edid)))
    for r in responses:
        subs.append(sub("NAM1", zstr(r)))
    subs.extend(conditions)
    if prompt is not None:
        subs.append(sub("RNAM", zstr(prompt)))
    if speaker is not None:
        subs.append(sub("ANAM", struct.pack("<I", speaker)))
    return rec("INFO", formid, subs)


def dial(formid: int, edid: str, full: str | None = None) -> bytes:
    subs = [sub("EDID", zstr(edid))]
    if full is not None:
        subs.append(sub("FULL", zstr(full)))
    return rec("DIAL", formid, subs)


def write_plugin(path, body: bytes, masters: tuple[str, ...] = (),
                 flags: int = 0):
    path.write_bytes(tes4(masters, flags=flags) + body)
    return path


# --------------------------------------------------------------------- tests

def test_dialogue_kinds_and_sex_condition(tmp_path):
    body = grup(
        dial(0x800, "WenchTopic", full="Tell me about yourself.")
        + grup(info(0x801, ["I am just a humble wench."],
                    [ctda(GET_IS_SEX, 1)]))
    )
    p = write_plugin(tmp_path / "a.esp", body)
    meta = espmeta.collect_meta([p])
    assert meta["Tell me about yourself."]["kind"] == "player_line"
    line = meta["I am just a humble wench."]
    assert line["kind"] == "npc_line"
    assert line["speaker"] == "female"
    assert line["edid"] == "WenchTopic"


def test_sex_condition_negated_and_prompt(tmp_path):
    body = (
        info(0x801, ["Get lost."], [ctda(GET_IS_SEX, 1, op=0, compval=0.0)],
             prompt="What do you want?")
    )
    p = write_plugin(tmp_path / "a.esp", body)
    meta = espmeta.collect_meta([p])
    assert meta["Get lost."]["speaker"] == "male"  # GetIsSex Female == 0
    assert meta["What do you want?"]["kind"] == "player_line"


def test_voicetype_direct_and_formlist(tmp_path):
    body = (
        rec("VTYP", 0x900, [sub("EDID", zstr("FemaleSultry")),
                            sub("DNAM", b"\x03")])
        + rec("VTYP", 0x901, [sub("EDID", zstr("MaleBrute")),
                              sub("DNAM", b"\x01")])
        + rec("FLST", 0x910, [sub("EDID", zstr("FemaleVoices")),
                              sub("LNAM", struct.pack("<I", 0x900))])
        + info(0x801, ["Hey there, sweetie."],
               [ctda(GET_IS_VOICE_TYPE, 0x910)])
        + info(0x802, ["Out of my way."],
               [ctda(GET_IS_VOICE_TYPE, 0x901)])
    )
    p = write_plugin(tmp_path / "a.esp", body)
    meta = espmeta.collect_meta([p])
    assert meta["Hey there, sweetie."]["speaker"] == "female"
    assert meta["Out of my way."]["speaker"] == "male"


def test_anam_speaker_and_getisid(tmp_path):
    npc_female = rec("NPC_", 0xA00, [
        sub("EDID", zstr("MyWench")),
        sub("ACBS", struct.pack("<IhhhHHHHHHH", 0x01, 0, 0, 1, 0, 0, 100, 35, 0, 0, 0)),
    ])
    body = (
        npc_female
        + info(0x801, ["My master is waiting."], speaker=0xA00)
        + info(0x802, ["As you command."], [ctda(GET_IS_ID, 0xA00)])
    )
    p = write_plugin(tmp_path / "a.esp", body)
    meta = espmeta.collect_meta([p])
    assert meta["My master is waiting."]["speaker"] == "female"
    assert meta["As you command."]["speaker"] == "female"


def test_mixed_and_unknown_speakers(tmp_path):
    body = (
        info(0x801, ["Nice armor."], [ctda(GET_IS_SEX, 1)])
        + info(0x802, ["Nice armor."], [ctda(GET_IS_SEX, 0)])
        + info(0x803, ["Anyone could say this."])
    )
    p = write_plugin(tmp_path / "a.esp", body)
    meta = espmeta.collect_meta([p])
    assert meta["Nice armor."]["speaker"] == "mixed"
    assert meta["Anyone could say this."]["speaker"] == "unknown"


def test_conditions_on_target_are_ignored(tmp_path):
    # run_on=Target (1) and swap-subject conditions describe the listener,
    # not the speaker
    body = info(0x801, ["You look strong."], [
        ctda(GET_IS_SEX, 1, run_on=1),
        ctda(GET_IS_SEX, 1, flags=espmeta.CTDA_FLAG_SWAP_SUBJECT),
    ])
    p = write_plugin(tmp_path / "a.esp", body)
    meta = espmeta.collect_meta([p])
    assert meta["You look strong."]["speaker"] == "unknown"


def test_compressed_book_and_generic_name(tmp_path):
    body = (
        rec("BOOK", 0xB00, [sub("EDID", zstr("MyBook")),
                            sub("FULL", zstr("Lusty Argonian Maid")),
                            sub("DESC", zstr("Chapter One..."))],
            flags=FLAG_COMPRESSED)
        + rec("MISC", 0xB01, [sub("EDID", zstr("MyRing")),
                              sub("FULL", zstr("Gilded Ring"))])
    )
    p = write_plugin(tmp_path / "a.esp", body)
    meta = espmeta.collect_meta([p])
    assert meta["Chapter One..."]["kind"] == "book"
    assert meta["Lusty Argonian Maid"]["kind"] == "name"
    assert meta["Gilded Ring"]["kind"] == "name"


def test_npc_line_wins_kind_priority(tmp_path):
    body = (
        rec("MISC", 0xB01, [sub("FULL", zstr("Follow me."))])
        + info(0x801, ["Follow me."], [ctda(GET_IS_SEX, 1)])
    )
    p = write_plugin(tmp_path / "a.esp", body)
    meta = espmeta.collect_meta([p])
    assert meta["Follow me."]["kind"] == "npc_line"
    assert meta["Follow me."]["speaker"] == "female"


def test_localized_plugin_yields_nothing(tmp_path):
    body = info(0x801, ["ignored"])
    p = write_plugin(tmp_path / "a.esp", body, flags=FLAG_LOCALIZED)
    assert espmeta.collect_meta([p]) == {}


def test_master_voicetype_resolved_and_cached(tmp_path):
    master = write_plugin(
        tmp_path / "voices.esm",
        rec("VTYP", 0x123, [sub("EDID", zstr("FemaleShy")),
                            sub("DNAM", b"\x02")]),
    )
    body = info(0x801, ["Please be gentle."],
                [ctda(GET_IS_VOICE_TYPE, (0 << 24) | 0x123)])
    p = write_plugin(tmp_path / "a.esp", body, masters=("voices.esm",))
    cache = tmp_path / "cache"
    meta = espmeta.collect_meta([p], cache_dir=cache)
    assert meta["Please be gentle."]["speaker"] == "female"
    assert (cache / "voices.esm.espmeta.json").is_file()
    # second run must be served from the cache: overwrite the master with
    # garbage of identical size and mtime (the cache key) to prove it
    st = master.stat()
    master.write_bytes(b"\0" * st.st_size)
    os.utime(master, (st.st_atime, st.st_mtime))
    meta2 = espmeta.collect_meta([p], cache_dir=cache)
    assert meta2["Please be gentle."]["speaker"] == "female"


def test_build_lookup_strip_fallback():
    meta = {" Padded": {"kind": "name"}}
    lookup = espmeta.build_lookup(meta)
    assert lookup(" Padded") == {"kind": "name"}
    assert lookup("Padded") == {"kind": "name"}
    assert lookup("missing") is None
