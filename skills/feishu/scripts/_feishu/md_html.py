"""Bounded Markdown → HTML/plain converter for Feishu docx paste.

`doc replace` pastes into the Feishu web editor.  Plain-text paste relied on
the editor's markdown *typing* heuristics and broke tables/code fences; the
editor's *paste* handler honors `text/html` clipboard content the same way a
copy from a web page does.  This module converts the GFM subset we support
into the exact HTML shapes Feishu recognizes from web copies.

Design rules (see references/docx-format.md):
- stdlib only — the venv bootstrap never re-installs into an existing venv,
  so a pip dependency would silently be missing on deployed machines.
- Raw HTML in the source is escaped, never passed through.
- Images degrade to alt-text placeholders (paste/import cannot upload bytes).
- Every construct also produces a plain-text rendering used both as the
  text/plain clipboard flavor and as the verification baseline.
"""

from __future__ import annotations

import html
import re

__all__ = ["markdown_to_html", "strip_frontmatter", "construct_texts", "wrap_clipboard_html", "SUPPORTED_LANG_HINT"]

# Feishu code blocks carry lowercase language labels; keep tags lowercase.
SUPPORTED_LANG_HINT = "语言标签请用小写(如 ```python),未知语言留空"

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*\n?", re.S)
_FENCE_RE = re.compile(r"^( {0,3})(```+|~~~+)\s*([A-Za-z0-9_+.#-]*)\s*$")
_HEADING_RE = re.compile(r"^( {0,3})(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^ {0,3}((\*\s*){3,}|(-\s*){3,}|(_\s*){3,})$")
_QUOTE_RE = re.compile(r"^ {0,3}>\s?(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)\d{1,9}[.)]\s+(.*)$")
_TASK_RE = re.compile(r"^\[( |x|X)\]\s+(.*)$")
_TABLE_DELIM_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

_CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1", re.S)
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__", re.S)
_DEL_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.S)
_EM_RE = re.compile(r"\*(?=\S)([^*]+?)(?<=\S)\*|_(?=\S)([^_]+?)(?<=\S)_", re.S)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((\s*<?[^)\s>]+>?(?:\s+\"[^\"]*\")?\s*)\)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((\s*<?[^)\s>]+>?)\s*\)")
_AUTOLINK_RE = re.compile(r"<((?:https?|ftp)://[^>\s]+|[^>\s@]+@[^>\s@]+\.[^>\s@]+)>")
_BARE_URL_RE = re.compile(r"(?<![\"'=\]])(https?://[^\s<>\]\)\"']+)")


def strip_frontmatter(md: str) -> str:
    """Remove a leading YAML frontmatter block; Feishu has no such concept."""
    return _FRONTMATTER_RE.sub("", md, count=1)


# ---------------------------------------------------------------- inline ----

def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _inline(text: str) -> str:
    """Escaped inline HTML for one logical line's text."""
    if not text:
        return ""
    codes: list[str] = []

    def stash_code(match: re.Match) -> str:
        codes.append(match.group(2))
        return f"\x00C{len(codes) - 1}\x00"

    work = _CODE_SPAN_RE.sub(stash_code, text)
    work = _escape(work)

    def repl_link(match: re.Match) -> str:
        label, target = match.group(1), match.group(2).strip()
        target = target.strip("<>").split(None, 1)[0]  # drop optional "title"
        if not re.match(r"^(https?://|mailto:)", target):
            return label  # relative/anchor links would 404 inside Feishu
        return f'<a href="{html.escape(target, quote=True)}">{label}</a>'

    def repl_image(match: re.Match) -> str:
        alt = match.group(1).strip() or "图片"
        return f"[图片:{alt}]"  # paste cannot upload bytes; keep a visible trace

    work = _IMAGE_RE.sub(repl_image, work)
    work = _LINK_RE.sub(repl_link, work)

    def repl_autolink(match: re.Match) -> str:
        body = match.group(1)
        href = body if "://" in body else f"mailto:{body}"
        return f'<a href="{html.escape(href, quote=True)}">{body}</a>'

    work = _AUTOLINK_RE.sub(repl_autolink, work)
    work = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", work)
    work = _DEL_RE.sub(r"<del>\1</del>", work)
    work = _EM_RE.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", work)

    def repl_bare(match: re.Match) -> str:
        url = match.group(1).rstrip(".,;:!?)\"'。，；：！？）】》」")
        tail = match.group(1)[len(url):]
        return f'<a href="{html.escape(url, quote=True)}">{url}</a>{tail}'

    work = _BARE_URL_RE.sub(repl_bare, work)

    def restore_code(match: re.Match) -> str:
        return f"<code>{_escape(codes[int(match.group(1))])}</code>"

    return re.sub("\x00C(\\d+)\x00", restore_code, work)


def _plain_inline(text: str) -> str:
    """Text with all markdown markers removed (for text/plain + verification)."""
    work = _CODE_SPAN_RE.sub(lambda m: m.group(2), text)
    work = _IMAGE_RE.sub(lambda m: m.group(1).strip() or "图片", work)
    work = _LINK_RE.sub(lambda m: m.group(1), work)
    work = _AUTOLINK_RE.sub(lambda m: m.group(1), work)
    work = _BOLD_RE.sub(lambda m: m.group(1) or m.group(2), work)
    work = _DEL_RE.sub(r"\1", work)
    work = _EM_RE.sub(lambda m: m.group(1) or m.group(2), work)
    return work


# ----------------------------------------------------------------- block ----

def _is_table_start(lines: list[str], i: int) -> bool:
    if i + 1 >= len(lines):
        return False
    head, delim = lines[i], lines[i + 1]
    return "|" in head and bool(_TABLE_DELIM_RE.match(delim))


def _split_row(line: str) -> list[str]:
    """Split a pipe row; escaped \\| and pipes inside code spans stay literal."""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    cells: list[str] = []
    buf: list[str] = []
    in_code = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    cells.append("".join(buf))
    return cells


# Feishu's own clipboard table shape (captured 2026-09-10 from a docx copy):
# <table class="ace-table" data-ace-table-col-widths="200;…" style="…fixed…">
# with <colgroup>, all rows as <tr><td colspan=1 rowspan=1 style="border:1px
# solid rgb(222,224,227);…"><div class="ace-line">…</div></td></tr> — no thead.
_TABLE_STYLE = "border:none;border-collapse:collapse;table-layout:fixed;width:{width}px;"
_CELL_STYLE = "border:1px solid rgb(222, 224, 227);font-size:10pt;padding:8px;vertical-align:top;"


def _flush_table(rows: list[list[str]], html_parts: list[str], plain_parts: list[str]) -> None:
    if not rows:
        return
    cols = max(len(row) for row in rows)
    widths = ";".join(["200"] * cols)
    out = [
        f'<table class="ace-table" data-ace-table-col-widths="{widths}" '
        f'style="{_TABLE_STYLE.format(width=200 * cols)}">',
        "<colgroup>" + "".join('<col width="200">' for _ in range(cols)) + "</colgroup>",
        "<tbody>",
    ]
    for row in rows:
        out.append('<tr style="height:39px;">')
        for cell in row:
            out.append(
                f'<td colspan="1" rowspan="1" style="{_CELL_STYLE}">'
                f'<div style="text-align:left" class="ace-line">{_inline(cell.strip())}</div></td>'
            )
        out.append("</tr>")
        plain_parts.append("  ".join(_plain_inline(c.strip()) for c in row))
    out.append("</tbody></table>")
    html_parts.append("".join(out))


def _flush_list(items: list[tuple[int, bool, str, str]], html_parts: list[str], plain_parts: list[str]) -> None:
    """items: (indent_level, ordered, task_marker_or_'', text)."""
    if not items:
        return
    min_indent = min(ind for ind, *_ in items)
    out: list[str] = []
    stack: list[tuple[int, str]] = []  # (indent, tag)

    def close_to(indent: int) -> None:
        while stack and stack[-1][0] >= indent:
            out.append(f"</{stack.pop()[1]}>")

    for indent, ordered, task, text in items:
        level = max(0, (indent - min_indent) // 2)
        tag = "ol" if ordered else "ul"
        content = _inline(text)
        if task:
            checked = " checked" if task.lower() == "x" else ""
            content = f'<input type="checkbox" disabled{checked}> ' + content
        if not stack:
            stack.append((level, tag))
            out.append(f"<{tag}>")
        elif level > stack[-1][0]:
            stack.append((level, tag))
            out.append(f"<{tag}>")
        elif level < stack[-1][0] or stack[-1][1] != tag:
            close_to(level + 1)
            if not stack or stack[-1][0] != level or stack[-1][1] != tag:
                stack.append((level, tag))
                out.append(f"<{tag}>")
        out.append(f"<li>{content}</li>")
    close_to(0)
    html_parts.append("".join(out))
    for _, _, task, text in items:
        mark = "[x] " if task.lower() == "x" else ("[ ] " if task else "")
        plain_parts.append(mark + _plain_inline(text))


def wrap_clipboard_html(body: str) -> str:
    """Wrap body HTML the way a real browser copy puts it on the clipboard.

    Feishu's paste parser expects the full-document skeleton with fragment
    markers; a bare fragment loses table conversion (verified 2026-09-10).
    """
    return (
        "<html><body><!--StartFragment-->"
        + body
        + "<!--EndFragment--></body></html>"
    )


def markdown_to_html(md: str) -> tuple[str, str]:
    """(html, plain) for the supported GFM subset. Never raises on bad input."""
    md = strip_frontmatter(md.replace("\r\n", "\n").replace("\r", "\n"))
    lines = md.split("\n")
    html_parts: list[str] = []
    plain_parts: list[str] = []
    para: list[str] = []

    def flush_para() -> None:
        if not para:
            return
        joined = " ".join(line.strip() for line in para)
        html_parts.append(f"<p>{_inline(joined)}</p>")
        plain_parts.append(_plain_inline(joined))
        para.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        fence = _FENCE_RE.match(line)
        if fence:
            flush_para()
            lang = fence.group(3).lower()
            body: list[str] = []
            marker = fence.group(2)
            i += 1
            while i < len(lines) and not re.match(rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}\s*$", lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # consume closing fence (or EOF)
            cls = f' class="language-{lang}"' if lang else ""
            html_parts.append(f"<pre><code{cls}>{_escape(chr(10).join(body))}</code></pre>")
            plain_parts.extend(body)
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            flush_para()
            level = len(heading.group(2))
            html_parts.append(f"<h{level}>{_inline(heading.group(3))}</h{level}>")
            plain_parts.append(_plain_inline(heading.group(3)))
            i += 1
            continue

        if _HR_RE.match(line):
            flush_para()
            html_parts.append("<hr>")
            i += 1
            continue

        if _is_table_start(lines, i):
            flush_para()
            rows = [_split_row(line)]
            i += 2  # skip header + delimiter
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1
            _flush_table(rows, html_parts, plain_parts)
            continue

        quote = _QUOTE_RE.match(line)
        if quote:
            flush_para()
            body = []
            while i < len(lines):
                q = _QUOTE_RE.match(lines[i])
                if not q:
                    break
                body.append(q.group(1))
                i += 1
            inner = " ".join(b.strip() for b in body if b.strip())
            html_parts.append(f"<blockquote><p>{_inline(inner)}</p></blockquote>")
            plain_parts.append(_plain_inline(inner))
            continue

        bullet = _BULLET_RE.match(line)
        ordered = _ORDERED_RE.match(line)
        if bullet or ordered:
            flush_para()
            items: list[tuple[int, bool, str, str]] = []
            while i < len(lines):
                b = _BULLET_RE.match(lines[i])
                o = _ORDERED_RE.match(lines[i])
                if not (b or o):
                    break
                match = b or o
                indent = len(match.group(1).replace("\t", "    "))
                text = match.group(2)
                task = ""
                t = _TASK_RE.match(text)
                if b and t:
                    task, text = t.group(1), t.group(2)
                items.append((indent, bool(o), task, text))
                i += 1
            _flush_list(items, html_parts, plain_parts)
            continue

        if not stripped:
            flush_para()
            i += 1
            continue

        para.append(line)
        i += 1

    flush_para()
    html_out = "\n".join(html_parts)
    plain_out = "\n".join(part for part in plain_parts if part.strip())
    return html_out, plain_out


def construct_texts(md: str) -> dict[str, list[str]]:
    """Code-fence body lines and table cell texts, for post-paste verification.

    These constructs are exactly what plain-text paste used to mangle, so the
    verifier checks each one survives as content in the re-exported document.
    """
    md = strip_frontmatter(md.replace("\r\n", "\n").replace("\r", "\n"))
    lines = md.split("\n")
    code_lines: list[str] = []
    cells: list[str] = []
    i = 0
    while i < len(lines):
        if _FENCE_RE.match(lines[i]):
            marker = _FENCE_RE.match(lines[i]).group(2)
            i += 1
            while i < len(lines) and not re.match(
                    rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}\s*$", lines[i]):
                if lines[i].strip():
                    code_lines.append(lines[i].strip())
                i += 1
            i += 1
            continue
        if _is_table_start(lines, i):
            for cell in _split_row(lines[i]):
                text = _plain_inline(cell.strip())
                if text.strip():
                    cells.append(text.strip())
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                for cell in _split_row(lines[i]):
                    text = _plain_inline(cell.strip())
                    if text.strip():
                        cells.append(text.strip())
                i += 1
            continue
        i += 1
    return {"code_lines": code_lines, "table_cells": cells}
