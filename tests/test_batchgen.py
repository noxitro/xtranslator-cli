from pathlib import Path

from xtcli import batchgen, wire


def test_extract_script():
    script = batchgen.build_script(
        [Path(r"C:\mods\foo.esp")], "english", "japanese",
        finalize=False, sentinel_file=Path(r"C:\tmp\xtcli_sentinel_english.txt"),
    )
    assert "StartRule" in script and "EndRule" in script
    assert r"Command=loadfile:C:\mods\foo.esp" in script
    assert "Command=apitranslation:7:1" in script
    assert "Command=finalize" not in script
    assert "Command=closefile" in script
    # sentinel rule closes everything and always sends its string
    assert r"Command=loadfile:C:\tmp\xtcli_sentinel_english.txt" in script
    assert "Command=apitranslation:7:0" in script
    assert "Command=closeall" in script


def test_apply_script_has_finalize_and_export():
    script = batchgen.build_script(
        [Path(r"C:\mods\foo.esp")], "english", "japanese",
        ssts=[Path(r"C:\dic\old.sst")], finalize=True,
    )
    assert f"exportsubfolder={batchgen.EXPORT_SUBFOLDER}" in script
    assert r"Command=importsst:0:1:C:\dic\old.sst" in script
    assert script.index("importsst") < script.index("apitranslation")
    assert script.index("apitranslation") < script.index("finalize")


def test_sentinel_mcm_is_utf16_with_bom(tmp_path):
    p = batchgen.write_sentinel_mcm(tmp_path, "english")
    raw = p.read_bytes()
    assert raw.startswith(b"\xff\xfe")
    text = raw.decode("utf-16")
    assert text == f"${wire.SENTINEL_KEY}\t{wire.SENTINEL_TEXT}\r\n"
    assert p.name == "xtcli_sentinel_english.txt"
