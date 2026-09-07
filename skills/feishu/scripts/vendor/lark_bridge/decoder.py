"""Text decoding for Feishu message protobuf content."""

import json
import re
from html.parser import HTMLParser

from google.protobuf.internal.decoder import _DecodeVarint

from lark_bridge.proto import proto_pb2 as pb
from lark_bridge._proto_utils import proto_to_dict


def decode_text(msg: dict) -> str:
    """Extract text from a message dict (with 'type' and 'content' fields)."""
    msg_type = msg.get("type", 0)
    content = msg.get("content", b"")
    if not content:
        return ""
    if msg_type == 4:
        return decode_type4(content)
    elif msg_type == 2:
        return decode_type2(content)
    elif msg_type == 14:
        return decode_type14(content)
    return ""


def decode_type4(content: bytes) -> str:
    """Decode type=4 (plain text) message.

    Primary: extract field 1 directly from wire format (newer Feishu encoding).
    Fallback: parse as TextContent proto and walk richText tree (legacy encoding).
    """
    # Primary: wire format field 1 = text content (varint tag 0x0a = field 1, wire type 2)
    text = _extract_wire_field1(content)
    if text:
        return text

    # Fallback: legacy richText tree
    try:
        tc = pb.TextContent()
        tc.ParseFromString(content)
        tc_dict = proto_to_dict(tc)
        rt = tc_dict.get("richText", {})
        elements = rt.get("elements", {}).get("dictionary", {})
        element_ids = rt.get("elementIds", [])
        at_ids = set(rt.get("atIds", []))

        def _extract(eid: str) -> str:
            el = elements.get(eid, {})
            prop = el.get("property", b"")
            parts: list[str] = []
            if prop:
                tp = pb.TextProperty()
                tp.ParseFromString(prop)
                if eid in at_ids:
                    parts.append(f"@({tp.content})")
                elif tp.content:
                    parts.append(tp.content)
            for cid in el.get("childIds", []):
                parts.append(_extract(cid))
            return "".join(parts)

        result = "".join(_extract(eid) for eid in element_ids)
        if result:
            return result
    except Exception:
        pass

    return ""


def _extract_wire_field1(data: bytes) -> str:
    """Extract field 1 (wire type 2 = length-delimited) as UTF-8 string from raw protobuf bytes."""
    try:
        pos = 0
        while pos < len(data):
            tag, new_pos = _DecodeVarint(data, pos)
            fn = tag >> 3
            wt = tag & 0x7
            if wt == 2:  # length-delimited
                length, new_pos = _DecodeVarint(data, new_pos)
                val = data[new_pos : new_pos + length]
                if fn == 1:
                    text = val.decode("utf-8", errors="replace")
                    # Sanity check: should look like actual text, not a nested proto
                    if text and not text.startswith("\x08") and not text.startswith("\x0a"):
                        return text
                pos = new_pos + length
            elif wt == 0:  # varint
                _, pos = _DecodeVarint(data, new_pos)
            elif wt == 5:  # 32-bit
                pos = new_pos + 4
            elif wt == 1:  # 64-bit
                pos = new_pos + 8
            else:
                break
    except Exception:
        pass
    return ""


def _iter_length_delimited_fields(data: bytes):
    """Yield ``(field_number, value)`` from a protobuf wire payload.

    Message type 2 stores its HTML body in a length-delimited field but also
    appends attachment metadata and binary image data.  A small bounded wire
    walker lets us select the body without trying to interpret those opaque
    fields as text.
    """
    pos = 0
    while pos < len(data):
        try:
            tag, next_pos = _DecodeVarint(data, pos)
            field_number, wire_type = tag >> 3, tag & 0x7
            if not field_number:
                return
            if wire_type == 0:
                _, pos = _DecodeVarint(data, next_pos)
            elif wire_type == 1:
                pos = next_pos + 8
            elif wire_type == 2:
                length, value_pos = _DecodeVarint(data, next_pos)
                end = value_pos + length
                if end > len(data):
                    return
                yield field_number, data[value_pos:end]
                pos = end
            elif wire_type == 5:
                pos = next_pos + 4
            else:
                return
            if pos > len(data):
                return
        except Exception:
            return


def _post_body(content: bytes) -> str:
    """Extract the HTML body from a type=2 post payload."""
    fields = list(_iter_length_delimited_fields(content))
    # Current web clients put the HTML in field 2.  The other candidates make
    # this tolerant of older/alternate envelopes without selecting binary
    # attachment fields by accident.
    for wanted in (2, 1, 20):
        for field_number, value in fields:
            if field_number != wanted or not value:
                continue
            try:
                candidate = value.decode("utf-8", errors="replace").replace("\x00", "")
            except Exception:
                continue
            if candidate.strip():
                return candidate

    # A few clients expose the body directly when the envelope is omitted.
    try:
        candidate = content.decode("utf-8", errors="replace").replace("\x00", "")
    except Exception:
        return ""
    if candidate.lstrip().startswith(("<", "{")):
        return candidate
    return ""


