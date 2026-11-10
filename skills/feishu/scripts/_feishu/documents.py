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
from urllib.parse import urlsplit

from . import auth, config, secure_store, transport
from .errors import FeishuError, ResourceIsolationUnavailable, SendBlocked
from .output import note

from lark_bridge import drive

_URL_RE = re.compile(
    r"https?://[^/]+/(?:(?:space|drive)/)?(?P<kind>docx|docs|doc|wiki|sheets|sheet|file|folder)/(?P<token>[A-Za-z0-9_-]+)"
)
_EXPORTABLE = {
    "docx": ("docx", ["markdown", "docx", "pdf"]),
    "doc": ("doc", ["docx", "pdf"]),
    "docs": ("doc", ["docx", "pdf"]),
    "wiki": (None, []),  # resolved first
    # 2026-09-10 实测:内网导出端点对 sheet 只认 xlsx,csv 报
    # "export file extension mismatch"(vendor docstring 里的 csv 是错的)。
    "sheets": ("sheet", ["xlsx"]),
    "sheet": ("sheet", ["xlsx"]),
}


def parse_doc_ref(ref: str) -> tuple[str, str]:
    """URL or bare token → (kind, token). kind ∈ docx|doc|wiki|sheet|file|folder."""
    m = _URL_RE.search(ref.strip())
    if m:
        kind = m.group("kind")
        norm_kind = {
            "sheets": "sheet",
            "docs": "doc",
        }.get(kind, kind)
        return (norm_kind, m.group("token"))
    token = ref.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise FeishuError(f"认不出这个文档引用：{ref!r}", hint="支持 docx/wiki/sheets/file/folder 链接或裸 token")
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


async def list_folder_children(folder_ref: str) -> dict:
    """List direct children of a folder by URL or folder token.

    Returns {"folder_token": str, "children": list[dict], "total": int}
    """
    cookie, cookies, domain = _creds()
    kind, token = parse_doc_ref(folder_ref)
    children = await drive.list_children(cookie, cookies, token, domain)
    return {
        "folder_token": token,
        "children": children,
        "total": len(children),
    }


