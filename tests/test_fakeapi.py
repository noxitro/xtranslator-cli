"""Exercises the fake API server exactly the way xTranslator's OpenAI client does."""

import json
import urllib.request

import pytest

from xtcli import wire
from xtcli.fakeapi import FakeApiServer

QUERY = "XTCLI-REQ::english>japanese"


def post(port: int, payload: str) -> tuple[int, str]:
    body = json.dumps({
        "model": "xtcli",
        "messages": [{"role": "user", "content": QUERY + "\r\n" + payload}],
    }).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=body, headers={"Content-Type": "application/json",
                            "Authorization": "Bearer xtcli-local"})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            return resp.status, data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def test_capture_echoes_and_records():
    with FakeApiServer(0, "capture") as srv:
        code, reply = post(srv.port, "Iron Sword")
        assert code == 200 and reply == "Iron Sword"
        post(srv.port, "Chapter one<L_F>It was a dark night.")
        post(srv.port, "Iron Sword")  # duplicate
    assert list(srv.state.captured) == ["Iron Sword", "Chapter one\nIt was a dark night."]
    assert srv.state.n_requests == 3
    assert not srv.state.done.is_set()


def test_serve_returns_translation_in_same_style():
    table = {
        "Iron Sword": "鉄の剣",
        "Chapter one\nIt was a dark night.": "第一章\n暗い夜だった。",
    }
    with FakeApiServer(0, "serve", table) as srv:
        _, reply = post(srv.port, "Iron Sword")
        assert reply == "鉄の剣"
        _, reply = post(srv.port, "Chapter one<L_F>It was a dark night.")
        assert reply == "第一章<L_F>暗い夜だった。"
        _, reply = post(srv.port, "Chapter one\r\nIt was a dark night.")  # block2 style
        assert reply == "第一章\r\n暗い夜だった。"
        _, reply = post(srv.port, "Unknown String")
        assert reply == "Unknown String"
    assert list(srv.state.missing) == ["Unknown String"]
    assert srv.state.served["Iron Sword"] == "鉄の剣"


def test_sentinel_sets_done():
    with FakeApiServer(0, "capture") as srv:
        code, reply = post(srv.port, wire.SENTINEL_TEXT)
        assert code == 200 and reply == wire.SENTINEL_TEXT
        assert srv.state.done.is_set()
    assert wire.SENTINEL_TEXT not in srv.state.captured


def test_wrong_query_prefix_is_500():
    with FakeApiServer(0, "capture") as srv:
        body = json.dumps({"model": "m", "messages": [
            {"role": "user", "content": "Translate to japanese:\r\nIron Sword"}]}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/v1/chat/completions", data=body,
            headers={"Content-Type": "application/json"})
        with pytest.raises(Exception):
            urllib.request.urlopen(req)
    assert srv.state.errors
