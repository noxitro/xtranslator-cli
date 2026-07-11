"""Wire protocol shared between xTranslator's OpenAI client and the fake API server.

xTranslator (TESVT_TranslatorApi.pas) sends one chat-completion request per string
when OpenAI_ArrayLimit=1 (set by `xtcli setup`):

    content = <OpenAI_Query pref> + CRLF + <payload>

The payload is a single source string in one of two encodings:
  - "tag":   internal line breaks replaced by '<L_F>' (clearCRLF, strings <= 3500 chars)
  - "raw":   literal CRLF line breaks (strings > 3500 chars, sent alone as "block2")
  - "plain": no line breaks at all

The reply must be choices[0].message.content, encoded the same way the payload was.
"""

from __future__ import annotations

CRLF_TAG = "<L_F>"

# Written into commonApiPrefs.ini as OpenAI_Query. xTranslator substitutes the
# %lang_*% placeholders, so the server can sanity-check the language pair too.
QUERY_PREF = "XTCLI-REQ::%lang_source%>%lang_dest%"
QUERY_PREFIX = "XTCLI-REQ::"

# Content of the sentinel MCM file loaded by the final batch rule. When this text
# arrives at the fake API server, every preceding rule has finished.
SENTINEL_KEY = "xtcli_sentinel"
SENTINEL_TEXT = "The xtcli sentinel batch has reached the final rule."

STYLE_PLAIN = "plain"
STYLE_TAG = "tag"
STYLE_RAW = "raw"


class WireError(ValueError):
    """Request did not match the expected xtcli wire format."""


def split_request(content: str) -> tuple[str, str]:
    """Split a request content into (query header, payload).

    Raises WireError when the sentinel query prefix is missing, which means the
    commonApiPrefs.ini overrides were not applied (setup problem).
    """
    header, sep, payload = content.partition("\r\n")
    if not sep:
        header, sep, payload = content.partition("\n")
    if not header.startswith(QUERY_PREFIX):
        raise WireError(
            f"request does not start with {QUERY_PREFIX!r} - "
            "OpenAI_Query override missing; re-run 'xtcli setup'"
        )
    if not sep:
        raise WireError("request has no payload after the query line")
    return header, payload


def decode_payload(payload: str) -> tuple[str, str]:
    """Return (logical text with \\n line breaks, encoding style)."""
    if "\r\n" in payload or "\n" in payload:
        text = payload.replace("\r\n", "\n")
        return text, STYLE_RAW
    if CRLF_TAG in payload:
        return payload.replace(CRLF_TAG, "\n"), STYLE_TAG
    return payload, STYLE_PLAIN


def encode_reply(text: str, style: str) -> str:
    """Encode a logical string back into the wire style of the request."""
    text = text.replace("\r\n", "\n")
    if style == STYLE_RAW:
        return text.replace("\n", "\r\n")
    return text.replace("\n", CRLF_TAG)