class _PostHTMLParser(HTMLParser):
    """Turn Feishu's small post-HTML dialect into readable plain text."""

    _BLOCK_TAGS = frozenset({
        "address", "article", "blockquote", "div", "figure", "h1", "h2",
        "h3", "h4", "h5", "h6", "header", "li", "ol", "p", "pre",
        "section", "table", "td", "th", "tr", "ul",
    })

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.mention_user_ids: list[str] = []

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attrs_map = {str(key).lower(): value or "" for key, value in attrs}
        if tag == "at":
            user_id = (
                attrs_map.get("user_id")
                or attrs_map.get("userid")
                or attrs_map.get("user_id_v2")
                or attrs_map.get("id")
            )
            if user_id:
                self.mention_user_ids.append(str(user_id))
        elif tag == "img":
            alt = attrs_map.get("alt", "").strip()
            self.parts.append(f"[图片: {alt}]" if alt else "[图片]")
        elif tag == "br":
            self._newline()
        elif tag in self._BLOCK_TAGS:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data.replace("\x00", ""))

    def text(self) -> str:
        raw = "".join(self.parts).replace("\r\n", "\n").replace("\r", "\n")
        lines = []
        for line in raw.split("\n"):
            line = re.sub(r"[ \t\f\v]+", " ", line).strip()
            if line or (lines and lines[-1]):
                lines.append(line)
        return "\n".join(lines).strip()


def decode_type2_content(content: bytes) -> tuple[str, list[str]]:
    """Decode a type=2 post into visible text and @ user ids."""
    html = _post_body(content)
    if not html:
        return "", []
    parser = _PostHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # HTMLParser is deliberately best-effort: a truncated post should
        # still expose any text and mentions parsed before the malformed tail.
        pass
    mentions = set(parser.mention_user_ids)
    mentions.update(
        re.findall(r"<at\b[^>]*\b(?:user_id|userid|userID)=['\"]([^'\"]+)", html, re.I)
    )
    return parser.text(), sorted(mentions)


def decode_type2(content: bytes) -> str:
    """Decode type=2 (rich post) message text."""
    return decode_type2_content(content)[0]


def decode_type14(content: bytes) -> str:
    """Decode type=14 (post/card) message."""
    try:
        title = ""
        json_body = ""
        pos = 0
        while pos < len(content):
            tag, new_pos = _DecodeVarint(content, pos)
            fn = tag >> 3
            wt = tag & 0x7
            if wt == 0:
                _, pos = _DecodeVarint(content, new_pos)
            elif wt == 2:
                length, pos = _DecodeVarint(content, new_pos)
                val = content[pos : pos + length]
                if fn == 20:
                    json_body = val.decode("utf-8", errors="replace")
                elif fn == 8:
                    try:
                        tp = 0
                        _, tp = _DecodeVarint(val, tp)
                        t_len, tp = _DecodeVarint(val, tp)
                        title = val[tp : tp + t_len].decode("utf-8", errors="replace")
                    except Exception:
                        pass
                pos += length
            else:
                break

        if json_body:
            body = json.loads(json_body)
            parts: list[str] = []
            for el in body.get("body", {}).get("elements", []):
                tag_name = el.get("tag", "")
                prop = el.get("property", {})
                if tag_name == "markdown":
                    for sub in prop.get("elements", []):
                        sub_tag = sub.get("tag", "")
                        sub_prop = sub.get("property", {})
                        if sub_tag == "plain_text":
                            parts.append(sub_prop.get("content", ""))
                        elif sub_tag == "br":
                            parts.append("\n")
                        elif sub_tag == "a":
                            parts.append(sub_prop.get("href", sub_prop.get("content", "")))
                        elif sub_tag == "at":
                            parts.append(f"@({sub_prop.get('userID', '')})")
                        else:
                            parts.append(sub_prop.get("content", ""))
                elif tag_name == "plain_text":
                    parts.append(prop.get("content", ""))
                elif tag_name == "br":
                    parts.append("\n")
            text = "".join(parts)
            if title:
                text = f"{title}\n{text}"
        else:
            # Fallback: protobuf tree traversal
            pos = 0
            _, pos = _DecodeVarint(content, pos)
            _, pos = _DecodeVarint(content, pos)
            _, pos = _DecodeVarint(content, pos)
            length, pos = _DecodeVarint(content, pos)
            rt = pb.RichText()
            rt.ParseFromString(content[pos : pos + length])
            rt_dict = proto_to_dict(rt)
            elems = rt_dict.get("elements", {}).get("dictionary", {})
            eids = rt_dict.get("elementIds", [])

            def _ext(eid: str) -> str:
                el = elems.get(eid, {})
                prop = el.get("property", b"")
                p: list[str] = []
                if prop:
                    tp = pb.TextProperty()
                    tp.ParseFromString(prop)
                    if tp.content:
                        p.append(tp.content)
                for cid in el.get("childIds", []):
                    p.append(_ext(cid))
                return "".join(p)

            text = "".join(_ext(eid) for eid in eids)
            if title:
                text = f"{title}\n{text}"

        return re.sub(r"\x1b\[[0-9;]*m", "", text)
    except Exception:
        return ""
