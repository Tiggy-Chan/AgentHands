"""Drive/docs: list spaces, walk trees, resolve wiki, export documents.

All calls go through the vendored drive module (patched to _http) and are
covered by transport's REST allowlist. Export is the read-semantics POST.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import asyncio
import tempfile
from collections import deque

from . import auth, config, secure_store, transport
from .errors import FeishuError, ResourceIsolationUnavailable, SendBlocked
from .output import note

from lark_bridge import drive

_URL_RE = re.compile(
    r"https?://[^/]+/(?P<kind>docx|docs|wiki|sheets|file|folder)/(?P<token>[A-Za-z0-9_-]+)"
)
_EXPORTABLE = {
    "docx": ("docx", ["markdown", "docx", "pdf"]),
    "doc": ("doc", ["docx", "pdf"]),
    "wiki": (None, []),  # resolved first
    # 2026-09-10 实测:内网导出端点对 sheet 只认 xlsx,csv 报
    # "export file extension mismatch"(vendor docstring 里的 csv 是错的)。
    "sheets": ("sheet", ["xlsx"]),
    "sheet": ("sheet", ["xlsx"]),
}


def parse_doc_ref(ref: str) -> tuple[str, str]:
    """URL or bare token → (kind, token). kind ∈ docx|doc|wiki|sheet."""
    m = _URL_RE.search(ref.strip())
    if m:
        kind = m.group("kind")
        return ({"sheets": "sheet"}.get(kind, kind), m.group("token"))
    token = ref.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise FeishuError(f"认不出这个文档引用：{ref!r}", hint="支持 docx/wiki/sheets 链接或裸 token")
    # bare tokens: docx is the common case; export errors will say otherwise
    return "docx", token


def _creds():
    data = auth.load_cookies()
    cookies = data["cookies"]
    domain = data.get("domain") or config.DOMAIN
    return auth.cookie_header(cookies), cookies, domain


async def export_document(ref: str, fmt: str, fallback: bool = True) -> bytes:
    """→ file bytes. fmt ∈ markdown/docx/pdf/xlsx.

    With fallback=True (default) a failed markdown export degrades to a
    browser DOM innerText dump.  That dump has NO markdown structure, so the
    result is prefixed with an explicit loss marker and a stderr note; pass
    fallback=False (--strict) to re-raise instead.
    """
    cookie, cookies, domain = _creds()
    try:
        kind, token = parse_doc_ref(ref)
        if kind == "wiki":
            token, kind = await drive._resolve_wiki_token(cookie, cookies, token, domain)
            if not token:
                raise FeishuError("wiki 解析失败（无权限或 token 不对）")
        doc_type, formats = _EXPORTABLE.get(kind, (None, []))
        if not doc_type:
            raise FeishuError(f"不支持的文档类型：{kind}")
        if fmt not in formats:
            raise FeishuError(f"{kind} 不支持导出 {fmt}（支持：{', '.join(formats)}）")
        data = await drive.export_document(cookie, cookies, token, doc_type, fmt, domain)
        if data is None:
            raise FeishuError(
                f"导出失败（{kind} {token} → {fmt}）",
                hint="可能是无权限、类型不符或导出超时；stderr 上有上游日志",
            )
        _cache_export(ref, fmt, data)
        return data
    except Exception:
        if fmt == "markdown" and fallback:
            from .browser_worker import run
            res = await run("doc-read", {"ref": ref}, timeout=max(config.BROWSER_TIMEOUT_SECONDS, 60))
            title = res.get("title", "")
            content = res.get("content", "")
            note("! 服务端导出失败，已降级为浏览器 DOM 文本（丢失 markdown 结构）")
            marker = "<!-- feishu:export-fallback source=dom-innertext structure=lost -->\n"
            rendered = f"{marker}# {title}\n\n{content}\n".encode("utf-8")
            _cache_export(ref, fmt, rendered)
            return rendered
        raise


def _cache_export(ref: str, fmt: str, data: bytes) -> None:
    """Cache only explicitly requested document bodies, never during walk."""
    if fmt != "markdown":
        return
    try:
        kind, token = parse_doc_ref(ref)
        digest = hashlib.sha256(f"{kind}:{token}".encode()).hexdigest()[:32]
        config.ensure_home()
        config.DOCS_CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        aad = b"agenthands/feishu/docs/v2"
        if secure_store.mode() == "encrypted":
            secure_store.atomic_write(
                config.DOCS_CACHE_DIR / f"{digest}.md.enc",
                secure_store.encrypt_blob(data, aad=aad),
            )
        else:
            secure_store.atomic_write(config.DOCS_CACHE_DIR / f"{digest}.md", data)
    except Exception as exc:
        # Caching is an optimization; never turn a successful explicit export
        # into a failed user operation.
        note(f"  ! 文档缓存失败：{type(exc).__name__}")


async def list_spaces() -> dict:
    cookie, cookies, domain = _creds()
    result = {"my_space": [], "shared_space": [], "errors": [], "complete": True, "incomplete": False}
    for label, key, loader in (
        ("my_space", "my_space", drive.list_my_space),
        ("shared_space", "shared_space", drive.list_shared_folders),
    ):
        try:
            result[key] = await loader(cookie, cookies, domain)
        except Exception as exc:
            result["complete"] = False
            result["incomplete"] = True
            result["errors"].append({"space": label, "error": type(exc).__name__})
    return result


async def walk(max_nodes: int = 5000) -> dict:
    """BFS both spaces → nodes + errors. A 403 on one folder never aborts
    the walk, but the result is then honestly marked incomplete."""
    if not 1 <= max_nodes <= 50_000:
        raise ValueError("doc walk max-nodes 必须在 1..50000 之间")
    cookie, cookies, domain = _creds()
    roots = []
    root_errors: list[dict] = []
    for label, loader in (("my", drive.list_my_space), ("shared", drive.list_shared_folders)):
        loaded = None
        last_error = None
        for attempt in range(2):
            try:
                loaded = await loader(cookie, cookies, domain)
                break
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.5)
        if loaded is not None:
            roots.extend((n, label) for n in loaded)
        else:
            root_errors.append({"space": label, "error": type(last_error).__name__})
    seen: set[str] = set()
    queue = deque((r, space, "") for r, space in roots)
    nodes: list[dict] = []
    errors: list[dict] = root_errors
    while queue and len(nodes) < max_nodes:
        node, space, path = queue.popleft()
        tok = node.get("token", "")
        if not tok or tok in seen:
            continue
        seen.add(tok)
        entry = {**node, "space": space, "path": f"{path}/{node.get('name','')}"}
        nodes.append(entry)
        if node.get("type") == "folder":
            children = None
            last_error = None
            for attempt in range(2):
                try:
                    children = await drive.list_children(cookie, cookies, tok, domain)
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt == 0:
                        await asyncio.sleep(0.5)
            if children is not None:
                queue.extend((c, space, entry["path"]) for c in children)
            else:
                errors.append({"token": tok, "path": entry["path"], "error": type(last_error).__name__})
    config.ensure_home()
    config.DOCS_INDEX.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".docs-index.", dir=str(config.DOCS_INDEX.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for n in nodes:
                f.write(json.dumps(n, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, config.DOCS_INDEX)
        os.chmod(config.DOCS_INDEX, 0o600)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return {
        "nodes": nodes,
        "errors": errors,
        "complete": not errors and len(nodes) < max_nodes,
        "incomplete": bool(errors) or len(nodes) >= max_nodes,
        "truncated": len(nodes) >= max_nodes,
    }


def _read_doc_write_file(file_path: str) -> bytes:
    if not os.path.exists(file_path):
        raise FeishuError(f"本地文件不存在：{file_path}")
    if not os.path.isfile(file_path):
        raise FeishuError(f"指定路径不是普通文件：{file_path}")
    size = os.path.getsize(file_path)
    if size > config.MAX_DOC_WRITE_BYTES:
        raise FeishuError(
            f"文档文件超过写入上限：{size} bytes",
            hint=f"当前上限为 {config.MAX_DOC_WRITE_BYTES} bytes",
        )
    with open(file_path, "rb") as f:
        return f.read(config.MAX_DOC_WRITE_BYTES + 1)


def doc_write_fingerprint(file_name: str, folder_token: str, content_sha256: str) -> str:
    h = hashlib.sha256()
    h.update(file_name.encode())
    h.update(b"|")
    h.update(folder_token.encode())
    h.update(b"|")
    h.update(content_sha256.encode())
    return h.hexdigest()[:16]


def make_doc_write_draft(file_path: str, file_name: str, folder_token: str, content: bytes) -> dict:
    content_sha256 = hashlib.sha256(content).hexdigest()
    fp = doc_write_fingerprint(file_name, folder_token, content_sha256)
    return {
        "status": "draft_not_imported",
        "action": "import_document",
        "file_path": file_path,
        "file_name": file_name,
        "size": len(content),
        "target_folder_token": folder_token or "(我的空间根目录)",
        "sha256": content_sha256,
        "fingerprint": fp,
    }


async def import_file(
    file_path: str,
    title: str = "",
    folder: str = "",
    confirm: str = "",
    direct: bool = False,
) -> dict:
    content = _read_doc_write_file(file_path)

    base_name = os.path.basename(file_path)
    ext = os.path.splitext(base_name)[1].lstrip(".").lower() or "md"
    file_title = title.strip() or os.path.splitext(base_name)[0]
    upload_file_name = f"{file_title}.{ext}"

    # Target type: docx for md/docx/txt, sheet for xlsx/xls/csv
    target_type = "docx"
    if ext in ("xlsx", "xls", "csv"):
        target_type = "sheet"

    folder_token = ""
    if folder.strip():
        m = _URL_RE.search(folder.strip())
        folder_token = m.group("token") if m else folder.strip()

    draft = make_doc_write_draft(file_path, upload_file_name, folder_token, content)

    if direct:
        note("直接写入模式：本次文档导入已明确授权。")
    elif not confirm:
        note("未执行导入。确认内容无误后，把指纹填进 --confirm-write 并设 FEISHU_ALLOW_WRITE=1 再执行。")
        raise SendBlocked("draft only", hint=json.dumps(draft, ensure_ascii=False))
    elif confirm != draft["fingerprint"]:
        raise SendBlocked(
            "指纹不匹配，拒绝写入",
            hint=f"本次写入指纹是 {draft['fingerprint']}；文件或路径有任何改动都会变",
        )
    # 环境变量是唯一 agent 无法自己满足的那半闸门：--direct 只免指纹核对，不免它。
    if os.environ.get("FEISHU_ALLOW_WRITE") != "1":
        raise SendBlocked(
            "环境变量 FEISHU_ALLOW_WRITE=1 未设置，拒绝写入",
            hint="--direct 只免指纹核对；这道闸门没有任何命令行旗标能绕过",
        )

    await auth.prepare()
    cookie, cookies, domain = _creds()
    async with transport.allow_write_operation(direct=direct):
        result = await drive.import_document(
            cookie=cookie,
            cookies=cookies,
            folder_token=folder_token,
            file_name=upload_file_name,
            file_content=content,
            file_extension=ext,
            target_type=target_type,
            domain=domain,
        )
    if not result:
        raise FeishuError("导入文档失败（飞书服务端处理超时或报错，详情见 stderr 日志）")

    result["name"] = file_title
    return result


def make_doc_replace_draft(ref: str, file_path: str, content: bytes) -> dict:
    kind, token = parse_doc_ref(ref)
    if kind != "docx":
        raise FeishuError("原地替换目前只支持 docx 文档")
    content_sha256 = hashlib.sha256(content).hexdigest()
    fp = doc_write_fingerprint(token, "replace", content_sha256)
    return {
        "status": "draft_not_replaced",
        "action": "replace_document",
        "document_token": token,
        "file_path": file_path,
        "size": len(content),
        "sha256": content_sha256,
        "fingerprint": fp,
    }


async def replace_document(
    ref: str,
    file_path: str,
    confirm: str = "",
    direct: bool = False,
) -> dict:
    """Replace an owned docx body in place while preserving its token/URL."""
    content = _read_doc_write_file(file_path)
    draft = make_doc_replace_draft(ref, file_path, content)

    if direct:
        note("直接写入模式：本次文档原地替换已明确授权。")
    elif not confirm:
        note("未执行替换。确认内容无误后，把指纹填进 --confirm-write 并设 FEISHU_ALLOW_WRITE=1 再执行。")
        raise SendBlocked("draft only", hint=json.dumps(draft, ensure_ascii=False))
    elif confirm != draft["fingerprint"]:
        raise SendBlocked(
            "指纹不匹配，拒绝写入",
            hint=f"本次写入指纹是 {draft['fingerprint']}；文件或文档引用有任何改动都会变",
        )
    # 环境变量是唯一 agent 无法自己满足的那半闸门：--direct 只免指纹核对，不免它。
    if os.environ.get("FEISHU_ALLOW_WRITE") != "1":
        raise SendBlocked(
            "环境变量 FEISHU_ALLOW_WRITE=1 未设置，拒绝写入",
            hint="--direct 只免指纹核对；这道闸门没有任何命令行旗标能绕过",
        )

    await auth.prepare()
    suffix = os.path.splitext(file_path)[1].lower()
    if suffix not in (".md", ".markdown", ".txt"):
        raise FeishuError("原地替换仅支持 Markdown/纯文本文件")

    # Convert once in the parent: the worker receives markdown + pre-rendered
    # HTML + plain text so the paste can offer text/html to Feishu's editor.
    from . import md_html

    markdown_text = content.decode("utf-8", errors="replace")
    html_body, plain_text = md_html.markdown_to_html(markdown_text)
    html_body = md_html.wrap_clipboard_html(html_body)
    payload = json.dumps(
        {"markdown": markdown_text, "html": html_body, "plain": plain_text},
        ensure_ascii=False,
    ).encode("utf-8")

    config.ensure_home()
    fd, temporary_name = tempfile.mkstemp(prefix=".doc-replace.", suffix=".json", dir=str(config.HOME))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        from .browser_worker import run

        result = await run(
            "doc-replace",
            {"ref": ref, "content_file": temporary_name},
            timeout=max(config.BROWSER_TIMEOUT_SECONDS, 180),
        )
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass

    result.update({
        "token": draft["document_token"],
        "sha256": draft["sha256"],
        "size": draft["size"],
    })
    return result


def _normalize_for_compare(text: str) -> str:
    """CJK/alnum-only lowercase form — immune to markdown marker differences."""
    return re.sub(r"[^0-9a-zA-Z一-鿿]+", "", text).lower()


async def _verify_replaced(data: dict, token: str, domain: str,
                           plain: str, markdown_text: str) -> dict:
    """Re-export the doc server-side and compare against the source.

    Calls drive.export_document directly: documents.export_document's DOM
    fallback would mask exactly the failure this check exists to catch.
    A mismatch is reported, never raised — the document is already written.
    """
    from . import md_html

    cookie = auth.cookie_header(data["cookies"])
    constructs = md_html.construct_texts(markdown_text)
    wanted = [
        _normalize_for_compare(t)
        for t in constructs["code_lines"] + constructs["table_cells"]
    ]
    wanted = [w for w in wanted if len(w) >= 2]
    # Line-level containment beats a similarity threshold: known-lossy
    # transforms (image → alt placeholder, quote merging, bare URL exported as
    # text+href doubling) shift chars but never drop a whole source line.
    url_re = re.compile(r"https?://[^\s)\]]+")
    expected_variants: list[set[str]] = []
    for line in plain.splitlines():
        norm = _normalize_for_compare(line)
        if len(norm) < 2:
            continue
        variants = {norm}
        stripped = url_re.sub("", line)
        norm_stripped = _normalize_for_compare(stripped)
        if len(norm_stripped) >= 2:
            variants.add(norm_stripped)
        expected_variants.append(variants)
    last_ratio = 0.0
    last_export = ""
    missing: list[str] = []
    missing_lines: list[str] = []
    for attempt in range(5):
        if attempt:
            await asyncio.sleep(2)  # server-side save debounce
        try:
            exported = await drive.export_document(cookie, data["cookies"], token, "docx", "markdown", domain)
        except Exception:
            continue
        if not exported:
            continue
        last_export = exported.decode("utf-8", errors="replace")
        if last_export.startswith("<!-- feishu:export-fallback"):
            continue  # never compare a structure-lost fallback dump
        actual = _normalize_for_compare(last_export)
        if not actual:
            continue
        # Link syntax re-expands URLs in the export (`[label](href)` carries
        # the href inline), so also compare a URL-stripped rendering of both.
        actual_stripped = _normalize_for_compare(url_re.sub("", last_export))
        import difflib

        last_ratio = difflib.SequenceMatcher(None, _normalize_for_compare(plain), actual).ratio()
        missing = [w for w in wanted if w not in actual and w not in actual_stripped]
        missing_lines = [
            sorted(v)[0] for v in expected_variants
            if not any(x in actual or x in actual_stripped for x in v)
        ]
        if not missing and not missing_lines:
            return {
                "verified": True,
                "similarity": round(last_ratio, 4),
                "missing_constructs": 0,
                "missing_lines": 0,
                "export_attempts": attempt + 1,
                "exported_chars": len(last_export),
            }
    return {
        "verified": False,
        "similarity": round(last_ratio, 4),
        "missing_constructs": len(missing),
        "missing_lines": len(missing_lines),
        "missing_line_samples": missing_lines[:3],
        "export_attempts": 5,
        "exported_chars": len(last_export),
    }


_RICH_CLIPBOARD_JS = """([html, plain]) => navigator.clipboard.write([
  new ClipboardItem({
    'text/html': new Blob([html], {type: 'text/html'}),
    'text/plain': new Blob([plain], {type: 'text/plain'})
  })
])"""

_DATATRANSFER_PASTE_JS = """([html, plain]) => {
  const dt = new DataTransfer();
  dt.setData('text/html', html);
  dt.setData('text/plain', plain);
  const ev = new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true});
  document.activeElement.dispatchEvent(ev);
}"""


# The docx page header renders affordance labels ("add icon/cover/info") and a
# zero-width space INSIDE .page-block.root-block — they are not document body.
_EDITOR_CHROME = {"添加图标", "添加封面", "展示文档信息", "添加时间"}


def _editor_body_text(inner: str) -> str:
    lines = [
        line.strip().strip("​﻿")
        for line in inner.splitlines()
        if line.strip().strip("​﻿")
    ]
    return "\n".join(line for line in lines if line not in _EDITOR_CHROME)


async def _clear_editor(page, editor) -> None:
    """Select-all ×3 + Backspace, then VERIFY the editor body is empty.

    Feishu expands the selection block→document across repeated Ctrl+A; the
    expansion is async, so short gaps race it — retry the whole cycle a few
    times and check the real body text between rounds.
    """
    for round_ in range(4):
        first_body = editor.locator("[data-block-id]:not([data-block-id='1'])").first
        if await first_body.count():
            await first_body.click()
        else:
            await editor.click()
        await page.wait_for_timeout(300)
        for _ in range(3):
            await page.keyboard.press("Control+a")
            await page.wait_for_timeout(250)
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(600)
        remaining = await page.evaluate("""() => {
            const blocks = document.querySelectorAll("[data-block-id]:not([data-block-id='1'])");
            if (blocks.length === 0) return 0;
            if (blocks.length === 1 && !blocks[0].innerText.trim()) return 0;
            return blocks.length;
        }""")
        if remaining == 0 or not _editor_body_text(await editor.inner_text()):
            return
    residue = _editor_body_text(await editor.inner_text())
    raise FeishuError(f"清空文档失败（全选删除后仍有残留内容：{residue[:120]!r}）",
                      hint="文档可能有只读区块或飞书前端已变化")


async def _wait_stable(page, editor, timeout_ms: int = 10_000) -> str:
    """Poll innerText until two consecutive reads match (paste settling)."""
    previous = None
    waited = 0
    while waited < timeout_ms:
        current = await editor.inner_text()
        if current == previous and _editor_body_text(current):
            return current
        previous = current
        await page.wait_for_timeout(500)
        waited += 500
    return await editor.inner_text()


async def _paste_content(page, editor, markdown_text: str, html_body: str, plain_text: str) -> str:
    """Paste with a text/html clipboard; degrade honestly, report the mode."""
    mode = "html"
    try:
        await page.evaluate(_RICH_CLIPBOARD_JS, [html_body, plain_text])
        await page.keyboard.press("Control+v")
    except Exception:
        mode = "html-datatransfer"
        try:
            await editor.click()
            await page.evaluate(_DATATRANSFER_PASTE_JS, [html_body, plain_text])
        except Exception:
            mode = "plain"
            await editor.click()
            await page.evaluate("text => navigator.clipboard.writeText(text)", markdown_text)
            await page.keyboard.press("Control+v")
    # Feishu sometimes offers a paste-format chooser; keep formatting, dismiss.
    await page.wait_for_timeout(400)
    try:
        popup = page.locator("[class*=paste]").first
        if await popup.count() and await popup.is_visible():
            await page.keyboard.press("Enter")
            await page.keyboard.press("Escape")
    except Exception:
        pass
    # Scroll through document so virtualized renderer instantiates blocks
    for _ in range(8):
        await page.evaluate("window.scrollBy(0, 1000)")
        await page.wait_for_timeout(150)
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(300)

    inner = await _wait_stable(page, editor)
    body = _editor_body_text(inner)
    if mode != "plain" and ("```" in body or "| --- |" in body):
        # The editor ignored the HTML flavor; retry as plain markdown text.
        await _clear_editor(page, editor)
        await page.evaluate("text => navigator.clipboard.writeText(text)", markdown_text)
        await page.keyboard.press("Control+v")
        await _wait_stable(page, editor)
        return "plain"
    return mode


async def _replace_document_impl(ref: str, content_file: str) -> dict:
    """Browser Worker implementation for one whole-document replacement."""
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("文档原地替换只能由受限 Browser Worker 执行")
    content_path = os.path.realpath(content_file)
    home_path = os.path.realpath(config.HOME)
    if os.path.commonpath((home_path, content_path)) != home_path:
        raise FeishuError("Browser Worker 拒绝读取 FEISHU_HOME 之外的写入文件")
    raw = _read_doc_write_file(content_path)
    try:
        payload = json.loads(raw.decode("utf-8"))
        markdown_text = payload["markdown"]
        html_body = payload["html"]
        plain_text = payload["plain"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        # Backward compatibility: a raw markdown file pastes as plain text.
        try:
            markdown_text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FeishuError("原地替换文件必须是 UTF-8 文本") from exc
        html_body, plain_text = "", markdown_text
    if not markdown_text.strip():
        raise FeishuError("拒绝用空内容替换文档")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FeishuError("当前 venv 没有 playwright", hint="先运行 feishu.py login") from exc

    data = auth.load_cookies()
    domain = data.get("domain") or config.DOMAIN
    kind, token = parse_doc_ref(ref)
    if kind != "docx":
        raise FeishuError("原地替换目前只支持 docx 文档")
    url = f"https://{domain}/docx/{token}"

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE),
            headless=True,
            ignore_https_errors=False,
            permissions=["clipboard-read", "clipboard-write"],
            env={k: v for k, v in os.environ.items()
                 if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")},
            args=config.BROWSER_ARGS,
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await ctx.add_cookies(auth.playwright_cookie_jar())
            await page.goto(url, wait_until="networkidle", timeout=60_000)
            await auth.raise_if_login_redirect(page)
            await page.wait_for_timeout(2_000)

            editor = page.locator(".page-block.root-block").first
            if not await editor.count():
                raise FeishuError("文档编辑器未渲染", hint="飞书网页结构可能已变化")
            if not await editor.get_attribute("contenteditable") == "true":
                raise FeishuError("当前账号对该文档没有编辑权限")

            await _clear_editor(page, editor)
            mode = await _paste_content(page, editor, markdown_text, html_body, plain_text)
            verification = await _verify_replaced(data, token, domain, plain_text, markdown_text)
        finally:
            await ctx.close()

    return {
        "status": "replaced",
        "url": url,
        "mode": mode,
        "source_chars": len(markdown_text),
        **verification,
    }


async def _inspect_clipboard_impl(ref: str) -> dict:
    """Copy a doc's own content and report the clipboard's text/html shape.

    Diagnostic for paste-fidelity work: Feishu converts pasted HTML best when
    it looks like markup Feishu itself produces.  Returns tag-level structure
    around the first <table>/<pre> only — bounded, no full page body.
    """
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("剪贴板形态检查只能由受限 Browser Worker 执行")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FeishuError("当前 venv 没有 playwright", hint="先运行 feishu.py login") from exc

    data = auth.load_cookies()
    domain = data.get("domain") or config.DOMAIN
    kind, token = parse_doc_ref(ref)
    url = f"https://{domain}/{kind}/{token}"
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE),
            headless=True,
            ignore_https_errors=False,
            permissions=["clipboard-read", "clipboard-write"],
            env={k: v for k, v in os.environ.items()
                 if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")},
            args=config.BROWSER_ARGS,
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await ctx.add_cookies(auth.playwright_cookie_jar())
            await page.goto(url, wait_until="networkidle", timeout=60_000)
            await auth.raise_if_login_redirect(page)
            await page.wait_for_timeout(2_000)
            editor = page.locator(".page-block.root-block").first
            if not await editor.count():
                raise FeishuError("文档编辑器未渲染")
            await editor.click()
            for _ in range(3):
                await page.keyboard.press("Control+a")
                await page.wait_for_timeout(150)
            await page.keyboard.press("Control+c")
            await page.wait_for_timeout(800)
            markup = await page.evaluate(
                """async () => {
                  const items = await navigator.clipboard.read();
                  for (const item of items) {
                    if (item.types.includes('text/html')) {
                      const blob = await item.getType('text/html');
                      return await blob.text();
                    }
                  }
                  return '';
                }"""
            )
        finally:
            await ctx.close()
    # Keep only structure around the interesting blocks; bound the output.
    excerpt = ""
    for tag in ("<table", "<pre"):
        idx = markup.find(tag)
        if idx >= 0:
            excerpt = markup[idx:idx + 2000]
            break
    if not excerpt:
        excerpt = markup[:2000]
    return {"url": url, "clipboard_html_excerpt": excerpt, "total_html_chars": len(markup)}


async def _read_document_impl(ref: str) -> dict:
    """Browser Worker implementation for reading a document/wiki page."""
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("文档读取只能由受限 Browser Worker 执行")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FeishuError("当前 venv 没有 playwright") from exc

    data = auth.load_cookies()
    domain = data.get("domain") or config.DOMAIN
    if ref.startswith("http://") or ref.startswith("https://"):
        url = ref
    else:
        kind, token = parse_doc_ref(ref)
        url = f"https://{domain}/{kind}/{token}"

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(config.PROFILE),
            headless=True,
            ignore_https_errors=False,
            env={k: v for k, v in os.environ.items()
                 if k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")},
            args=config.BROWSER_ARGS,
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await ctx.add_cookies(auth.playwright_cookie_jar())
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await auth.raise_if_login_redirect(page)
            await page.wait_for_timeout(3500)
            title = await page.title()
            text = await page.evaluate('''() => {
                const el = document.querySelector('.page-block.root-block') ||
                           document.querySelector('.wiki-content') ||
                           document.querySelector('.docx-page') ||
                           document.querySelector('#doc-main') ||
                           document.querySelector('.render-unit-wrapper') ||
                           document.querySelector('article') ||
                           document.querySelector('main') ||
                           document.body;
                return el ? el.innerText : '';
            }''')
            return {"title": title, "content": text, "url": url}
        finally:
            await ctx.close()
