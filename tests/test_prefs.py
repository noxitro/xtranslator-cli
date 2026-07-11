from pathlib import Path

from xtcli import prefs
from xtcli.config import Config


def make_cfg(tmp_path: Path) -> Config:
    exe = tmp_path / "xTranslator.exe"
    exe.write_bytes(b"MZ")
    (tmp_path / "Misc").mkdir()
    (tmp_path / "UserPrefs").mkdir()
    return Config(xtranslator_exe=str(exe))


def test_set_values_preserves_unrelated_lines_and_crlf(tmp_path):
    p = tmp_path / "f.ini"
    p.write_bytes(b"A=1\r\nOpenAI_Key=\r\nZ=9\r\n")
    prefs.set_values(p, {"openai_key": "xtcli-local", "New_Key": "v"})
    assert p.read_bytes() == b"A=1\r\nopenai_key=xtcli-local\r\nZ=9\r\nNew_Key=v\r\n"


def test_set_values_keeps_non_ascii_bytes_untouched(tmp_path):
    p = tmp_path / "f.ini"
    sjis_line = "Path=C:\\モッド\\".encode("cp932")
    p.write_bytes(sjis_line + b"\r\nOpenAI_Url=old\r\n")
    prefs.set_values(p, {"OpenAI_Url": "http://127.0.0.1:1/x"})
    data = p.read_bytes()
    assert sjis_line in data
    assert b"OpenAI_Url=http://127.0.0.1:1/x" in data


def test_apply_setup_and_restore(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.api_translator_txt.write_bytes(b"OpenAI_ArrayLimit=49\r\nOpenAI_enabled=true\r\n")
    cfg.common_prefs.write_bytes(b"UILang=Japanese\r\nArgsPref=0,3\r\n")
    prefs.apply_setup(cfg)

    assert prefs.get_value(cfg.api_translator_txt, "OpenAI_ArrayLimit") == "1"
    assert prefs.get_value(cfg.api_prefs, "OpenAI_Key") == "xtcli-local"
    assert prefs.get_value(cfg.api_prefs, "OpenAI_Url") == cfg.api_url
    assert prefs.get_value(cfg.common_prefs, "UILang") == "English"
    # every remembered-dialog slot pre-answered Yes(6); 16 slots total
    argspref = prefs.get_value(cfg.common_prefs, "ArgsPref").split(",")
    assert argspref == ["6"] * prefs.ASK_PREFS_SLOTS
    # first-launch language dialog suppressed in the game prefs
    assert prefs.get_value(cfg.game_prefs_ini, "isFirstLaunch2") == "0"

    prefs.restore_all(cfg)
    assert prefs.get_value(cfg.api_translator_txt, "OpenAI_ArrayLimit") == "49"
    assert prefs.get_value(cfg.common_prefs, "UILang") == "Japanese"


def test_apply_setup_creates_missing_api_prefs(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.api_translator_txt.write_bytes(b"OpenAI_enabled=true\r\n")
    prefs.apply_setup(cfg)
    assert cfg.api_prefs.is_file()
    assert prefs.get_value(cfg.api_prefs, "OpenAI_Query", ) == "XTCLI-REQ::%lang_source%>%lang_dest%"
