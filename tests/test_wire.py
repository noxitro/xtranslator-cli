import pytest

from xtcli import wire


def test_split_request_crlf():
    header, payload = wire.split_request("XTCLI-REQ::english>japanese\r\nIron Sword")
    assert header == "XTCLI-REQ::english>japanese"
    assert payload == "Iron Sword"


def test_split_request_multiline_payload():
    _, payload = wire.split_request("XTCLI-REQ::e>j\r\nline1\r\nline2")
    assert payload == "line1\r\nline2"


def test_split_request_rejects_default_query():
    with pytest.raises(wire.WireError):
        wire.split_request("Translate the following text to japanese:\r\nIron Sword")


def test_decode_plain():
    assert wire.decode_payload("Iron Sword") == ("Iron Sword", wire.STYLE_PLAIN)


def test_decode_tag():
    text, style = wire.decode_payload("line1<L_F>line2")
    assert text == "line1\nline2"
    assert style == wire.STYLE_TAG


def test_decode_raw():
    text, style = wire.decode_payload("line1\r\nline2")
    assert text == "line1\nline2"
    assert style == wire.STYLE_RAW


def test_encode_roundtrip_tag():
    text, style = wire.decode_payload("a<L_F>b")
    assert wire.encode_reply(text, style) == "a<L_F>b"


def test_encode_roundtrip_raw():
    text, style = wire.decode_payload("a\r\nb")
    assert wire.encode_reply(text, style) == "a\r\nb"


def test_encode_translation_newlines_match_style():
    assert wire.encode_reply("訳1\n訳2", wire.STYLE_TAG) == "訳1<L_F>訳2"
    assert wire.encode_reply("訳1\n訳2", wire.STYLE_RAW) == "訳1\r\n訳2"
    assert wire.encode_reply("一行", wire.STYLE_PLAIN) == "一行"