async def download_drive_file(
    file_ref: str,
    out: str = "",
    allow_overwrite: bool = False,
) -> dict:
    """Download a raw file from Feishu Drive by URL or file token.

    Returns {"file_token": str, "file_name": str, "out_path": str, "size": int, "sha256": str}
    """
    cookie, cookies, domain = _creds()
    kind, token = parse_doc_ref(file_ref)
    meta = await drive.download_file_with_meta(cookie, cookies, token, domain)
    if not meta or not meta.get("content"):
        raise FeishuError(f"云盘文件下载失败：{file_ref}")

    content = meta["content"]
    file_name = meta.get("filename") or f"file_{token[:12]}"
    if out:
        if os.path.isdir(out):
            target_path = os.path.join(out, file_name)
        else:
            target_path = out
    else:
        target_path = file_name

    target_path = os.path.abspath(target_path)
    if os.path.exists(target_path) and not allow_overwrite:
        raise FeishuError(
            f"目标文件已存在：{target_path}",
            hint="指定不同的输出路径或文件名，避免意外覆盖已有文件",
        )

    parent_dir = os.path.dirname(target_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    with open(target_path, "wb") as f:
        f.write(content)

    sha256 = hashlib.sha256(content).hexdigest()
    return {
        "file_token": token,
        "file_name": file_name,
        "out_path": target_path,
        "size": len(content),
        "sha256": sha256,
        "content_type": meta.get("content_type", "application/octet-stream"),
    }


async def download_folder(
    folder_ref: str,
    out_dir: str,
    allow_overwrite: bool = False,
) -> dict:
    """Download all files from a Feishu Drive folder into out_dir.

    Returns summary of downloaded files.
    """
    cookie, cookies, domain = _creds()
    kind, token = parse_doc_ref(folder_ref)
    children = await drive.list_children(cookie, cookies, token, domain)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    downloaded = []
    failed = []
    skipped = []

    for item in children:
        item_name = item.get("name") or "unnamed"
        item_token = item.get("obj_token") or item.get("token")
        target_path = os.path.join(out_dir, item_name)

        if os.path.exists(target_path) and not allow_overwrite:
            skipped.append({"name": item_name, "token": item_token, "reason": "already_exists"})
            continue

        try:
            meta = await drive.download_file_with_meta(cookie, cookies, item_token, domain)
            if not meta or not meta.get("content"):
                failed.append({"name": item_name, "token": item_token, "error": "download_failed"})
                continue
            with open(target_path, "wb") as f:
                f.write(meta["content"])
            downloaded.append({
                "name": item_name,
                "token": item_token,
                "out_path": target_path,
                "size": len(meta["content"]),
                "sha256": hashlib.sha256(meta["content"]).hexdigest(),
            })
        except Exception as exc:
            failed.append({"name": item_name, "token": item_token, "error": str(exc)})

    return {
        "folder_token": token,
        "out_dir": out_dir,
        "total_children": len(children),
        "downloaded_count": len(downloaded),
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "downloaded": downloaded,
        "failed": failed,
        "skipped": skipped,
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
    # The env var has to be on the very command that writes: whoever approves that command
    # sees it, and it does not outlive the command. --direct skips only the fingerprint check.
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
    # The env var has to be on the very command that writes: whoever approves that command
    # sees it, and it does not outlive the command. --direct skips only the fingerprint check.
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
            await page.goto(url, wait_until="load", timeout=30_000)
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
            await page.goto(url, wait_until="load", timeout=30_000)
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


MAX_LINKIFY_URLS = 50
_LINKIFY_PATH_RE = re.compile(r"^/(?:docx|sheets|file|wiki|drive)/(?P<token>[A-Za-z0-9_-]+)")


def _linkify_url_token(u: str) -> str:
    """严格校验飞书文档 URL 并取出 token；不合法返回 ""。

    不能用一条正则从头抓到尾：`https://[^/]+\\.feishu\\.cn/...` 里的 `[^/]+`
    会吞掉 `#` 和 `?`，`https://evil.example#.feishu.cn/docx/x`、
    `https://evil.example?.feishu.cn/docx/x` 这类真实 host 在别处的伪造链接
    都能混进来。linkify 会把 URL 写进可能是多人共享的文档，host 必须拆开验。
    """
    try:
        parts = urlsplit(u.strip())
    except ValueError:
        return ""
    if parts.scheme != "https":
        return ""
    host = (parts.hostname or "").lower()
    if host != "feishu.cn" and not host.endswith(".feishu.cn"):
        return ""
    m = _LINKIFY_PATH_RE.match(parts.path)
    return m.group("token") if m else ""


def make_doc_linkify_draft(ref: str, urls: list[str], after_heading: str) -> dict:
    """Draft for linkify: 在文档里插入原生文档引用卡片（mention-doc chip）。"""
    kind, token = parse_doc_ref(ref)
    return {
        "action": "linkify",
        "document_token": token,
        "urls": urls,
        "url_count": len(urls),
        "after_heading": after_heading,
        "fingerprint": doc_write_fingerprint("linkify", token, json.dumps(urls, ensure_ascii=False)),
    }


async def linkify_document(
    ref: str,
    urls: list[str],
    after_heading: str = "",
    confirm: str = "",
    direct: bool = False,
) -> dict:
    """把飞书文档 URL 列表以原生引用卡片形式插入文档（登记「本文引用」）。

    机制：编辑器里以纯文本粘贴飞书文档 URL，会被自动转换为 mention-doc 卡片；
    只有这种卡片才会进入飞书的引用关系（普通超链接不算）。
    after_heading 给出时插入到与该文本精确相等的标题块之后，否则追加到文档末尾。
    """
    urls = [u.strip() for u in urls if u and u.strip()]
    if not urls:
        raise FeishuError("linkify 需要至少一个飞书文档 URL")
    if len(urls) > MAX_LINKIFY_URLS:
        raise FeishuError(f"linkify 一次最多 {MAX_LINKIFY_URLS} 个 URL（当前 {len(urls)} 个）")
    for u in urls:
        if not _linkify_url_token(u):
            raise FeishuError(f"不是可识别的飞书文档链接：{u}")
    draft = make_doc_linkify_draft(ref, urls, after_heading)

    if direct:
        note("直接写入模式：本次插入引用卡片已明确授权。")
    elif not confirm:
        note("未执行插入。确认内容无误后，把指纹填进 --confirm-write 并设 FEISHU_ALLOW_WRITE=1 再执行。")
        raise SendBlocked("draft only", hint=json.dumps(draft, ensure_ascii=False))
    elif confirm != draft["fingerprint"]:
        raise SendBlocked(
            "指纹不匹配，拒绝写入",
            hint=f"本次写入指纹是 {draft['fingerprint']}；URL 列表或文档引用有任何改动都会变",
        )
    # The env var has to be on the very command that writes: whoever approves that command
    # sees it, and it does not outlive the command. --direct skips only the fingerprint check.
    if os.environ.get("FEISHU_ALLOW_WRITE") != "1":
        raise SendBlocked(
            "环境变量 FEISHU_ALLOW_WRITE=1 未设置，拒绝写入",
            hint="--direct 只免指纹核对；这道闸门没有任何命令行旗标能绕过",
        )

    await auth.prepare()

    # 复核基线：插入前导出一次，记录每个 token 的出现次数；
    # 之后的导出复核看增量，文档里原本就有的 token 不算数。
    baseline_counts: dict[str, int] = {}
    try:
        before_text = (await export_document(ref, "markdown")).decode("utf-8", errors="replace")
        for u in urls:
            tok = _linkify_url_token(u)
            baseline_counts[tok] = before_text.count(tok)
    except Exception as exc:
        note(f"linkify 导出基线不可用：{type(exc).__name__}: {exc}")
        baseline_counts = {}
    payload = json.dumps(
        {"ref": ref, "urls": urls, "after_heading": after_heading},
        ensure_ascii=False,
    ).encode("utf-8")

    config.ensure_home()
    fd, temporary_name = tempfile.mkstemp(prefix=".doc-linkify.", suffix=".json", dir=str(config.HOME))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        from .browser_worker import run

        result = await run(
            "doc-linkify",
            {"ref": ref, "payload_file": temporary_name},
            timeout=max(config.BROWSER_TIMEOUT_SECONDS, 60 + 30 * len(urls)),
        )
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass

    result.update({"token": draft["document_token"], "url_count": len(urls)})

    # 对齐 doc replace 的标准：写完后重新导出文档，按插入前基线逐条核对增量。
    # 注意导出文本区分不了卡片和普通链接，「是不是真卡片」由 worker 的
    # reload_verified（卡片 href 增量）把关；这里只确认导出内容里 token 变多了。
    inserted_urls = list(result.get("inserted_urls") or [])
    export_verified: list[str] = []
    if inserted_urls and baseline_counts:
        try:
            after_text = (await export_document(ref, "markdown")).decode("utf-8", errors="replace")
            for u in inserted_urls:
                tok = _linkify_url_token(u)
                if tok and after_text.count(tok) > baseline_counts.get(tok, 0):
                    export_verified.append(u)
        except Exception as exc:  # 导出复核不可用不影响插入结果，但如实标注
            note(f"linkify 导出复核不可用：{type(exc).__name__}: {exc}")
            result["export_check"] = f"unavailable: {type(exc).__name__}"
    elif inserted_urls:
        result["export_check"] = "unavailable: 无插入前基线"
    result["export_verified"] = export_verified
    reload_verified = set(result.get("reload_verified") or [])
    result["verified"] = (
        bool(inserted_urls)
        and reload_verified == set(inserted_urls)
        and set(export_verified) == set(inserted_urls)
    )
    return result


_SCROLL_BOTTOM_JS = """async () => {
  const ed = document.querySelector('[data-block-id]');
  let el = ed;
  while (el && el.scrollHeight <= el.clientHeight + 50) el = el.parentElement;
  if (!el) el = document.scrollingElement || document.documentElement;
  let last = -1;
  for (let i = 0; i < 20; i++) {
    el.scrollTop = el.scrollHeight;
    await new Promise(r => setTimeout(r, 250));
    if (Math.abs(el.scrollTop - last) < 2) break;
    last = el.scrollTop;
  }
}"""

_FIND_LAST_EDITABLE_BLOCK_JS = """() => {
  const blocks = Array.from(document.querySelectorAll('[data-block-id]'));
  for (let i = blocks.length - 1; i >= 0; i--) {
    const b = blocks[i];
    const type = b.getAttribute('data-block-type') || '';
    if (type === 'page' || type.includes('back_ref') || b.closest('[class*="back-ref"], [class*="back_ref"]')) continue;
    const editable = b.querySelector('[contenteditable="true"]') || (b.getAttribute('contenteditable') === 'true' ? b : null);
    if (!editable) continue;
    const r = b.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && b.offsetParent !== null) {
      return b.getAttribute('data-block-id');
    }
  }
  return null;
}"""

_COLLECT_HEADING_MATCHES_JS = """async (needle) => {
  const norm = s => (s || '').replace(/[\\s\\u200b\\u200e\\u200f\\ufeff]+/g, ' ').trim();
  const ed = document.querySelector('[data-block-id]');
  let scroller = ed;
  while (scroller && scroller.scrollHeight <= scroller.clientHeight + 50)
    scroller = scroller.parentElement;
  if (!scroller) scroller = document.scrollingElement || document.documentElement;
  const seen = new Map();
  const collect = () => {
    for (const b of document.querySelectorAll('[data-block-id]')) {
      if (!/heading/i.test(b.className || '')) continue;
      if (norm(b.innerText) !== needle) continue;
      seen.set(b.getAttribute('data-block-id'), true);
    }
  };
  scroller.scrollTop = 0;
  await new Promise(r => setTimeout(r, 300));
  const step = Math.max(scroller.clientHeight, 400);
  for (let y = 0; y <= scroller.scrollHeight + step; y += step) {
    scroller.scrollTop = y;
    await new Promise(r => setTimeout(r, 200));
    collect();
  }
  scroller.scrollTop = scroller.scrollHeight;
  await new Promise(r => setTimeout(r, 300));
  collect();
  return [...seen.keys()];
}"""

_COLLECT_CHIP_HREFS_JS = """async () => {
  const ed = document.querySelector('[data-block-id]');
  let scroller = ed;
  while (scroller && scroller.scrollHeight <= scroller.clientHeight + 50)
    scroller = scroller.parentElement;
  if (!scroller) scroller = document.scrollingElement || document.documentElement;
  const hrefs = new Set();
  const collect = () => {
    for (const el of document.querySelectorAll('[class*=mention-doc]')) {
      const a = el.querySelector('a[href]');
      const href = (a && a.href) || el.getAttribute('href') || '';
      if (href) hrefs.add(href);
    }
  };
  scroller.scrollTop = 0;
  await new Promise(r => setTimeout(r, 300));
  const step = Math.max(scroller.clientHeight, 400);
  for (let y = 0; y <= scroller.scrollHeight + step; y += step) {
    scroller.scrollTop = y;
    await new Promise(r => setTimeout(r, 200));
    collect();
  }
  scroller.scrollTop = scroller.scrollHeight;
  await new Promise(r => setTimeout(r, 300));
  collect();
  return [...hrefs];
}"""


async def _find_chip_href(page, token: str) -> str:
    """在已渲染的 mention-doc 卡片里找指向 token 的那张，返回其 href。"""
    if not token:
        return ""
    return await page.evaluate(
        """(tok) => {
          for (const el of document.querySelectorAll('[class*=mention-doc]')) {
            const a = el.querySelector('a[href]');
            const href = (a && a.href) || el.getAttribute('href') || '';
            if (href && href.includes(tok)) return href;
          }
          return '';
        }""",
        token,
    )


_BLOCK_SNAPSHOT_JS = """() => {
  const norm = s => (s || '').replace(/[\\s\\u200b\\u200e\\u200f\\ufeff]+/g, ' ').trim();
  const sel = window.getSelection();
  let node = sel && sel.anchorNode;
  if (!node) return null;
  let el = node.nodeType === 1 ? node : node.parentElement;
  const block = el && el.closest('[data-block-id]');
  if (!block) return null;
  let nxt = block.nextElementSibling;
  while (nxt && !nxt.getAttribute('data-block-id')) nxt = nxt.nextElementSibling;
  return {
    id: block.getAttribute('data-block-id'),
    text: norm(block.innerText),
    next_id: nxt ? nxt.getAttribute('data-block-id') : '',
    next_text: nxt ? norm(nxt.innerText) : '',
  };
}"""

_BLOCK_STATE_BY_ID_JS = """(bid) => {
  const norm = s => (s || '').replace(/[\\s\\u200b\\u200e\\u200f\\ufeff]+/g, ' ').trim();
  const block = document.querySelector('[data-block-id="' + bid + '"]');
  if (!block) return null;
  let nxt = block.nextElementSibling;
  while (nxt && !nxt.getAttribute('data-block-id')) nxt = nxt.nextElementSibling;
  return {
    id: bid,
    text: norm(block.innerText),
    next_id: nxt ? nxt.getAttribute('data-block-id') : '',
    next_text: nxt ? norm(nxt.innerText) : '',
  };
}"""


async def _linkify_document_impl(ref: str, payload_file: str) -> dict:
    """Browser Worker implementation: paste bare feishu URLs so they become mention-doc chips."""
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("插入引用卡片只能由受限 Browser Worker 执行")
    payload_path = os.path.realpath(payload_file)
    home_path = os.path.realpath(config.HOME)
    if os.path.commonpath((home_path, payload_path)) != home_path:
        raise FeishuError("Browser Worker 拒绝读取 FEISHU_HOME 之外的写入文件")
    try:
        payload = json.loads(_read_doc_write_file(payload_path).decode("utf-8"))
        urls = [str(u) for u in payload["urls"]]
        after_heading = str(payload.get("after_heading") or "")
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise FeishuError("linkify payload 必须是 JSON（含 urls/after_heading）") from exc
    if not urls:
        raise FeishuError("linkify 需要至少一个 URL")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FeishuError("当前 venv 没有 playwright", hint="先运行 feishu.py login") from exc

    data = auth.load_cookies()
    domain = data.get("domain") or config.DOMAIN
    kind, token = parse_doc_ref(ref)
    if kind != "docx":
        raise FeishuError("插入引用卡片目前只支持 docx 文档")
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
            await page.goto(url, wait_until="load", timeout=30_000)
            await auth.raise_if_login_redirect(page)
            await page.wait_for_timeout(2_000)

            editor = page.locator(".page-block.root-block").first
            if not await editor.count():
                raise FeishuError("文档编辑器未渲染", hint="飞书网页结构可能已变化")

            # 复核基线：插入前收集全文档已有的卡片 href，
            # 结束后的重载复核只看增量，文档里原本就有的卡片不算数。
            baseline_hrefs = set(await page.evaluate(_COLLECT_CHIP_HREFS_JS))

            # 定位插入点。不能用 Ctrl+End——编辑器虚拟化渲染下它不一定跳到全文末尾。
            # 先滚动到文档底部强制渲染尾部块（目标块通常都在文档后段）。
            await page.evaluate(_SCROLL_BOTTOM_JS)
            await page.wait_for_timeout(800)
            if after_heading:
                # 只匹配标题块（class 含 heading），文本归一化后精确相等；
                # 虚拟化渲染下从头滚到底收集全部渲染过的标题块，要求命中唯一。
                needle = re.sub(r"\s+", " ", after_heading).strip()
                matches = await page.evaluate(_COLLECT_HEADING_MATCHES_JS, needle)
                if not matches:
                    raise FeishuError(
                        f"找不到标题块「{after_heading}」",
                        hint="--after-heading 只匹配标题块，且文本需与标题精确相等",
                    )
                if len(matches) > 1:
                    raise FeishuError(
                        f"标题块「{after_heading}」命中 {len(matches)} 个，无法确定插入位置",
                        hint="换一个全文唯一的标题文本",
                    )
                target = page.locator(f"[data-block-id='{matches[0]}']")
                await target.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)
                await target.click()
            else:
                last_id = await page.evaluate(_FIND_LAST_EDITABLE_BLOCK_JS)
                if not last_id:
                    raise FeishuError("找不到可编辑的文档末尾块")
                target = page.locator(f"[data-block-id='{last_id}']")
                await target.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)
                await target.click()
            await page.keyboard.press("End")
            await page.wait_for_timeout(200)

            inserted_urls: list[str] = []
            failed: list[str] = []
            skipped: list[str] = []
            rollback_unconfirmed: dict = {}
            for index, target_url in enumerate(urls):
                token = _linkify_url_token(target_url)
                before = await page.locator("[class*=mention-doc]").count()
                # 尝试前快照光标所在块及其下一块：回退后逐项比对，
                # 不假设"一次撤销恰好只撤掉这次粘贴"。
                snapshot = await page.evaluate(_BLOCK_SNAPSHOT_JS)
                if not snapshot or not snapshot.get("id"):
                    raise FeishuError(
                        "无法确定光标所在的文档块，中止插入",
                        hint="此时文档尚未被本次操作修改；请人工打开文档确认后重试",
                    )
                # Enter 与粘贴是这一次尝试的全部改动；转换失败时原样回退，
                # 不把纯文本/普通链接留在别人的文档里。
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(300)
                await page.evaluate("t => navigator.clipboard.writeText(t)", target_url)
                await page.keyboard.press("Control+v")
                chip_href = ""
                converted = False
                for _ in range(20):
                    await page.wait_for_timeout(250)
                    # 主信号：出现指向本 token 的卡片（不看总数——虚拟化渲染
                    # 会让 [class*=mention-doc] 的个数随滚动自己变化）。
                    chip_href = await _find_chip_href(page, token)
                    if chip_href:
                        converted = True
                        break
                    # 兜底信号：计数增加。光标附近无滚动，短窗口内计数稳定；
                    # 是否指向本 URL 由结束后的重载复核把关。
                    if await page.locator("[class*=mention-doc]").count() > before:
                        converted = True
                        break
                if converted:
                    inserted_urls.append(target_url)
                    # 转换成功后等待编辑器内部 DOM 与撤销栈状态稳定，
                    # 再开始下一个 URL 的 Enter，避免连续粘贴破坏卡片。
                    await page.wait_for_timeout(600)
                    continue

                failed.append(target_url)
                note(f"linkify: 第 {index + 1} 个 URL 未转换为引用卡片，执行回退: {target_url}")
                # 回退按快照实测分情形处理，不盲按固定键序：
                await page.keyboard.press("Control+z")
                await page.wait_for_timeout(400)
                cur = await page.evaluate(_BLOCK_SNAPSHOT_JS)
                cur_id = cur.get("id") if cur else ""
                orig_id = snapshot.get("id") or ""
                next_id = snapshot.get("next_id") or ""
                # 只有证明光标既不在原块、也不在原本就存在的下一块，即严格落在
                # 本次 Enter 新建的段落里时，才清文字、并段；其余情形绝不盲按破坏性按键。
                if cur and cur_id and cur_id != orig_id and cur_id != next_id:
                    if cur.get("text"):
                        await page.keyboard.press("Home")
                        await page.keyboard.press("Shift+End")
                        await page.keyboard.press("Delete")
                        await page.wait_for_timeout(200)
                    await page.keyboard.press("Backspace")
                    await page.wait_for_timeout(300)
                # 否则光标已回到原块：撤销把 Enter+粘贴一起撤了，
                # 绝不能再 Backspace——那会删掉上一块的最后一个字。
                after = await page.evaluate(_BLOCK_STATE_BY_ID_JS, snapshot["id"])
                rollback_ok = (
                    after is not None
                    and after["text"] == snapshot["text"]
                    and after["next_id"] == snapshot["next_id"]
                    and after["next_text"] == snapshot["next_text"]
                )
                if not rollback_ok:
                    rollback_unconfirmed = {
                        "url": target_url,
                        "block_id": snapshot["id"],
                        "expected": snapshot,
                        "actual": after,
                        "note": "回退未能确认恢复原样，需人工检查该块及其前后内容",
                    }
                    skipped = urls[index + 1:]
                    note(f"linkify: 回退比对不通过（块 {snapshot['id']}），立即停止，剩余 {len(skipped)} 个 URL 未尝试")
                    break
            await page.wait_for_timeout(1_500)

            # 结束后重新加载页面重读文档，滚动全文收集卡片 href，按插入前
            # 基线取增量、按 token 逐条核对每条 URL 的引用确实存在
            # （对齐 doc replace 的复核标准）。
            reload_verified: list[str] = []
            chips_expose_href = False
            if inserted_urls:
                await page.reload(wait_until="load", timeout=30_000)
                await auth.raise_if_login_redirect(page)
                await page.wait_for_timeout(2_000)
                hrefs = set(await page.evaluate(_COLLECT_CHIP_HREFS_JS))
                chips_expose_href = bool(hrefs)
                new_hrefs = hrefs - baseline_hrefs
                for u in inserted_urls:
                    tok = _linkify_url_token(u)
                    if tok and any(tok in h for h in new_hrefs):
                        reload_verified.append(u)
        finally:
            await ctx.close()

    if not inserted_urls:
        status = "failed"
    elif failed or skipped:
        status = "partial"
    else:
        status = "linkified"
    result = {
        "status": status,
        "url": url,
        "inserted": len(inserted_urls),
        "inserted_urls": inserted_urls,
        "failed": failed,
        "reload_verified": reload_verified,
        "reload_check": "chip_href_delta" if chips_expose_href else "unavailable:卡片未暴露href",
        "after_heading": after_heading,
    }
    if skipped:
        result["skipped"] = skipped
    if rollback_unconfirmed:
        result["rollback_unconfirmed"] = rollback_unconfirmed
    return result


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


async def screenshot_document(ref: str, out_dir: str) -> dict:
    """抓取文档正文中的全部图片（受限 Browser Worker 渲染 + 滚动强制加载）。"""
    from .browser_worker import run

    return await run(
        "doc-screenshot",
        {"ref": ref, "out_dir": out_dir},
        timeout=max(config.BROWSER_TIMEOUT_SECONDS, 300),
    )


