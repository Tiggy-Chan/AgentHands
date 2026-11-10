#!/usr/bin/env python3
"""xianyu — Personal account action CLI for Xianyu (Goofish) without developer APIs.

Data output on stdout (Markdown or JSON). Progress and diagnostics on stderr.
Exit codes:
  0: Success
  1: Error
  2: Authentication missing or expired
  3: Write action blocked by safety gate
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

# Add script directory to sys.path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _xianyu import (
    auth,
    config,
    mtop,
    order_ops,
    output,
    product_ops,
    search_ops,
    security,
    user_ops,
)

_GLOBAL_VALUE_FLAGS = ("--format",)
_GLOBAL_BOOL_FLAGS = ("--json",)


def _split_global_flags(argv: list[str]) -> tuple[dict[str, object], list[str]]:
    found: dict[str, object] = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        matched = False
        for flag in _GLOBAL_VALUE_FLAGS:
            if token == flag and i + 1 < len(argv):
                found[flag.lstrip("-").replace("-", "_")] = argv[i + 1]
                i += 2
                matched = True
                break
            if token.startswith(flag + "="):
                found[flag.lstrip("-").replace("-", "_")] = token.split("=", 1)[1]
                i += 1
                matched = True
                break
        if matched:
            continue
        if token in _GLOBAL_BOOL_FLAGS:
            found[token.lstrip("-").replace("-", "_")] = True
            i += 1
            continue
        rest.append(token)
        i += 1
    return found, rest


def _add_write_gate_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--direct", action="store_true", help="Bypass draft verification and execute directly")
    p.add_argument("--execute", action="store_true", help="Execute write action with confirmed fingerprint")
    p.add_argument("--fingerprint", default=None, help="Confirmed SHA256 draft fingerprint")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xianyu",
        description="Xianyu personal account action framework for AI agents.",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json", "jsonl"],
        default="markdown",
        help="Output format (default: markdown)",
    )
    parser.add_argument("--json", action="store_true", help="Shortcut for --format json")

    sub = parser.add_subparsers(dest="subcommand", required=True)

    # login
    p_login = sub.add_parser("login", help="Set credentials or validate current session")
    p_login.add_argument("--manual", default=None, help="Raw cookie string")
    p_login.add_argument("--user-id", default="", help="Account user ID (unb)")
    p_login.add_argument("--check", action="store_true", help="Validate existing cookies")

    # whoami
    sub.add_parser("whoami", help="Display current account status")

    # products
    p_prod = sub.add_parser("products", help="Product management operations")
    sub_prod = p_prod.add_subparsers(dest="product_action", required=True)

    p_prod_list = sub_prod.add_parser("list", help="List my products")
    p_prod_list.add_argument("--status", choices=["ALL", "ONLINE", "OFFLINE"], default="ALL")
    p_prod_list.add_argument("--page", type=int, default=1)
    p_prod_list.add_argument("--limit", type=int, default=20)

    p_prod_get = sub_prod.add_parser("get", help="Get product details")
    p_prod_get.add_argument("item_id", help="Product item ID")

    p_prod_pub = sub_prod.add_parser("publish", help="Publish new product")
    p_prod_pub.add_argument("--title", required=True, help="Product title")
    p_prod_pub.add_argument("--desc", required=True, help="Description")
    p_prod_pub.add_argument("--price", type=float, required=True, help="Selling price")
    p_prod_pub.add_argument("--orig-price", type=float, default=None, help="Original price")
    p_prod_pub.add_argument("--images", nargs="*", default=[], help="Image URLs")
    p_prod_pub.add_argument("--category", default="", help="Category ID")
    _add_write_gate_args(p_prod_pub)

    p_prod_upd = sub_prod.add_parser("update", help="Update product details")
    p_prod_upd.add_argument("item_id", help="Product item ID")
    p_prod_upd.add_argument("--price", type=float, default=None)
    p_prod_upd.add_argument("--title", default=None)
    p_prod_upd.add_argument("--desc", default=None)
    _add_write_gate_args(p_prod_upd)

    p_prod_shelf = sub_prod.add_parser("shelf", help="Toggle item shelf status")
    p_prod_shelf.add_argument("item_id", help="Product item ID")
    p_prod_shelf_group = p_prod_shelf.add_mutually_exclusive_group(required=True)
    p_prod_shelf_group.add_argument("--on", action="store_true", help="Put on shelf (online)")
    p_prod_shelf_group.add_argument("--off", action="store_true", help="Take off shelf (offline)")
    _add_write_gate_args(p_prod_shelf)

    p_prod_del = sub_prod.add_parser("delete", help="Delete product listing")
    p_prod_del.add_argument("item_id", help="Product item ID")
    _add_write_gate_args(p_prod_del)

    # orders
    p_ord = sub.add_parser("orders", help="Order management operations")
    sub_ord = p_ord.add_subparsers(dest="order_action", required=True)

    p_ord_list = sub_ord.add_parser("list", help="List orders")
    p_ord_list.add_argument("--role", choices=["seller", "buyer"], default="seller")
    p_ord_list.add_argument("--status", default=None)
    p_ord_list.add_argument("--page", type=int, default=1)
    p_ord_list.add_argument("--limit", type=int, default=20)

    p_ord_get = sub_ord.add_parser("get", help="Get order details")
    p_ord_get.add_argument("order_id", help="Order ID")

    p_ord_ship = sub_ord.add_parser("ship", help="Ship an order")
    p_ord_ship.add_argument("order_id", help="Order ID")
    p_ord_ship.add_argument("--waybill", required=True, help="Tracking / Waybill number")
    p_ord_ship.add_argument("--company", required=True, help="Express company code (e.g. SF, YTO)")
    _add_write_gate_args(p_ord_ship)

    p_ord_cancel = sub_ord.add_parser("cancel", help="Cancel an order")
    p_ord_cancel.add_argument("order_id", help="Order ID")
    p_ord_cancel.add_argument("--reason", default="协商一致退款", help="Cancellation reason")
    _add_write_gate_args(p_ord_cancel)

    p_ord_eval = sub_ord.add_parser("evaluate", help="Rate and evaluate an order")
    p_ord_eval.add_argument("order_id", help="Order ID")
    p_ord_eval.add_argument("--content", default="非常满意，交易顺利！", help="Review content")
    p_ord_eval.add_argument("--score", type=int, default=5, help="Rating score (1-5)")
    _add_write_gate_args(p_ord_eval)

    # search
    p_search = sub.add_parser("search", help="Search marketplace listings")
    p_search.add_argument("keyword", help="Search keywords")
    p_search.add_argument("--page", type=int, default=1)
    p_search.add_argument("--min-price", type=float, default=None)
    p_search.add_argument("--max-price", type=float, default=None)
    p_search.add_argument("--sort", default=None, help="Sorting criteria")

    # suggest
    p_sug = sub.add_parser("suggest", help="Search keywords auto-completion")
    p_sug.add_argument("keyword", help="Partial query")

    # categories
    sub.add_parser("categories", help="List item categories")

    # address
    p_addr = sub.add_parser("address", help="Delivery address management")
    sub_addr = p_addr.add_subparsers(dest="address_action", required=True)

    sub_addr.add_parser("list", help="List delivery addresses")

    p_addr_add = sub_addr.add_parser("add", help="Add new delivery address")
    p_addr_add.add_argument("--name", required=True, help="Recipient name")
    p_addr_add.add_argument("--phone", required=True, help="Phone number")
    p_addr_add.add_argument("--prov", required=True, help="Province")
    p_addr_add.add_argument("--city", required=True, help="City")
    p_addr_add.add_argument("--area", required=True, help="District / Area")
    p_addr_add.add_argument("--detail", required=True, help="Street and house address")
    p_addr_add.add_argument("--default", action="store_true", help="Set as default address")
    _add_write_gate_args(p_addr_add)

    p_addr_del = sub_addr.add_parser("delete", help="Delete address")
    p_addr_del.add_argument("address_id", help="Address ID")
    _add_write_gate_args(p_addr_del)

    return parser


async def main_async(argv: list[str] | None = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    globals_, rest = _split_global_flags(raw_argv)
    parser = build_parser()
    args = parser.parse_args(rest)
    for k, v in globals_.items():
        setattr(args, k, v)
    fmt = "json" if getattr(args, "json", False) else getattr(args, "format", "markdown")

    try:
        if args.subcommand == "login":
            return await handle_login(args, fmt)
        elif args.subcommand == "whoami":
            res = await user_ops.get_self_info()
            _output_result(res, fmt, "whoami")
            return 0
        elif args.subcommand == "products":
            return await handle_products(args, fmt)
        elif args.subcommand == "orders":
            return await handle_orders(args, fmt)
        elif args.subcommand == "search":
            res = await search_ops.search_items(
                args.keyword,
                page=args.page,
                min_price=args.min_price,
                max_price=args.max_price,
                sort_field=args.sort,
            )
            _output_result(res, fmt, f"search:{args.keyword}")
            return 0
        elif args.subcommand == "suggest":
            res = await search_ops.suggest(args.keyword)
            _output_result(res, fmt, f"suggest:{args.keyword}")
            return 0
        elif args.subcommand == "categories":
            res = await search_ops.get_categories()
            _output_result(res, fmt, "categories")
            return 0
        elif args.subcommand == "address":
            return await handle_address(args, fmt)
        return 0

    except security.WriteBlockedError as exc:
        output.note(f"Blocked by safety gate: {exc}")
        return 3
    except mtop.MtopError as exc:
        output.note(f"MTOP Error: {exc.ret}")
        return 2 if any("SESSION" in r or "TOKEN" in r for r in exc.ret) else 1
    except Exception as exc:
        output.note(f"Error: {output.scrubbed_exc(exc)}")
        return 1


async def handle_login(args: argparse.Namespace, fmt: str) -> int:
    if args.manual:
        auth.save_credentials(args.manual, args.user_id)
        output.note("Saved credentials successfully.")
        res = await user_ops.get_self_info()
        _output_result(res, fmt, "login")
        return 0

    if args.check:
        try:
            res = await user_ops.get_self_info()
            output.note(f"Session valid for user: {res['user_id']}")
            _output_result(res, fmt, "auth_check")
            return 0
        except Exception as e:
            output.note(f"Session check failed: {output.scrubbed_exc(e)}")
            return 2

    output.note("Please provide cookies via --manual '<cookies>' or set XIANYU_COOKIES in environment.")
    return 1


async def handle_products(args: argparse.Namespace, fmt: str) -> int:
    action = args.product_action
    if action == "list":
        res = await product_ops.list_products(args.status, args.page, args.limit)
        _output_result(res, fmt, "product_list")
        return 0
    elif action == "get":
        res = await product_ops.get_product(args.item_id)
        _output_result(res, fmt, f"product:{args.item_id}")
        return 0
    elif action == "publish":
        res = await product_ops.publish_product(
            title=args.title,
            desc=args.desc,
            price=args.price,
            original_price=args.orig_price,
            images=args.images,
            category_id=args.category,
            execute=args.execute,
            direct=args.direct,
            expected_fp=args.fingerprint,
        )
        _output_result(res, fmt, "product_publish")
        return 0
    elif action == "update":
        res = await product_ops.update_product(
            item_id=args.item_id,
            price=args.price,
            title=args.title,
            desc=args.desc,
            execute=args.execute,
            direct=args.direct,
            expected_fp=args.fingerprint,
        )
        _output_result(res, fmt, "product_update")
        return 0
    elif action == "shelf":
        online = True if args.on else False
        res = await product_ops.set_shelf(
            item_id=args.item_id,
            online=online,
            execute=args.execute,
            direct=args.direct,
            expected_fp=args.fingerprint,
        )
        _output_result(res, fmt, "product_shelf")
        return 0
    elif action == "delete":
        res = await product_ops.delete_product(
            item_id=args.item_id,
            execute=args.execute,
            direct=args.direct,
            expected_fp=args.fingerprint,
        )
        _output_result(res, fmt, "product_delete")
        return 0
    return 0


async def handle_orders(args: argparse.Namespace, fmt: str) -> int:
    action = args.order_action
    if action == "list":
        res = await order_ops.list_orders(args.role, args.status, args.page, args.limit)
        _output_result(res, fmt, "order_list")
        return 0
    elif action == "get":
        res = await order_ops.get_order(args.order_id)
        _output_result(res, fmt, f"order:{args.order_id}")
        return 0
    elif action == "ship":
        res = await order_ops.ship_order(
            order_id=args.order_id,
            waybill_no=args.waybill,
            express_company=args.company,
            execute=args.execute,
            direct=args.direct,
            expected_fp=args.fingerprint,
        )
        _output_result(res, fmt, "order_ship")
        return 0
    elif action == "cancel":
        res = await order_ops.cancel_order(
            order_id=args.order_id,
            reason=args.reason,
            execute=args.execute,
            direct=args.direct,
            expected_fp=args.fingerprint,
        )
        _output_result(res, fmt, "order_cancel")
        return 0
    elif action == "evaluate":
        res = await order_ops.evaluate_order(
            order_id=args.order_id,
            content=args.content,
            score=args.score,
            execute=args.execute,
            direct=args.direct,
            expected_fp=args.fingerprint,
        )
        _output_result(res, fmt, "order_evaluate")
        return 0
    return 0


async def handle_address(args: argparse.Namespace, fmt: str) -> int:
    action = args.address_action
    if action == "list":
        res = await user_ops.list_addresses()
        _output_result(res, fmt, "address_list")
        return 0
    elif action == "add":
        res = await user_ops.add_address(
            name=args.name,
            phone=args.phone,
            province=args.prov,
            city=args.city,
            area=args.area,
            detail=args.detail,
            is_default=args.default,
            execute=args.execute,
            direct=args.direct,
            expected_fp=args.fingerprint,
        )
        _output_result(res, fmt, "address_add")
        return 0
    elif action == "delete":
        res = await user_ops.delete_address(
            address_id=args.address_id,
            execute=args.execute,
            direct=args.direct,
            expected_fp=args.fingerprint,
        )
        _output_result(res, fmt, "address_delete")
        return 0
    return 0


def _output_result(data: Any, fmt: str, tag: str) -> None:
    if fmt == "json":
        output.emit_json(data)
    elif fmt == "jsonl" and isinstance(data, list):
        output.emit_jsonl(data)
    else:
        if isinstance(data, dict):
            lines = [f"# {tag}"]
            for k, v in data.items():
                if isinstance(v, (dict, list)):
                    lines.append(f"## {k}")
                    lines.append(f"```json\n{output.scrub(str(v))}\n```")
                else:
                    lines.append(f"- **{k}**: {output.safe_label(str(v))}")
            output.emit("\n".join(lines))
        elif isinstance(data, list):
            lines = [f"# {tag} (Count: {len(data)})"]
            for idx, item in enumerate(data, start=1):
                if isinstance(item, dict):
                    preview = " | ".join(
                        f"{k}: {output.safe_label(str(v), 24)}"
                        for k, v in item.items() if k not in ("desc", "detail")
                    )
                    lines.append(f"{idx}. {preview}")
                    if "desc" in item and item["desc"]:
                        lines.append(output.untrusted(str(item["desc"]), f"item:{item.get('item_id', idx)}"))
                else:
                    lines.append(f"{idx}. {output.safe_label(str(item))}")
            output.emit("\n".join(lines))
        else:
            output.emit(output.untrusted(str(data), tag))


def main() -> None:
    try:
        rc = asyncio.run(main_async())
        sys.exit(rc)
    except KeyboardInterrupt:
        output.note("Interrupted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
