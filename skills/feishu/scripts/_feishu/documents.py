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
from .errors import FeishuError
from .output import note

from lark_bridge import drive

_URL_RE = re.compile(
    r"https?://[^/]+/(?P<kind>docx|docs|wiki|sheets|file|folder)/(?P<token>[A-Za-z0-9_-]+)"
)
_EXPORTABLE = {
    "docx": ("docx", ["markdown", "docx", "pdf"]),
    "doc": ("doc", ["docx", "pdf"]),
    "wiki": (None, []),  # resolved first
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


async def export_document(ref: str, fmt: str) -> bytes:
    """→ file bytes. fmt ∈ markdown/docx/pdf/xlsx."""
    cookie, cookies, domain = _creds()
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