_SCREENSHOT_COLLECT_JS = """() => {
    const root = document.querySelector('.page-block.root-block') ||
                 document.querySelector('.wiki-content') ||
                 document.querySelector('.docx-page') ||
                 document.querySelector('#doc-main') ||
                 document.querySelector('.render-unit-wrapper') ||
                 document.body;
    const vh = window.innerHeight;
    const imgs = [...root.querySelectorAll('img')].map((img) => {
        const r = img.getBoundingClientRect();
        return {
            img,
            src: img.currentSrc || img.src || '',
            w: img.naturalWidth || 0,
            h: img.naturalHeight || 0,
            top: r.top,
            bottom: r.bottom,
            visible: r.bottom > -50 && r.top < vh + 50 && r.width > 0,
        };
    }).filter(x => x.visible && x.w >= 150 && x.h >= 80
                  && !/avatar|icon|emoji|logo/i.test(x.src));
    imgs.forEach((x, i) => x.img.setAttribute('data-feishu-shot', String(i)));
    return imgs.map((x, i) => ({shot: String(i), src: x.src, w: x.w, h: x.h}));
}"""

_SCREENSHOT_FETCH_JS = """async (src) => {
    const resp = await fetch(src);
    if (!resp.ok) throw new Error('http ' + resp.status);
    const blob = await resp.blob();
    if (blob.size > 24 * 1024 * 1024) throw new Error('too large: ' + blob.size);
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let bin = '';
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return {type: blob.type || '', b64: btoa(bin)};
}"""

_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}


async def _screenshot_document_impl(ref: str, out_dir: str) -> dict:
    """Browser Worker implementation: 滚动文档抓取正文图片。

    优先在浏览器内 fetch 原图（http(s) 与 blob: 源均可，保持原始分辨率）；
    fetch 失败退化为元素截图。虚拟化渲染的文档必须边滚动边抓。
    """
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("文档图片抓取只能由受限 Browser Worker 执行")
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FeishuError("当前 venv 没有 playwright") from exc
    import base64
    from pathlib import Path

    data = auth.load_cookies()
    domain = data.get("domain") or config.DOMAIN
    if ref.startswith("http://") or ref.startswith("https://"):
        url = ref
    else:
        kind, token = parse_doc_ref(ref)
        url = f"https://{domain}/{kind}/{token}"

    out = Path(out_dir)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)

    saved: list[str] = []
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
            await page.wait_for_timeout(4000)
            title = await page.title()

            # 选定真正的滚动容器（scrollHeight 最大的可滚动元素），后续统一滚动它
            await page.evaluate("""() => {
                let best = document.scrollingElement || document.documentElement;
                for (const c of document.querySelectorAll('*')) {
                    if (c.clientHeight > 200 && c.scrollHeight > c.clientHeight + 100
                        && c.scrollHeight > (best.scrollHeight || 0)) best = c;
                }
                window.__feishuScroller = best;
            }""")

            seen: set[str] = set()
            last_top = -1
            stall = 0
            for _step in range(200):
                if len(saved) >= 80:
                    break
                metas = await page.evaluate(_SCREENSHOT_COLLECT_JS)
                for meta in metas:
                    key = meta["src"] or f"shot:{meta['shot']}:{meta['w']}x{meta['h']}"
                    if key in seen:
                        continue
                    seen.add(key)
                    n = len(saved) + 1
                    name = ""
                    src = meta["src"]
                    if src:
                        try:
                            res = await page.evaluate(_SCREENSHOT_FETCH_JS, src)
                            ext = _MIME_EXT.get((res.get("type") or "").split(";")[0].strip().lower(), ".png")
                            name = f"img_{n:02d}{ext}"
                            (out / name).write_bytes(base64.b64decode(res["b64"]))
                        except Exception:
                            name = ""
                    if not name:
                        try:
                            handle = await page.query_selector(f'[data-feishu-shot="{meta["shot"]}"]')
                            if handle:
                                await handle.scroll_into_view_if_needed(timeout=3000)
                                name = f"img_{n:02d}.png"
                                await handle.screenshot(path=str(out / name), timeout=5000)
                            else:
                                continue
                        except Exception:
                            if name and (out / name).exists():
                                (out / name).unlink()
                            continue
                    saved.append(name)
                top = await page.evaluate("""() => {
                    const s = window.__feishuScroller;
                    s.scrollTop = (s.scrollTop || 0) + Math.round(window.innerHeight * 0.75);
                    return s.scrollTop;
                }""")
                await page.wait_for_timeout(700)
                if top == last_top:
                    stall += 1
                    if stall >= 2:
                        break
                else:
                    stall = 0
                last_top = top

            return {"url": url, "title": title, "out_dir": str(out), "images": saved}
        finally:
            await ctx.close()


# ---------------------------------------------------------------------------
# 文档插图（image insert）
# ---------------------------------------------------------------------------

MAX_INSERT_IMAGES = 20
_INSERT_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}
_INSERT_IMAGE_MAX_BYTES = 10 * 1024 * 1024

_SCROLL_TO_BLOCK_JS = """async (bid) => {
  const sel = () => document.querySelector('[data-block-id="' + bid + '"]');
  if (sel()) return true;
  const ed = document.querySelector('[data-block-id]');
  let scroller = ed;
  while (scroller && scroller.scrollHeight <= scroller.clientHeight + 50)
    scroller = scroller.parentElement;
  if (!scroller) scroller = document.scrollingElement || document.documentElement;
  // 虚拟化渲染下目标块可能已卸载：先回顶部，再逐步下滚直到重新挂载
  scroller.scrollTop = 0;
  await new Promise(r => setTimeout(r, 250));
  if (sel()) return true;
  const step = Math.max(Math.floor(scroller.clientHeight * 0.8), 300);
  for (let y = 0; y <= scroller.scrollHeight + step; y += step) {
    scroller.scrollTop = y;
    await new Promise(r => setTimeout(r, 220));
    if (sel()) return true;
  }
  return !!sel();
}"""

_IMG_CLIPBOARD_WRITE_JS = """async ([b64, mime]) => {
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const blob = new Blob([bytes], {type: mime});
  const item = {};
  item[mime] = blob;
  await navigator.clipboard.write([new ClipboardItem(item)]);
}"""

_IMG_DATATRANSFER_PASTE_JS = """([b64, mime, name]) => {
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const file = new File([bytes], name, {type: mime});
  const dt = new DataTransfer();
  dt.items.add(file);
  // 派发到选区所在的 contenteditable 上：从不可编辑的根 div 冒泡的
  // paste 事件会被编辑器忽略（实测踩过）
  const sel = window.getSelection();
  let el = sel && sel.anchorNode;
  el = el && (el.nodeType === 1 ? el : el.parentElement);
  const editable = el && el.closest ? el.closest('[contenteditable="true"]') : null;
  const target = editable || document.activeElement;
  const ev = new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true});
  target.dispatchEvent(ev);
}"""

# 探测指定块的“下一块”是否含图片（位置感知检测，免疫虚拟化计数抖动）
_NEXT_BLOCK_IS_IMAGE_JS = """(bid) => {
  const b = document.querySelector('[data-block-id="' + bid + '"]');
  if (!b) return {found: false};
  let n = b.nextElementSibling;
  while (n && !n.getAttribute('data-block-id')) n = n.nextElementSibling;
  if (!n) return {found: false};
  const t = (n.getAttribute('data-block-type') || '') + ' ' + (n.className || '');
  return {found: true, isImage: !!n.querySelector('img') || /image/i.test(t)};
}"""

# 拖放路径：在光标坐标处派发 DragEvent(drop)，走编辑器的文件拖入上传通道
_IMG_DROP_JS = """([b64, mime, name]) => {
  const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const file = new File([bytes], name, {type: mime});
  const dt = new DataTransfer();
  dt.items.add(file);
  const sel = window.getSelection();
  let x = 400, y = 300, target = null;
  if (sel && sel.rangeCount) {
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    if (rect && (rect.width || rect.height)) { x = rect.left + 2; y = rect.top + 2; }
    let el = sel.anchorNode;
    el = el && (el.nodeType === 1 ? el : el.parentElement);
    target = el && el.closest ? el.closest('[contenteditable="true"]') : null;
  }
  if (!target) {
    target = document.elementFromPoint(x, y) || document.activeElement;
  }
  for (const type of ['dragenter', 'dragover', 'drop']) {
    const ev = new DragEvent(type, {dataTransfer: dt, bubbles: true, cancelable: true,
                                    clientX: x, clientY: y});
    target.dispatchEvent(ev);
  }
}"""

_IMG_COUNT_NEAR_JS = """() => {
  const root = document.querySelector('.page-block.root-block');
  if (!root) return 0;
  let n = root.querySelectorAll('img').length;
  for (const b of root.querySelectorAll('[data-block-id]')) {
    const t = (b.getAttribute('data-block-type') || '') + (b.className || '');
    if (/image/i.test(t) && !b.querySelector('img')) n += 1;  // 图片块占位（上传中）
  }
  return n;
}"""

_IMG_UPLOAD_BUSY_JS = """() => {
  const root = document.querySelector('.page-block.root-block');
  if (!root) return false;
  return !!root.querySelector('[class*=upload], [class*=progress], [class*=loading]');
}"""

_COUNT_ALL_IMAGES_JS = """async () => {
  const ed = document.querySelector('[data-block-id]');
  let scroller = ed;
  while (scroller && scroller.scrollHeight <= scroller.clientHeight + 50)
    scroller = scroller.parentElement;
  if (!scroller) scroller = document.scrollingElement || document.documentElement;
  const seen = new Set();
  const collect = () => {
    const root = document.querySelector('.page-block.root-block');
    if (!root) return;
    for (const img of root.querySelectorAll('img')) {
      const key = img.currentSrc || img.src || '';
      if (key) seen.add(key);
    }
    for (const b of root.querySelectorAll('[data-block-id]')) {
      const t = (b.getAttribute('data-block-type') || '') + (b.className || '');
      if (/image/i.test(t)) seen.add('block:' + b.getAttribute('data-block-id'));
    }
  };
  scroller.scrollTop = 0;
  await new Promise(r => setTimeout(r, 300));
  const step = Math.max(scroller.clientHeight, 400);
  for (let y = 0; y <= scroller.scrollHeight + step; y += step) {
    scroller.scrollTop = y;
    await new Promise(r => setTimeout(r, 200));
    collect();
  }
  scroller.scrollTop = scroller.scrollHeight;
  await new Promise(r => setTimeout(r, 300));
  collect();
  return seen.size;
}"""


def make_doc_images_draft(ref: str, images: list[str], after_heading: str) -> dict:
    """Draft for insert-images: 在文档指定位置插入本地图片。"""
    kind, token = parse_doc_ref(ref)
    h = hashlib.sha256()
    entries = []
    for path in images:
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        entries.append({"file": os.path.basename(path), "size": os.path.getsize(path), "sha256": digest})
        h.update(digest.encode())
    return {
        "action": "insert_images",
        "document_token": token,
        "images": entries,
        "image_count": len(entries),
        "after_heading": after_heading,
        "fingerprint": doc_write_fingerprint("insert-images", token, h.hexdigest()),
    }


async def insert_images(
    ref: str,
    images: list[str],
    after_heading: str = "",
    confirm: str = "",
    direct: bool = False,
) -> dict:
    """把本地图片插入已有 docx 文档（after_heading 标题块之后，默认文末）。

    机制：受限浏览器打开文档 → 定位插入点 → 把图片写入剪贴板
    （ClipboardItem，失败退化为 DataTransfer 合成粘贴事件）→ 模拟 Ctrl+V，
    飞书编辑器自动完成上传并生成图片块。逐张核对、失败回退、结束重载复核。
    """
    images = [str(p) for p in images if p and str(p).strip()]
    if not images:
        raise FeishuError("insert-images 需要至少一张本地图片")
    if len(images) > MAX_INSERT_IMAGES:
        raise FeishuError(f"insert-images 一次最多 {MAX_INSERT_IMAGES} 张（当前 {len(images)} 张）")
    kind, _token = parse_doc_ref(ref)
    if kind != "docx":
        raise FeishuError("插入图片目前只支持 docx 文档")

    import base64 as _b64

    payload_images = []
    total_b64 = 0
    for path in images:
        real = os.path.realpath(path)
        if not os.path.isfile(real):
            raise FeishuError(f"图片不存在：{path}")
        ext = os.path.splitext(real)[1].lower()
        mime = _INSERT_IMAGE_MIME.get(ext)
        if not mime:
            raise FeishuError(f"不支持的图片格式：{path}（支持 {', '.join(sorted(_INSERT_IMAGE_MIME))}）")
        size = os.path.getsize(real)
        if size > _INSERT_IMAGE_MAX_BYTES:
            raise FeishuError(f"单张图片超过 10MB 上限：{path}")
        with open(real, "rb") as f:
            b64 = _b64.b64encode(f.read()).decode("ascii")
        total_b64 += len(b64)
        payload_images.append({"name": os.path.basename(real), "mime": mime, "b64": b64})
    if total_b64 > config.MAX_DOC_WRITE_BYTES:
        raise FeishuError(
            f"图片总体积超过上限（base64 后 {total_b64} bytes）",
            hint=f"当前上限为 {config.MAX_DOC_WRITE_BYTES} bytes；分批插入或压缩图片",
        )

    draft = make_doc_images_draft(ref, images, after_heading)
    if direct:
        note("直接写入模式：本次插入图片已明确授权。")
    elif not confirm:
        note("未执行插入。确认内容无误后，把指纹填进 --confirm-write 并设 FEISHU_ALLOW_WRITE=1 再执行。")
        raise SendBlocked("draft only", hint=json.dumps(draft, ensure_ascii=False))
    elif confirm != draft["fingerprint"]:
        raise SendBlocked(
            "指纹不匹配，拒绝写入",
            hint=f"本次写入指纹是 {draft['fingerprint']}；图片列表或文档引用有任何改动都会变",
        )
    if os.environ.get("FEISHU_ALLOW_WRITE") != "1":
        raise SendBlocked(
            "环境变量 FEISHU_ALLOW_WRITE=1 未设置，拒绝写入",
            hint="--direct 只免指纹核对；这道闸门没有任何命令行旗标能绕过",
        )

    await auth.prepare()
    payload = json.dumps(
        {"ref": ref, "after_heading": after_heading, "images": payload_images},
        ensure_ascii=False,
    ).encode("utf-8")

    config.ensure_home()
    fd, temporary_name = tempfile.mkstemp(prefix=".doc-images.", suffix=".json", dir=str(config.HOME))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        from .browser_worker import run

        result = await run(
            "doc-insert-images",
            {"ref": ref, "payload_file": temporary_name},
            timeout=max(config.BROWSER_TIMEOUT_SECONDS, 90 + 45 * len(images)),
        )
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass

    inserted = result.get("inserted") or []
    result["verified"] = bool(inserted) and len(result.get("reload_verified") or []) == len(inserted)
    return result


async def _insert_images_impl(ref: str, payload_file: str) -> dict:
    """Browser Worker implementation: 逐张把图片粘贴进文档编辑器。"""
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("插入图片只能由受限 Browser Worker 执行")
    payload_path = os.path.realpath(payload_file)
    home_path = os.path.realpath(config.HOME)
    if os.path.commonpath((home_path, payload_path)) != home_path:
        raise FeishuError("Browser Worker 拒绝读取 FEISHU_HOME 之外的写入文件")
    try:
        payload = json.loads(_read_doc_write_file(payload_path).decode("utf-8"))
        images = payload["images"]
        after_heading = str(payload.get("after_heading") or "")
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise FeishuError("insert-images payload 必须是 JSON（含 images/after_heading）") from exc
    if not images:
        raise FeishuError("insert-images 需要至少一张图片")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FeishuError("当前 venv 没有 playwright") from exc

    data = auth.load_cookies()
    domain = data.get("domain") or config.DOMAIN
    kind, token = parse_doc_ref(ref)
    if kind != "docx":
        raise FeishuError("插入图片目前只支持 docx 文档")
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
            await page.goto(url, wait_until="load", timeout=30_000)
            await auth.raise_if_login_redirect(page)
            await page.wait_for_timeout(2_000)

            editor = page.locator(".page-block.root-block").first
            if not await editor.count():
                raise FeishuError("文档编辑器未渲染", hint="飞书网页结构可能已变化")

            # 基线：插入前滚动全文统计已有图片数（虚拟化渲染下逐个收集去重）
            baseline_count = await page.evaluate(_COUNT_ALL_IMAGES_JS)

            # 定位插入点（与 linkify 同一套：禁 Ctrl+End，先滚底强制渲染）
            await page.evaluate(_SCROLL_BOTTOM_JS)
            await page.wait_for_timeout(800)
            if after_heading:
                needle = re.sub(r"\s+", " ", after_heading).strip()
                matches = await page.evaluate(_COLLECT_HEADING_MATCHES_JS, needle)
                if not matches:
                    raise FeishuError(
                        f"找不到标题块「{after_heading}」",
                        hint="--after-heading 只匹配标题块，且文本需与标题精确相等",
                    )
                if len(matches) > 1:
                    raise FeishuError(
                        f"标题块「{after_heading}」命中 {len(matches)} 个，无法确定插入位置",
                        hint="换一个全文唯一的标题文本",
                    )
                target_id = matches[0]
            else:
                target_id = await page.evaluate(_FIND_LAST_EDITABLE_BLOCK_JS)
                if not target_id:
                    raise FeishuError("找不到可编辑的文档末尾块")
            # 标题收集结束时滚动条在文末，靠前的目标块可能已被虚拟化卸载：
            # 先滚动到它重新挂载，再 scroll_into_view
            if not await page.evaluate(_SCROLL_TO_BLOCK_JS, target_id):
                raise FeishuError(f"目标块 {target_id} 滚动后仍未渲染，无法定位插入点")
            target = page.locator(f"[data-block-id='{target_id}']")
            await target.scroll_into_view_if_needed()
            await page.wait_for_timeout(500)
            # 用 Selection API 精确建立文本光标（鼠标点击可能落在块容器上，
            # 导致焦点在根 div、键盘/粘贴事件不进入编辑区）
            if await page.evaluate(_FOCUS_BLOCK_END_JS, target_id) != "ok":
                raise FeishuError("无法在目标块内建立文本光标（块类型不支持？）")
            await page.wait_for_timeout(200)

            inserted: list[str] = []
            failed: list[str] = []
            diagnostics: list[dict] = []
            for index, image in enumerate(images):
                name = image["name"]
                # 连续插图脱困：若当前焦点仍在图片等不可编辑块上，先按 ArrowDown 离开
                if not await page.evaluate(_IS_EDITABLE_FOCUSED_JS):
                    await page.keyboard.press("ArrowDown")
                    await page.wait_for_timeout(300)
                snapshot = await page.evaluate(_BLOCK_SNAPSHOT_JS)
                if not snapshot or not snapshot.get("id"):
                    raise FeishuError(
                        "无法确定光标所在的文档块，中止插图",
                        hint="此时文档尚未被本次操作修改；请人工打开文档确认后重试",
                    )
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(300)
                before_imgs = await page.evaluate(_IMG_COUNT_NEAR_JS)
                # 大图上传慢：检测窗口随体积放宽（基础 30s，每 MB +10s）
                size_mb = max(1, len(image["b64"]) * 3 // 4 // (1024 * 1024))
                deadline_polls = 60 + 20 * size_mb

                async def _try_paste(mode: str) -> str:
                    """按指定方式粘贴一次；返回实际生效的方式（"" 表示没能发出）。"""
                    if mode == "clipboard":
                        try:
                            # clipboard.write 需要文档聚焦；Enter 后光标已在编辑区，
                            # 再补一次聚焦防止 evaluate 调用把焦点偷走
                            await page.evaluate("() => { const e = document.activeElement; if (e && e.focus) e.focus(); }")
                            await page.evaluate(_IMG_CLIPBOARD_WRITE_JS, [image["b64"], image["mime"]])
                            await page.keyboard.press("Control+v")
                            return "clipboard"
                        except Exception as exc:
                            note(f"insert-images: clipboard 方式发出失败（{type(exc).__name__}: {exc}）")
                            return ""
                    try:
                        await page.evaluate(_IMG_DATATRANSFER_PASTE_JS,
                                            [image["b64"], image["mime"], name])
                        return "datatransfer"
                    except Exception as exc:
                        note(f"insert-images: datatransfer 方式发出失败（{type(exc).__name__}: {exc}）")
                        return ""

                ok = False
                used_mode = ""
                for mode in ("clipboard", "datatransfer"):
                    used_mode = await _try_paste(mode)
                    if not used_mode:
                        continue
                    for _ in range(deadline_polls):
                        await page.wait_for_timeout(500)
                        if await page.evaluate(_IMG_COUNT_NEAR_JS) > before_imgs:
                            ok = True
                            break
                    if ok:
                        break
                    note(f"insert-images: {used_mode} 方式 {deadline_polls // 2}s 内未被接收，换下一种方式")
                    # 换一种方式前撤销可能的残留（空段落由后续回退统一处理）
                    await page.keyboard.press("Control+z")
                    await page.wait_for_timeout(300)
                if ok:
                    inserted.append(name)
                    note(f"insert-images: 第 {index + 1} 张已插入（{used_mode}）：{name}")
                    # 等上传与撤销栈稳定再插下一张
                    await page.wait_for_timeout(2_000)
                    cur_snap = await page.evaluate(_BLOCK_SNAPSHOT_JS)
                    cur_bid = cur_snap.get("id") if cur_snap else ""
                    if cur_bid:
                        res = await page.evaluate(_ENSURE_EDITABLE_AFTER_BLOCK_JS, cur_bid)
                        if res != "next-focused":
                            await page.keyboard.press("ArrowDown")
                            await page.wait_for_timeout(300)
                    continue
                diag = await page.evaluate("""() => {
                    const e = document.activeElement;
                    const popups = [...document.querySelectorAll('[class*=paste], [class*=modal], [class*=dialog], [class*=upload]')]
                        .filter(x => x.offsetWidth > 0)
                        .map(x => (x.className || '').slice(0, 60) + ':' + (x.innerText || '').slice(0, 40));
                    return {
                        activeElement: e ? e.tagName + '.' + (e.className || '') : 'none',
                        imgs: document.querySelectorAll('.page-block.root-block')
                            ? document.querySelector('.page-block.root-block').querySelectorAll('img').length : -1,
                        popups,
                    };
                }""")
                shot = config.HOME / f"debug-insert-{int(__import__('time').time())}.png"
                try:
                    await page.screenshot(path=str(shot), full_page=False)
                    diag["screenshot"] = str(shot)
                except Exception:
                    pass
                diagnostics.append(diag)

                failed.append(name)
                note(f"insert-images: 第 {index + 1} 张未被编辑器接收，执行回退：{name}")
                await page.keyboard.press("Control+z")
                await page.wait_for_timeout(400)
                cur = await page.evaluate(_BLOCK_SNAPSHOT_JS)
                cur_id = cur.get("id") if cur else ""
                orig_id = snapshot.get("id") or ""
                next_id = snapshot.get("next_id") or ""
                if cur and cur_id and cur_id != orig_id and cur_id != next_id:
                    if cur.get("text"):
                        await page.keyboard.press("Home")
                        await page.keyboard.press("Shift+End")
                        await page.keyboard.press("Delete")
                        await page.wait_for_timeout(200)
                    await page.keyboard.press("Backspace")
                    await page.wait_for_timeout(300)
                after = await page.evaluate(_BLOCK_STATE_BY_ID_JS, snapshot["id"])
                rollback_ok = (
                    after is not None
                    and after["text"] == snapshot["text"]
                    and after["next_id"] == snapshot["next_id"]
                    and after["next_text"] == snapshot["next_text"]
                )
                if not rollback_ok:
                    result = {
                        "status": "failed",
                        "url": url,
                        "inserted": inserted,
                        "failed": failed + [img["name"] for img in images[index + 1:]],
                        "rollback_unconfirmed": {
                            "image": name,
                            "block_id": snapshot["id"],
                            "expected": snapshot,
                            "actual": after,
                            "note": "回退未能确认恢复原样，需人工检查该块及其前后内容",
                        },
                    }
                    return result
            await page.wait_for_timeout(1_500)

            # 复核：重载页面滚动全文统计图片数，应不少于 基线 + 插入数
            reload_verified: list[str] = []
            if inserted:
                await page.reload(wait_until="load", timeout=30_000)
                await auth.raise_if_login_redirect(page)
                await page.wait_for_timeout(2_000)
                after_count = await page.evaluate(_COUNT_ALL_IMAGES_JS)
                if after_count >= baseline_count + len(inserted):
                    reload_verified = list(inserted)
                else:
                    note(f"insert-images: 重载复核图片数不足（基线 {baseline_count}，"
                         f"现 {after_count}，预期 +{len(inserted)}）")

            status = "inserted" if inserted and not failed else ("partial" if inserted else "failed")
            return {
                "status": status,
                "url": url,
                "inserted": inserted,
                "failed": failed,
                "reload_verified": reload_verified,
                "after_heading": after_heading,
                "diagnostics": diagnostics,
            }
        finally:
            await ctx.close()


# ---------------------------------------------------------------------------
# 文档附件（file attachment insert）：任意文件粘贴为附件块，同时入云盘
# ---------------------------------------------------------------------------

MAX_INSERT_FILE_BYTES = 100 * 1024 * 1024
MAX_INSERT_FILES = 20

# 光标附近（文末挂载区）按文件名出现次数探测附件块是否生成
_FILE_NAME_NEAR_COUNT_JS = """(name) => {
  const root = document.querySelector('.page-block.root-block');
  if (!root) return 0;
  const text = root.innerText || '';
  let n = 0, i = 0;
  for (;;) { i = text.indexOf(name, i); if (i < 0) break; n++; i += name.length; }
  return n;
}"""

# 滚动全文收集含目标文件名的块（虚拟化免疫），用于基线与重载复核
_FILE_NAME_BLOCKS_COUNT_JS = """async (name) => {
  const ed = document.querySelector('[data-block-id]');
  let scroller = ed;
  while (scroller && scroller.scrollHeight <= scroller.clientHeight + 50)
    scroller = scroller.parentElement;
  if (!scroller) scroller = document.scrollingElement || document.documentElement;
  const seen = new Set();
  const collect = () => {
    const root = document.querySelector('.page-block.root-block');
    if (!root) return;
    for (const b of root.querySelectorAll('[data-block-id]')) {
      if ((b.innerText || '').indexOf(name) >= 0) seen.add(b.getAttribute('data-block-id'));
    }
  };
  scroller.scrollTop = 0;
  await new Promise(r => setTimeout(r, 300));
  const step = Math.max(scroller.clientHeight, 400);
  for (let y = 0; y <= scroller.scrollHeight + step; y += step) {
    scroller.scrollTop = y;
    await new Promise(r => setTimeout(r, 200));
    collect();
  }
  scroller.scrollTop = scroller.scrollHeight;
  await new Promise(r => setTimeout(r, 300));
  collect();
  return seen.size;
}"""


def make_doc_file_draft(
    ref: str,
    file_paths: list[str] | str,
    after_heading: str = "",
    find: str = "",
    insert_before: bool = False,
) -> dict:
    """Draft for insert-file: 把本地文件（单个或批量）作为附件块插入文档（文件随附件入云盘）。"""
    kind, token = parse_doc_ref(ref)
    if isinstance(file_paths, str):
        file_paths = [file_paths]
    h = hashlib.sha256()
    entries = []
    for path in file_paths:
        real = os.path.realpath(path)
        with open(real, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        entries.append({"file": os.path.basename(real), "size": os.path.getsize(real), "sha256": digest})
        h.update(digest.encode())
    content_token = entries[0]["sha256"] if len(entries) == 1 else h.hexdigest()
    res = {
        "action": "insert_file",
        "document_token": token,
        "files": entries,
        "file_count": len(entries),
        "after_heading": after_heading,
        "find": find,
        "insert_before": insert_before,
        "fingerprint": doc_write_fingerprint("insert-file", token, content_token),
    }
    if len(entries) == 1:
        res["file"] = entries[0]
    return res


async def insert_file(
    ref: str,
    file_path: str | list[str],
    after_heading: str = "",
    find: str = "",
    insert_before: bool = False,
    confirm: str = "",
    direct: bool = False,
) -> dict:
    """把本地任意文件（单文件或批量）插入已有 docx 文档为附件块。

    支持插入位置：
      - 默认：文档末尾
      - after_heading: 位于指定文本的标题块之后
      - find + insert_before / insert_after: 位于指定锚点块之前或之后

    机制同 insert-images：受限浏览器打开文档 → 定位插入点 → DataTransfer
    合成粘贴（ClipboardItem 先行尝试）逐个上传附件 → 文件随之进入云盘。
    按文件名出现次数核对、失败回退、结束重载复核。
    """
    if isinstance(file_path, str):
        raw_paths = [file_path.strip()] if file_path.strip() else []
    else:
        raw_paths = [str(p).strip() for p in file_path if p and str(p).strip()]
    if not raw_paths:
        raise FeishuError("insert-file 需要 --attach 指定本地文件")
    if len(raw_paths) > MAX_INSERT_FILES:
        raise FeishuError(f"insert-file 一次最多 {MAX_INSERT_FILES} 个文件（当前 {len(raw_paths)} 个）")
    kind, _token = parse_doc_ref(ref)
    if kind != "docx":
        raise FeishuError("插入附件目前只支持 docx 文档")

    import base64 as _b64
    import mimetypes as _mimetypes

    payload_files = []
    total_b64 = 0
    total_bytes = 0
    for path in raw_paths:
        real = os.path.realpath(path)
        if not os.path.isfile(real):
            raise FeishuError(f"文件不存在：{path}")
        size = os.path.getsize(real)
        if size > MAX_INSERT_FILE_BYTES:
            raise FeishuError(f"文件超过 100MB 上限：{path} ({size} bytes)")
        mime = _mimetypes.guess_type(real)[0] or "application/octet-stream"
        with open(real, "rb") as f:
            b64 = _b64.b64encode(f.read()).decode("ascii")
        total_b64 += len(b64)
        total_bytes += size
        payload_files.append({
            "name": os.path.basename(real),
            "mime": mime,
            "b64": b64,
            "size": size,
        })

    if total_b64 > config.MAX_DOC_WRITE_BYTES:
        raise FeishuError(
            f"文件总体积超过写入上限（base64 后 {total_b64} bytes）",
            hint=f"当前上限为 {config.MAX_DOC_WRITE_BYTES} bytes，请分批插入",
        )

    draft = make_doc_file_draft(ref, raw_paths, after_heading=after_heading, find=find, insert_before=insert_before)
    if direct:
        note("直接写入模式：本次插入附件已明确授权。")
    elif not confirm:
        note("未执行插入。确认内容无误后，把指纹填进 --confirm-write 并设 FEISHU_ALLOW_WRITE=1 再执行。")
        raise SendBlocked("draft only", hint=json.dumps(draft, ensure_ascii=False))
    elif confirm != draft["fingerprint"]:
        raise SendBlocked(
            "指纹不匹配，拒绝写入",
            hint=f"本次写入指纹是 {draft['fingerprint']}；文件或文档引用有任何改动都会变",
        )
    if os.environ.get("FEISHU_ALLOW_WRITE") != "1":
        raise SendBlocked(
            "环境变量 FEISHU_ALLOW_WRITE=1 未设置，拒绝写入",
            hint="--direct 只免指纹核对；这道闸门没有任何命令行旗标能绕过",
        )

    await auth.prepare()
    payload = json.dumps(
        {
            "ref": ref,
            "after_heading": after_heading,
            "find": find,
            "insert_before": insert_before,
            "files": payload_files,
            "file": payload_files[0] if payload_files else {},
        },
        ensure_ascii=False,
    ).encode("utf-8")

    config.ensure_home()
    fd, temporary_name = tempfile.mkstemp(prefix=".doc-file.", suffix=".json", dir=str(config.HOME))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        from .browser_worker import run

        size_mb = max(1, total_bytes // (1024 * 1024))
        result = await run(
            "doc-insert-file",
            {"ref": ref, "payload_file": temporary_name},
            timeout=max(config.BROWSER_TIMEOUT_SECONDS, 120 + 30 * size_mb + 15 * len(payload_files)),
        )
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass

    inserted = result.get("inserted") or []
    result["verified"] = bool(inserted) and (len(result.get("reload_verified") or []) == len(inserted))
    return result


async def _paste_file_at_cursor(page, file_info: dict, diag: dict | None = None) -> bool:
    """在当前光标处粘贴一个附件（clipboard → datatransfer），返回是否被编辑器接收并完成上传。"""
    name = file_info["name"]
    before = await page.evaluate(_FILE_NAME_NEAR_COUNT_JS, name)
    size_mb = max(1, len(file_info["b64"]) * 3 // 4 // (1024 * 1024))
    deadline_polls = 60 + 20 * size_mb
    if diag is not None:
        diag.update({"file": name, "before_count": before, "attempts": []})

    for mode in ("clipboard", "datatransfer"):
        sent = False
        exc_info = ""
        if mode == "clipboard":
            try:
                await page.evaluate("() => { const e = document.activeElement; if (e && e.focus) e.focus(); }")
                await page.evaluate(_IMG_CLIPBOARD_WRITE_JS, [file_info["b64"], file_info["mime"]])
                await page.keyboard.press("Control+v")
                sent = True
            except Exception as exc:
                exc_info = f"{type(exc).__name__}: {str(exc)[:200]}"
        elif mode == "datatransfer":
            try:
                await page.evaluate(_IMG_DATATRANSFER_PASTE_JS, [file_info["b64"], file_info["mime"], name])
                sent = True
            except Exception as exc:
                exc_info = f"{type(exc).__name__}: {str(exc)[:200]}"

        if sent:
            ok = False
            for _ in range(deadline_polls):
                await page.wait_for_timeout(500)
                if await page.evaluate(_FILE_NAME_NEAR_COUNT_JS, name) > before:
                    ok = True
                    break
            if ok:
                for _ in range(40):
                    await page.wait_for_timeout(500)
                    if not await page.evaluate(_IMG_UPLOAD_BUSY_JS):
                        break
                await page.wait_for_timeout(1_000)
                if diag is not None:
                    diag["attempts"].append({"mode": mode, "status": "accepted"})
                return True
            note(f"paste_file: {mode} 方式未被接收，换下一种方式")
            await page.keyboard.press("Control+z")
            await page.wait_for_timeout(300)
            if diag is not None:
                diag["attempts"].append({"mode": mode, "status": "timeout"})
        else:
            if diag is not None:
                diag["attempts"].append({"mode": mode, "status": "failed_to_send", "error": exc_info})
    return False


async def _insert_file_impl(ref: str, payload_file: str) -> dict:
    """Browser Worker implementation: 把文件（单个或批量）粘贴为文档附件块。"""
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("插入附件只能由受限 Browser Worker 执行")
    payload_path = os.path.realpath(payload_file)
    home_path = os.path.realpath(config.HOME)
    if os.path.commonpath((home_path, payload_path)) != home_path:
        raise FeishuError("Browser Worker 拒绝读取 FEISHU_HOME 之外的写入文件")
    try:
        payload = json.loads(_read_doc_write_file(payload_path).decode("utf-8"))
        files = payload.get("files")
        if not files and "file" in payload and payload["file"]:
            files = [payload["file"]]
        after_heading = str(payload.get("after_heading") or "")
        find_anchor = str(payload.get("find") or "")
        insert_before = bool(payload.get("insert_before"))
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise FeishuError("insert-file payload 必须是 JSON（含 files/after_heading）") from exc
    if not files:
        raise FeishuError("insert-file 需要至少一个文件")

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FeishuError("当前 venv 没有 playwright") from exc

    data = auth.load_cookies()
    domain = data.get("domain") or config.DOMAIN
    kind, token = parse_doc_ref(ref)
    if kind != "docx":
        raise FeishuError("插入附件目前只支持 docx 文档")
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
            await page.goto(url, wait_until="load", timeout=30_000)
            await auth.raise_if_login_redirect(page)
            await page.wait_for_timeout(2_000)

            editor = page.locator(".page-block.root-block").first
            if not await editor.count():
                raise FeishuError("文档编辑器未渲染", hint="飞书网页结构可能已变化")

            # 基线：全文（滚动收集）已含各文件名的块数
            baseline_counts = {}
            for f in files:
                fname = f["name"]
                if fname not in baseline_counts:
                    baseline_counts[fname] = await page.evaluate(_FILE_NAME_BLOCKS_COUNT_JS, fname)

            # 定位插入点
            await page.evaluate(_SCROLL_BOTTOM_JS)
            await page.wait_for_timeout(800)
            if find_anchor:
                target_id = await _locate_unique_block(page, find_anchor, kind_hint="锚点块")
                if not await page.evaluate(_SCROLL_TO_BLOCK_JS, target_id):
                    raise FeishuError(f"锚点块 {target_id} 滚动后仍未渲染，无法定位插入点")
                target = page.locator(f"[data-block-id='{target_id}']")
                await target.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)
                if insert_before:
                    if await page.evaluate(_FOCUS_BLOCK_START_JS, target_id) != "ok":
                        raise FeishuError("无法在目标块开头建立文本光标")
                    await page.wait_for_timeout(200)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(300)
                    await page.keyboard.press("ArrowUp")
                    await page.wait_for_timeout(300)
                else:
                    if await page.evaluate(_FOCUS_BLOCK_END_JS, target_id) != "ok":
                        raise FeishuError("无法在目标块末尾建立文本光标")
                    await page.wait_for_timeout(200)
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(300)
            elif after_heading:
                needle = re.sub(r"\s+", " ", after_heading).strip()
                matches = await page.evaluate(_COLLECT_HEADING_MATCHES_JS, needle)
                if not matches:
                    raise FeishuError(
                        f"找不到标题块「{after_heading}」",
                        hint="--after-heading 只匹配标题块，且文本需与标题精确相等",
                    )
                if len(matches) > 1:
                    raise FeishuError(
                        f"标题块「{after_heading}」命中 {len(matches)} 个，无法确定插入位置",
                        hint="换一个全文唯一的标题文本",
                    )
                target_id = matches[0]
                if not await page.evaluate(_SCROLL_TO_BLOCK_JS, target_id):
                    raise FeishuError(f"目标块 {target_id} 滚动后仍未渲染，无法定位插入点")
                target = page.locator(f"[data-block-id='{target_id}']")
                await target.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)
                if await page.evaluate(_FOCUS_BLOCK_END_JS, target_id) != "ok":
                    raise FeishuError("无法在目标块内建立文本光标（块类型不支持？）")
                await page.wait_for_timeout(200)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(300)
            else:
                target_id = await page.evaluate(_FIND_LAST_EDITABLE_BLOCK_JS)
                if not target_id:
                    raise FeishuError("找不到可编辑的文档末尾块")
                if not await page.evaluate(_SCROLL_TO_BLOCK_JS, target_id):
                    raise FeishuError(f"目标块 {target_id} 滚动后仍未渲染，无法定位插入点")
                target = page.locator(f"[data-block-id='{target_id}']")
                await target.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)
                if await page.evaluate(_FOCUS_BLOCK_END_JS, target_id) != "ok":
                    raise FeishuError("无法在目标块内建立文本光标（块类型不支持？）")
                await page.wait_for_timeout(200)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(300)

            inserted: list[str] = []
            failed: list[str] = []
            diagnostics: list[dict] = []
            rollback_unconfirmed = None

            for index, file_info in enumerate(files):
                name = file_info["name"]
                if not await page.evaluate(_IS_EDITABLE_FOCUSED_JS):
                    await page.keyboard.press("ArrowDown")
                    await page.wait_for_timeout(300)

                if index > 0:
                    snapshot_pre = await page.evaluate(_BLOCK_SNAPSHOT_JS)
                    if snapshot_pre and snapshot_pre.get("id"):
                        await page.keyboard.press("Enter")
                        await page.wait_for_timeout(300)

                snapshot = await page.evaluate(_BLOCK_SNAPSHOT_JS)
                if not snapshot or not snapshot.get("id"):
                    raise FeishuError(
                        "无法确定光标所在的文档块，中止插入附件",
                        hint="此时文档尚未被本次操作修改；请人工打开文档确认后重试",
                    )

                diag: dict = {}
                if await _paste_file_at_cursor(page, file_info, diag):
                    inserted.append(name)
                    note(f"insert-file: 第 {index + 1}/{len(files)} 个附件已插入：{name}")
                    cur_snap = await page.evaluate(_BLOCK_SNAPSHOT_JS)
                    cur_bid = cur_snap.get("id") if cur_snap else ""
                    if cur_bid:
                        res = await page.evaluate(_ENSURE_EDITABLE_AFTER_BLOCK_JS, cur_bid)
                        if res != "next-focused":
                            await page.keyboard.press("ArrowDown")
                            await page.wait_for_timeout(300)
                else:
                    diag_snap = await page.evaluate("""() => {
                        const e = document.activeElement;
                        const popups = [...document.querySelectorAll('[class*=paste], [class*=modal], [class*=dialog], [class*=upload]')]
                            .filter(x => x.offsetWidth > 0)
                            .map(x => (x.className || '').slice(0, 60) + ':' + (x.innerText || '').slice(0, 40));
                        return {
                            activeElement: e ? e.tagName + '.' + (e.className || '') : 'none',
                            popups,
                        };
                    }""")
                    shot = config.HOME / f"debug-insert-file-{int(__import__('time').time())}.png"
                    try:
                        await page.screenshot(path=str(shot), full_page=False)
                        diag_snap["screenshot"] = str(shot)
                    except Exception:
                        pass
                    diag["diagnostics"] = diag_snap
                    diagnostics.append(diag)
                    failed.append(name)
                    note(f"insert-file: 附件未被编辑器接收，执行回退：{name}")
                    await page.keyboard.press("Control+z")
                    await page.wait_for_timeout(400)
                    cur = await page.evaluate(_BLOCK_SNAPSHOT_JS)
                    cur_id = cur.get("id") if cur else ""
                    orig_id = snapshot.get("id") or ""
                    next_id = snapshot.get("next_id") or ""
                    if cur and cur_id and cur_id != orig_id and cur_id != next_id:
                        if cur.get("text"):
                            await page.keyboard.press("Home")
                            await page.keyboard.press("Shift+End")
                            await page.keyboard.press("Delete")
                            await page.wait_for_timeout(200)
                        await page.keyboard.press("Backspace")
                        await page.wait_for_timeout(300)
                    after = await page.evaluate(_BLOCK_STATE_BY_ID_JS, snapshot["id"])
                    rollback_ok = (
                        after is not None
                        and after["text"] == snapshot["text"]
                        and after["next_id"] == snapshot["next_id"]
                        and after["next_text"] == snapshot["next_text"]
                    )
                    if not rollback_ok:
                        rollback_unconfirmed = {
                            "file": name,
                            "block_id": snapshot["id"],
                            "expected": snapshot,
                            "actual": after,
                            "note": "回退未能确认恢复原样，需人工检查该块及其前后内容",
                        }
                        break
            await page.wait_for_timeout(1_500)

            # 复核：重载页面滚动全文按文件名计数
            reload_verified: list[str] = []
            if inserted:
                await page.reload(wait_until="load", timeout=30_000)
                await auth.raise_if_login_redirect(page)
                await page.wait_for_timeout(2_000)
                for name in inserted:
                    after_count = await page.evaluate(_FILE_NAME_BLOCKS_COUNT_JS, name)
                    if after_count > baseline_counts.get(name, 0):
                        reload_verified.append(name)
                    else:
                        note(f"insert-file: 重载复核文件名块数不足：{name}（基线 {baseline_counts.get(name, 0)}，现 {after_count}）")

            status = "inserted" if (len(inserted) == len(files) and not failed) else ("partial" if inserted else "failed")
            res_dict = {
                "status": status,
                "url": url,
                "inserted": inserted,
                "failed": failed,
                "reload_verified": reload_verified,
                "after_heading": after_heading,
                "find": find_anchor,
                "diagnostics": diagnostics,
            }
            if rollback_unconfirmed:
                res_dict["rollback_unconfirmed"] = rollback_unconfirmed
            return res_dict
        finally:
            await ctx.close()


# ---------------------------------------------------------------------------
# 块级外科编辑（surgical block edit）：像人一样把光标停到目标块再增删改
# ---------------------------------------------------------------------------

# 通用块匹配：叶块（内部不再嵌套 data-block-id）归一化文本精确相等
_COLLECT_BLOCK_MATCHES_JS = """async (needle) => {
  const norm = s => (s || '').replace(/[\\s\\u200b\\u200e\\u200f\\ufeff]+/g, ' ').trim();
  const ed = document.querySelector('[data-block-id]');
  let scroller = ed;
  while (scroller && scroller.scrollHeight <= scroller.clientHeight + 50)
    scroller = scroller.parentElement;
  if (!scroller) scroller = document.scrollingElement || document.documentElement;
  const seen = new Map();
  const collect = () => {
    for (const b of document.querySelectorAll('[data-block-id]')) {
      if (b.querySelector('[data-block-id]')) continue;
      if (norm(b.innerText) !== needle) continue;
      seen.set(b.getAttribute('data-block-id'), true);
    }
  };
  scroller.scrollTop = 0;
  await new Promise(r => setTimeout(r, 300));
  const step = Math.max(scroller.clientHeight, 400);
  for (let y = 0; y <= scroller.scrollHeight + step; y += step) {
    scroller.scrollTop = y;
    await new Promise(r => setTimeout(r, 200));
    collect();
  }
  scroller.scrollTop = scroller.scrollHeight;
  await new Promise(r => setTimeout(r, 300));
  collect();
  return [...seen.keys()];
}"""

# 用 Selection API 把文本光标精确放到目标块的可编辑区末尾（不依赖鼠标坐标）
_FOCUS_BLOCK_END_JS = """(bid) => {
  const block = document.querySelector('[data-block-id="' + bid + '"]');
  if (!block) return 'missing';
  const editable = block.querySelector('[contenteditable="true"]') || block;
  editable.focus();
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(editable);
  range.collapse(false);
  sel.removeAllRanges();
  sel.addRange(range);
  const node = sel.anchorNode;
  const el = node && (node.nodeType === 1 ? node : node.parentElement);
  const ok = !!(el && el.closest && el.closest('[contenteditable="true"]'));
  return ok ? 'ok' : 'no-editable';
}"""

# 把文本光标放到目标块可编辑区的开头（insert-before 用）
_FOCUS_BLOCK_START_JS = """(bid) => {
  const block = document.querySelector('[data-block-id="' + bid + '"]');
  if (!block) return 'missing';
  const editable = block.querySelector('[contenteditable="true"]') || block;
  editable.focus();
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(editable);
  range.collapse(true);
  sel.removeAllRanges();
  sel.addRange(range);
  const node = sel.anchorNode;
  const el = node && (node.nodeType === 1 ? node : node.parentElement);
  const ok = !!(el && el.closest && el.closest('[contenteditable="true"]'));
  return ok ? 'ok' : 'no-editable';
}"""

# 探测当前选区是否已在可编辑区域内
_IS_EDITABLE_FOCUSED_JS = """() => {
  const sel = window.getSelection();
  const el = sel && sel.anchorNode ? (sel.anchorNode.nodeType === 1 ? sel.anchorNode : sel.anchorNode.parentElement) : null;
  return !!(el && el.closest && el.closest('[contenteditable="true"]'));
}"""

# 脱困辅助：若指定块后已有可编辑文本块则精确聚焦到其开头，否则返回需按 ArrowDown
_ENSURE_EDITABLE_AFTER_BLOCK_JS = """(bid) => {
  const block = document.querySelector('[data-block-id="' + bid + '"]');
  if (!block) return 'missing';
  let nxt = block.nextElementSibling;
  while (nxt && !nxt.getAttribute('data-block-id')) nxt = nxt.nextElementSibling;
  if (nxt) {
    const editable = nxt.querySelector('[contenteditable="true"]') || nxt;
    if (editable && (editable.isContentEditable || editable.getAttribute('contenteditable') === 'true')) {
      editable.focus();
      const sel = window.getSelection();
      sel.removeAllRanges();
      const range = document.createRange();
      range.selectNodeContents(editable);
      range.collapse(true);
      sel.addRange(range);
      return 'next-focused';
    }
  }
  return 'need-arrow-down';
}"""

# 选中目标块可编辑区的全部内容（replace-block 用）
_SELECT_BLOCK_CONTENT_JS = """(bid) => {
  const block = document.querySelector('[data-block-id="' + bid + '"]');
  if (!block) return 'missing';
  const editable = block.querySelector('[contenteditable="true"]') || block;
  editable.focus();
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(editable);
  sel.removeAllRanges();
  sel.addRange(range);
  const node = sel.anchorNode;
  const el = node && (node.nodeType === 1 ? node : node.parentElement);
  const ok = !!(el && el.closest && el.closest('[contenteditable="true"]'));
  return ok ? 'ok' : 'no-editable';
}"""

# 全文滚动收集所有叶块归一化文本（删除/替换后的核验用）
_COLLECT_ALL_BLOCK_TEXTS_JS = """async () => {
  const norm = s => (s || '').replace(/[\\s\\u200b\\u200e\\u200f\\ufeff]+/g, ' ').trim();
  const ed = document.querySelector('[data-block-id]');
  let scroller = ed;
  while (scroller && scroller.scrollHeight <= scroller.clientHeight + 50)
    scroller = scroller.parentElement;
  if (!scroller) scroller = document.scrollingElement || document.documentElement;
  const texts = new Set();
  const collect = () => {
    for (const b of document.querySelectorAll('[data-block-id]')) {
      if (b.querySelector('[data-block-id]')) continue;
      const t = norm(b.innerText);
      if (t) texts.add(t);
    }
  };
  scroller.scrollTop = 0;
  await new Promise(r => setTimeout(r, 300));
  const step = Math.max(scroller.clientHeight, 400);
  for (let y = 0; y <= scroller.scrollHeight + step; y += step) {
    scroller.scrollTop = y;
    await new Promise(r => setTimeout(r, 200));
    collect();
  }
  scroller.scrollTop = scroller.scrollHeight;
  await new Promise(r => setTimeout(r, 300));
  collect();
  return [...texts];
}"""


async def _locate_unique_block(page, anchor: str, *, kind_hint: str = "正文块") -> str:
    """全文滚动收集，定位归一化文本与 anchor 精确相等的唯一叶块，返回 block_id。"""
    needle = re.sub(r"\s+", " ", anchor).strip()
    matches = await page.evaluate(_COLLECT_BLOCK_MATCHES_JS, needle)
    if not matches:
        raise FeishuError(
            f"找不到块「{anchor[:60]}」",
            hint="锚点按叶块归一化文本精确匹配；代码块/表格等多行内容请用整段文本或换更短的唯一片段",
        )
    if len(matches) > 1:
        raise FeishuError(
            f"锚点「{anchor[:60]}」命中 {len(matches)} 个{kind_hint}，无法确定位置",
            hint="换一个全文唯一的锚点文本",
        )
    block_id = matches[0]
    if not await page.evaluate(_SCROLL_TO_BLOCK_JS, block_id):
        raise FeishuError(f"目标块 {block_id} 滚动后仍未渲染，无法定位")
    return block_id


async def _paste_rich_at_cursor(page, markdown_text: str, html_body: str, plain_text: str) -> str:
    """在当前光标处粘贴富文本（对齐 _paste_content 的三级降级），返回实际方式。"""
    try:
        await page.evaluate(_RICH_CLIPBOARD_JS, [html_body, plain_text])
        await page.keyboard.press("Control+v")
        return "html"
    except Exception:
        pass
    try:
        await page.evaluate(_DATATRANSFER_PASTE_JS, [html_body, plain_text])
        return "html-datatransfer"
    except Exception:
        pass
    await page.evaluate("text => navigator.clipboard.writeText(text)", markdown_text)
    await page.keyboard.press("Control+v")
    return "plain"


async def _paste_image_at_cursor(page, image: dict, diag: dict | None = None,
                                 watch_block_id: str = "") -> bool:
    """在当前光标处粘贴一张图片（clipboard → datatransfer → drop），返回是否被编辑器接收。

    watch_block_id 给出时用位置感知检测（该块的“下一块”出现图片内容即成功），
    免疫虚拟化渲染造成的全局图片计数抖动；否则退化为全局计数对比。
    diag 传入时会回写诊断信息（方式/异常/计数/失败截图），worker stderr 不外传，
    诊断必须走返回值。
    """
    size_mb = max(1, len(image["b64"]) * 3 // 4 // (1024 * 1024))
    deadline_polls = 60 + 20 * size_mb
    before = await page.evaluate(_IMG_COUNT_NEAR_JS)
    if diag is not None:
        diag.update({"image": image["name"], "before_imgs": before, "attempts": []})

    async def _detected() -> bool:
        if watch_block_id:
            probe = await page.evaluate(_NEXT_BLOCK_IS_IMAGE_JS, watch_block_id)
            if probe and probe.get("isImage"):
                return True
        return await page.evaluate(_IMG_COUNT_NEAR_JS) > before
    for mode in ("clipboard", "datatransfer", "drop"):
        sent = False
        exc_info = ""
        if mode == "clipboard":
            try:
                await page.evaluate("() => { const e = document.activeElement; if (e && e.focus) e.focus(); }")
                await page.evaluate(_IMG_CLIPBOARD_WRITE_JS, [image["b64"], image["mime"]])
                await page.keyboard.press("Control+v")
                sent = True
            except Exception as exc:
                exc_info = f"{type(exc).__name__}: {str(exc)[:200]}"
        elif mode == "datatransfer":
            try:
                await page.evaluate(_IMG_DATATRANSFER_PASTE_JS,
                                    [image["b64"], image["mime"], image["name"]])
                sent = True
            except Exception as exc:
                exc_info = f"{type(exc).__name__}: {str(exc)[:200]}"
        else:
            try:
                await page.evaluate(_IMG_DROP_JS,
                                    [image["b64"], image["mime"], image["name"]])
                sent = True
            except Exception as exc:
                exc_info = f"{type(exc).__name__}: {str(exc)[:200]}"
        if diag is not None:
            diag["attempts"].append({"mode": mode, "sent": sent, "error": exc_info})
        if not sent:
            continue
        for _poll_i in range(deadline_polls):
            await page.wait_for_timeout(500)
            if await _detected():
                await page.wait_for_timeout(2_000)  # 等上传与撤销栈稳定
                if diag is not None:
                    diag["result"] = f"inserted via {mode}"
                return True
        await page.keyboard.press("Control+z")
        await page.wait_for_timeout(300)
    if diag is not None:
        diag["after_imgs"] = await page.evaluate(_IMG_COUNT_NEAR_JS)
        diag["activeElement"] = await page.evaluate(
            "() => { const e = document.activeElement; return e ? e.tagName + '.' + (e.className || '') : 'none'; }")
        diag["selection"] = await page.evaluate(
            """() => { const s = window.getSelection();
                       const n = s && s.anchorNode;
                       const el = n && (n.nodeType === 1 ? n : n.parentElement);
                       return el && el.closest ? (el.closest('[contenteditable="true"]') ? 'in-editable' : 'not-editable') : 'none'; }""")
        try:
            shot = config.HOME / f"debug-edit-img-{int(__import__('time').time())}.png"
            await page.screenshot(path=str(shot), full_page=False)
            diag["screenshot"] = str(shot)
        except Exception:
            pass
    return False


def make_doc_edit_draft(ref: str, op: str, anchor: str, content_desc: str, content_sha: str) -> dict:
    kind, token = parse_doc_ref(ref)
    return {
        "action": "block_edit",
        "op": op,
        "document_token": token,
        "anchor": anchor,
        "content": content_desc,
        "fingerprint": doc_write_fingerprint(f"edit-{op}", token, content_sha + anchor),
    }


async def edit_block(
    ref: str,
    find: str,
    op: str,
    text: str = "",
    text_file: str = "",
    images: list[str] | None = None,
    attach: list[str] | None = None,
    confirm: str = "",
    direct: bool = False,
) -> dict:
    """在已有 docx 文档上做块级外科编辑（不全文替换）。

    op:
      insert-after  在锚点块之后插入内容（--text/--text-file 富文本，--images 图片，或 --attach 附件）
      insert-before 在锚点块之前插入内容（同上；实现在锚点块首造空段后粘贴）
      replace       替换锚点块的正文内容（纯文本/富文本，限单个文本块）
      delete        删除锚点块（限文本块）
      delete-next   删除锚点块的下一个块（用于图片等无文本块）
    """
    if op not in ("insert-after", "insert-before", "replace", "delete", "delete-next"):
        raise FeishuError(f"不支持的编辑操作：{op}")
    if not find.strip():
        raise FeishuError("doc edit 需要 --find 锚点文本")
    kind, _token = parse_doc_ref(ref)
    if kind != "docx":
        raise FeishuError("块级编辑目前只支持 docx 文档")

    import base64 as _b64
    from . import md_html

    markdown_text, html_body, plain_text = "", "", ""
    payload_images: list[dict] = []
    payload_files: list[dict] = []
    content_desc = "(无)"
    if op in ("insert-after", "insert-before", "replace"):
        if attach:
            if op == "replace":
                raise FeishuError("replace 暂不支持附件内容（请先 delete 再 insert-after）")
            if len(attach) > MAX_INSERT_FILES:
                raise FeishuError(f"一次最多 {MAX_INSERT_FILES} 个附件")
            import mimetypes as _mimetypes
            total_b64 = 0
            for path in attach:
                real = os.path.realpath(path)
                if not os.path.isfile(real):
                    raise FeishuError(f"附件不存在：{path}")
                if os.path.getsize(real) > MAX_INSERT_FILE_BYTES:
                    raise FeishuError(f"单文件超过 100MB 上限：{path}")
                mime = _mimetypes.guess_type(real)[0] or "application/octet-stream"
                with open(real, "rb") as f:
                    b64 = _b64.b64encode(f.read()).decode("ascii")
                total_b64 += len(b64)
                payload_files.append({"name": os.path.basename(real), "mime": mime, "b64": b64, "size": os.path.getsize(real)})
            if total_b64 > config.MAX_DOC_WRITE_BYTES:
                raise FeishuError(f"附件总体积超过上限（base64 后 {total_b64} bytes）")
            content_desc = f"{len(payload_files)} 个附件"
        elif images:
            if len(images) > MAX_INSERT_IMAGES:
                raise FeishuError(f"一次最多 {MAX_INSERT_IMAGES} 张图片")
            total_b64 = 0
            for path in images:
                real = os.path.realpath(path)
                if not os.path.isfile(real):
                    raise FeishuError(f"图片不存在：{path}")
                ext = os.path.splitext(real)[1].lower()
                mime = _INSERT_IMAGE_MIME.get(ext)
                if not mime:
                    raise FeishuError(f"不支持的图片格式：{path}")
                if os.path.getsize(real) > _INSERT_IMAGE_MAX_BYTES:
                    raise FeishuError(f"单张图片超过 10MB 上限：{path}")
                with open(real, "rb") as f:
                    b64 = _b64.b64encode(f.read()).decode("ascii")
                total_b64 += len(b64)
                payload_images.append({"name": os.path.basename(real), "mime": mime, "b64": b64})
            if total_b64 > config.MAX_DOC_WRITE_BYTES:
                raise FeishuError(f"图片总体积超过上限（base64 后 {total_b64} bytes）")
            content_desc = f"{len(payload_images)} 张图片"
        else:
            if text_file:
                markdown_text = _read_doc_write_file(text_file).decode("utf-8", errors="replace")
            else:
                markdown_text = text
            if not markdown_text.strip():
                raise FeishuError(f"{op} 需要 --text/--text-file、--images 或 --attach 提供内容")
            html_body, plain_text = md_html.markdown_to_html(markdown_text)
            html_body = md_html.wrap_clipboard_html(html_body)
            content_desc = f"文本 {len(markdown_text)} 字符"
    elif text or text_file or images or attach:
        raise FeishuError(f"{op} 不接受内容参数")

    content_sha = hashlib.sha256(
        (markdown_text + "|" + ",".join(i["name"] for i in payload_images) + "|" + ",".join(f["name"] for f in payload_files)).encode()
    ).hexdigest()
    draft = make_doc_edit_draft(ref, op, find, content_desc, content_sha)
    if direct:
        note("直接写入模式：本次块级编辑已明确授权。")
    elif not confirm:
        note("未执行编辑。确认内容无误后，把指纹填进 --confirm-write 并设 FEISHU_ALLOW_WRITE=1 再执行。")
        raise SendBlocked("draft only", hint=json.dumps(draft, ensure_ascii=False))
    elif confirm != draft["fingerprint"]:
        raise SendBlocked(
            "指纹不匹配，拒绝写入",
            hint=f"本次写入指纹是 {draft['fingerprint']}；锚点或内容有任何改动都会变",
        )
    if os.environ.get("FEISHU_ALLOW_WRITE") != "1":
        raise SendBlocked(
            "环境变量 FEISHU_ALLOW_WRITE=1 未设置，拒绝写入",
            hint="--direct 只免指纹核对；这道闸门没有任何命令行旗标能绕过",
        )

    await auth.prepare()
    payload = json.dumps(
        {
            "ref": ref,
            "op": op,
            "anchor": find,
            "markdown": markdown_text,
            "html": html_body,
            "plain": plain_text,
            "images": payload_images,
            "files": payload_files,
        },
        ensure_ascii=False,
    ).encode("utf-8")

    config.ensure_home()
    fd, temporary_name = tempfile.mkstemp(prefix=".doc-edit.", suffix=".json", dir=str(config.HOME))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        from .browser_worker import run

        result = await run(
            "doc-block-edit",
            {"ref": ref, "payload_file": temporary_name},
            timeout=max(config.BROWSER_TIMEOUT_SECONDS, 120 + 45 * max(1, len(payload_images))),
        )
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass

    result["verified"] = bool(result.get("reload_verified"))
    return result


async def _block_edit_impl(ref: str, payload_file: str) -> dict:
    """Browser Worker implementation: 定位锚点块，执行一次块级增/删/改并核验。"""
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("块级编辑只能由受限 Browser Worker 执行")
    payload_path = os.path.realpath(payload_file)
    home_path = os.path.realpath(config.HOME)
    if os.path.commonpath((home_path, payload_path)) != home_path:
        raise FeishuError("Browser Worker 拒绝读取 FEISHU_HOME 之外的写入文件")
    try:
        payload = json.loads(_read_doc_write_file(payload_path).decode("utf-8"))
        op = str(payload["op"])
        anchor = str(payload["anchor"])
        markdown_text = str(payload.get("markdown") or "")
        html_body = str(payload.get("html") or "")
        plain_text = str(payload.get("plain") or "")
        images = payload.get("images") or []
        files = payload.get("files") or []
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise FeishuError("block-edit payload 必须是 JSON（含 op/anchor/...）") from exc

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise FeishuError("当前 venv 没有 playwright") from exc

    data = auth.load_cookies()
    domain = data.get("domain") or config.DOMAIN
    kind, token = parse_doc_ref(ref)
    if kind != "docx":
        raise FeishuError("块级编辑目前只支持 docx 文档")
    url = f"https://{domain}/docx/{token}"
    needle = re.sub(r"\s+", " ", anchor).strip()
    # 核验针用渲染后首个非空块的纯文本，防止首行（如标题）不足 40 字时跨段导致单块匹配必败
    _first_lines = [l.strip() for l in (plain_text or markdown_text).splitlines() if l.strip()]
    _first_probe = _first_lines[0] if _first_lines else ""
    _clean_probe = re.sub(r"[`*#~\[\]()_]", "", _first_probe)
    _clean_probe = re.sub(r"\s+", " ", _clean_probe).strip()
    new_needle = _clean_probe[:min(40, len(_clean_probe))] if len(_clean_probe) >= 4 else re.sub(r"\s+", " ", plain_text or markdown_text).strip()[:40]

    def _text_matched(candidate: str, block_texts: list[str]) -> bool:
        if not candidate:
            return False
        if any(candidate in t for t in block_texts):
            return True
        cn = re.sub(r"[`*#~\[\]()_\s​‎‏﻿]+", "", candidate)
        if len(cn) >= 4:
            return any(cn in re.sub(r"[`*#~\[\]()_\s​‎‏﻿]+", "", t) for t in block_texts)
        return False

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
            await page.goto(url, wait_until="load", timeout=30_000)
            await auth.raise_if_login_redirect(page)
            await page.wait_for_timeout(2_000)

            editor = page.locator(".page-block.root-block").first
            if not await editor.count():
                raise FeishuError("文档编辑器未渲染", hint="飞书网页结构可能已变化")

            diagnostics: list[dict] = []
            baseline_img_count = await page.evaluate(_COUNT_ALL_IMAGES_JS)
            baseline_file_counts = {}
            if files:
                for f in files:
                    fname = f["name"]
                    if fname not in baseline_file_counts:
                        baseline_file_counts[fname] = await page.evaluate(_FILE_NAME_BLOCKS_COUNT_JS, fname)
            block_id = await _locate_unique_block(page, anchor)
            snapshot = await page.evaluate(_BLOCK_STATE_BY_ID_JS, block_id)
            if not snapshot:
                raise FeishuError(f"锚点块 {block_id} 状态读取失败，中止编辑")

            detail = ""
            if op == "insert-after":
                if await page.evaluate(_FOCUS_BLOCK_END_JS, block_id) != "ok":
                    raise FeishuError("无法在锚点块内建立文本光标（块类型不支持？）")
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(300)
                if images:
                    inserted_imgs = []
                    watch_id = block_id
                    for image in images:
                        diag: dict = {}
                        if await _paste_image_at_cursor(page, image, diag, watch_block_id=watch_id):
                            inserted_imgs.append(image["name"])
                            # 下一张的观察点 = 刚插入的图片块（锚点的当前下一块）
                            probe = await page.evaluate(_BLOCK_STATE_BY_ID_JS, block_id)
                            if probe and probe.get("next_id"):
                                watch_id = probe["next_id"]
                        else:
                            diagnostics.append(diag)
                            break
                    if len(inserted_imgs) != len(images):
                        for _ in range(4):
                            await page.keyboard.press("Control+z")
                            await page.wait_for_timeout(300)
                        return {
                            "status": "failed",
                            "url": url,
                            "op": op,
                            "anchor": anchor,
                            "detail": f"图片粘贴未全部被接收（{len(inserted_imgs)}/{len(images)}），已撤销本次编辑",
                            "reload_verified": False,
                            "diagnostics": diagnostics,
                        }
                    detail = f"插入 {len(inserted_imgs)} 张图片"
                elif files:
                    inserted_files = []
                    for f in files:
                        diag: dict = {}
                        if await _paste_file_at_cursor(page, f, diag):
                            inserted_files.append(f["name"])
                            await page.wait_for_timeout(500)
                        else:
                            diagnostics.append(diag)
                            break
                    if len(inserted_files) != len(files):
                        for _ in range(4):
                            await page.keyboard.press("Control+z")
                            await page.wait_for_timeout(300)
                        return {
                            "status": "failed",
                            "url": url,
                            "op": op,
                            "anchor": anchor,
                            "detail": f"附件粘贴未全部被接收（{len(inserted_files)}/{len(files)}），已撤销本次编辑",
                            "reload_verified": False,
                            "diagnostics": diagnostics,
                        }
                    detail = f"插入 {len(inserted_files)} 个附件"
                else:
                    await _paste_rich_at_cursor(page, markdown_text, html_body, plain_text)
                    await page.wait_for_timeout(800)
                    detail = f"插入文本 {len(markdown_text)} 字符"

            elif op == "insert-before":
                if images:
                    # 图片不存在“并段”问题：直接把光标放到上一块末尾粘贴，
                    # 图片块即落在上一块与锚点之间 = 锚点之前。
                    # （走 Enter+ArrowUp 的话，0 高度空段会被光标导航跳过，
                    #   焦点丢失后剪贴板粘贴会打空——实测踩过）
                    prev_id = await page.evaluate(
                        """(bid) => {
                             const b = document.querySelector('[data-block-id="' + bid + '"]');
                             let p = b && b.previousElementSibling;
                             while (p && !p.getAttribute('data-block-id')) p = p.previousElementSibling;
                             return p ? p.getAttribute('data-block-id') : '';
                           }""",
                        block_id,
                    )
                    placed = False
                    if prev_id and await page.evaluate(_FOCUS_BLOCK_END_JS, prev_id) == "ok":
                        placed = True
                    else:
                        if await page.evaluate(_FOCUS_BLOCK_START_JS, block_id) != "ok":
                            raise FeishuError("无法在锚点区域建立文本光标（块类型不支持？）")
                        await page.keyboard.press("Enter")
                        await page.wait_for_timeout(300)
                        await page.keyboard.press("ArrowUp")
                        await page.wait_for_timeout(200)
                        placed = True
                    inserted_imgs = []
                    watch_id = prev_id if prev_id else block_id
                    for image in images:
                        diag: dict = {}
                        if await _paste_image_at_cursor(page, image, diag, watch_block_id=watch_id):
                            inserted_imgs.append(image["name"])
                            probe = await page.evaluate(_BLOCK_STATE_BY_ID_JS, watch_id)
                            if probe and probe.get("next_id"):
                                watch_id = probe["next_id"]
                        else:
                            diagnostics.append(diag)
                            break
                    if len(inserted_imgs) != len(images):
                        for _ in range(4):
                            await page.keyboard.press("Control+z")
                            await page.wait_for_timeout(300)
                        raise FeishuError(
                            f"图片粘贴未全部被接收（{len(inserted_imgs)}/{len(images)}），已撤销本次编辑",
                            hint="可换更小的图片重试；文档已恢复原样",
                        )
                    detail = f"在锚点前插入 {len(inserted_imgs)} 张图片"
                elif files:
                    prev_id = await page.evaluate(
                        """(bid) => {
                             const b = document.querySelector('[data-block-id="' + bid + '"]');
                             let p = b && b.previousElementSibling;
                             while (p && !p.getAttribute('data-block-id')) p = p.previousElementSibling;
                             return p ? p.getAttribute('data-block-id') : '';
                           }""",
                        block_id,
                    )
                    placed = False
                    if prev_id and await page.evaluate(_FOCUS_BLOCK_END_JS, prev_id) == "ok":
                        placed = True
                    else:
                        if await page.evaluate(_FOCUS_BLOCK_START_JS, block_id) != "ok":
                            raise FeishuError("无法在锚点区域建立文本光标（块类型不支持？）")
                        await page.keyboard.press("Enter")
                        await page.wait_for_timeout(300)
                        await page.keyboard.press("ArrowUp")
                        await page.wait_for_timeout(200)
                        placed = True
                    inserted_files = []
                    for f in files:
                        diag: dict = {}
                        if await _paste_file_at_cursor(page, f, diag):
                            inserted_files.append(f["name"])
                            await page.wait_for_timeout(500)
                        else:
                            diagnostics.append(diag)
                            break
                    if len(inserted_files) != len(files):
                        for _ in range(4):
                            await page.keyboard.press("Control+z")
                            await page.wait_for_timeout(300)
                        raise FeishuError(
                            f"附件粘贴未全部被接收（{len(inserted_files)}/{len(files)}），已撤销本次编辑",
                            hint="可换更小的文件重试；文档已恢复原样",
                        )
                    detail = f"在锚点前插入 {len(inserted_files)} 个附件"
                else:
                    if await page.evaluate(_FOCUS_BLOCK_START_JS, block_id) != "ok":
                        raise FeishuError("无法在锚点块内建立文本光标（块类型不支持？）")
                    # 块首 Enter 在上方造空段，ArrowUp 进入空段再粘贴
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(300)
                    await page.keyboard.press("ArrowUp")
                    await page.wait_for_timeout(200)
                    await _paste_rich_at_cursor(page, markdown_text, html_body, plain_text)
                    await page.wait_for_timeout(800)
                    detail = f"在锚点前插入文本 {len(markdown_text)} 字符"

            elif op == "replace":
                if images or files:
                    raise FeishuError("replace 暂不支持图片或附件内容（先 delete-next/delete 再 insert-after）")
                if await page.evaluate(_SELECT_BLOCK_CONTENT_JS, block_id) != "ok":
                    raise FeishuError("无法在锚点块内建立选区（块类型不支持？）")
                await _paste_rich_at_cursor(page, markdown_text, html_body, plain_text)
                await page.wait_for_timeout(800)
                detail = f"替换为 {len(markdown_text)} 字符"

            elif op == "delete":
                if await page.evaluate(_SELECT_BLOCK_CONTENT_JS, block_id) != "ok":
                    raise FeishuError("无法在锚点块内建立选区（非文本块请用 delete-next）")
                await page.keyboard.press("Delete")
                await page.wait_for_timeout(300)
                await page.keyboard.press("Backspace")
                await page.wait_for_timeout(400)
                detail = "删除锚点块"

            elif op == "delete-next":
                if await page.evaluate(_FOCUS_BLOCK_END_JS, block_id) != "ok":
                    raise FeishuError("无法在锚点块内建立文本光标")
                # 段落末尾前向 Delete：第一下选中下一块（图片等），第二下删除
                for _ in range(2):
                    await page.keyboard.press("Delete")
                    await page.wait_for_timeout(400)
                    cur = await page.evaluate(_BLOCK_STATE_BY_ID_JS, block_id)
                    if cur and cur.get("next_id") != snapshot.get("next_id"):
                        break
                detail = "删除锚点的下一块"

            # ---- 操作后核验 + 失败回退 ----
            ok = False
            if op == "delete":
                texts = await page.evaluate(_COLLECT_ALL_BLOCK_TEXTS_JS)
                ok = needle not in texts
            elif op == "delete-next":
                cur = await page.evaluate(_BLOCK_STATE_BY_ID_JS, block_id)
                ok = bool(cur) and cur.get("next_id") != snapshot.get("next_id")
            elif op in ("insert-after", "insert-before") and images:
                count_now = await page.evaluate(_COUNT_ALL_IMAGES_JS)
                ok = count_now >= baseline_img_count + len(images)
            elif op in ("insert-after", "insert-before") and files:
                ok = len(inserted_files) == len(files)
            else:  # insert-after / insert-before 文本 / replace
                texts = await page.evaluate(_COLLECT_ALL_BLOCK_TEXTS_JS)
                ok = _text_matched(new_needle, texts)
            if not ok:
                for _ in range(4):
                    await page.keyboard.press("Control+z")
                    await page.wait_for_timeout(300)
                after = await page.evaluate(_BLOCK_STATE_BY_ID_JS, block_id)
                restored = (
                    op in ("delete", "delete-next")
                    or (after is not None
                        and after["text"] == snapshot["text"]
                        and after["next_id"] == snapshot["next_id"])
                )
                raise FeishuError(
                    f"编辑后核验未通过（{op}），已撤销本次操作",
                    hint="文档已尽力恢复原样" + ("" if restored else "；但回退比对不完全一致，请人工检查"),
                )

            # ---- 重载复核 ----
            await page.wait_for_timeout(1_500)
            await page.reload(wait_until="load", timeout=30_000)
            await auth.raise_if_login_redirect(page)
            await page.wait_for_timeout(2_000)
            reload_verified = False
            if op in ("insert-after", "insert-before") and images:
                after_count = await page.evaluate(_COUNT_ALL_IMAGES_JS)
                reload_verified = after_count >= baseline_img_count + len(images)
            elif op in ("insert-after", "insert-before") and files:
                all_found = True
                for f in files:
                    cnt = await page.evaluate(_FILE_NAME_BLOCKS_COUNT_JS, f["name"])
                    if cnt <= baseline_file_counts.get(f["name"], 0):
                        all_found = False
                        break
                reload_verified = all_found
            else:
                texts = await page.evaluate(_COLLECT_ALL_BLOCK_TEXTS_JS)
                if op == "delete":
                    reload_verified = needle not in texts
                elif op == "delete-next":
                    reload_verified = True  # 下一块无文本可比对，以操作后核验为准
                else:
                    reload_verified = _text_matched(new_needle, texts)
            return {
                "status": "edited" if reload_verified else "unverified",
                "url": url,
                "op": op,
                "anchor": anchor,
                "detail": detail,
                "reload_verified": reload_verified,
            }
        finally:
            await ctx.close()


_BLOCK_MAP_JS = """async () => {
  const norm = s => (s || '').replace(/[\s\u200b\u200e\u200f\ufeff]+/g, ' ').trim();
  const ed = document.querySelector('[data-block-id]');
  let scroller = ed;
  while (scroller && scroller.scrollHeight <= scroller.clientHeight + 50)
    scroller = scroller.parentElement;
  if (!scroller) scroller = document.scrollingElement || document.documentElement;
  const seen = new Map();
  const collect = () => {
    for (const b of document.querySelectorAll('[data-block-id]')) {
      if (b.querySelector('[data-block-id]')) continue;
      const id = b.getAttribute('data-block-id');
      if (seen.has(id)) continue;
      const r = b.getBoundingClientRect();
      const absTop = r.top + (scroller.scrollTop || window.scrollY || 0);
      const cls = (b.className || '') + ' ' + (b.getAttribute('data-block-type') || '');
      const type = b.querySelector('img') ? 'image'
                 : /heading/i.test(cls) ? 'heading'
                 : /code/i.test(cls) ? 'code'
                 : /table/i.test(cls) ? 'table' : 'text';
      seen.set(id, {id, y: Math.round(absTop), type, text: norm(b.innerText).slice(0, 70)});
    }
  };
  scroller.scrollTop = 0;
  await new Promise(r => setTimeout(r, 300));
  const step = Math.max(scroller.clientHeight, 400);
  for (let y = 0; y <= scroller.scrollHeight + step; y += step) {
    scroller.scrollTop = y;
    await new Promise(r => setTimeout(r, 200));
    collect();
  }
  scroller.scrollTop = scroller.scrollHeight;
  await new Promise(r => setTimeout(r, 300));
  collect();
  return [...seen.values()].sort((a, b) => a.y - b.y)
    .map((v, i) => ({i, id: v.id, type: v.type, text: v.text}));
}"""


async def block_map(ref: str) -> dict:
    """只读：滚动全文输出叶块顺序图（index/id/type/text），供选取编辑锚点。"""
    from .browser_worker import run

    return await run(
        "doc-block-map",
        {"ref": ref},
        timeout=max(config.BROWSER_TIMEOUT_SECONDS, 120),
    )


async def _block_map_impl(ref: str) -> dict:
    """Browser Worker implementation: 输出文档叶块顺序图。"""
    if os.environ.get("FEISHU_BROWSER_WORKER") != "1":
        raise ResourceIsolationUnavailable("文档块扫描只能由受限 Browser Worker 执行")
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
            await page.goto(url, wait_until="load", timeout=30_000)
            await auth.raise_if_login_redirect(page)
            await page.wait_for_timeout(2_500)
            title = await page.title()
            blocks = await page.evaluate(_BLOCK_MAP_JS)
            return {"url": url, "title": title, "blocks": blocks, "count": len(blocks)}
        finally:
            await ctx.close()

