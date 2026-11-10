---
name: xianyu
description: Personal account action framework for Xianyu (Goofish / 闲鱼) without developer APIs. Manage product listings (publish, update price/desc, shelf on/off, delete), fulfill orders (list, ship with tracking, cancel, review), search marketplace items with price filters, and manage shipping addresses. Triggers: "闲鱼", "xianyu", "goofish", "闲鱼发货", "改价", "闲鱼上架", "闲鱼搜索", "闲鱼订单".
---

# Xianyu (Goofish) Personal Account Action Framework

Operate your personal Xianyu seller and buyer account directly from AI agents without enterprise developer credentials.
Uses local MTOP MD5 parameter signature calculation and session token maintenance.

## Key Capabilities

- **Product Management**: List seller items, publish new goods, modify prices, toggle on/off shelf, or delete listings.
- **Order Fulfillment**: Track seller/buyer orders, view delivery addresses, submit tracking numbers to ship orders, handle cancellations, and submit ratings.
- **Marketplace Discovery**: Search second-hand goods with pricing and sorting filters, retrieve auto-completion suggestions, and browse category trees.
- **Address Book**: List, add, and remove personal delivery addresses.

---

## Controlled Write Safety: Dual-Gate Mechanism

Mutating actions (`products publish/update/shelf/delete`, `orders ship/cancel/evaluate`, `address add/delete`) are strictly protected:

1. **Draft Fingerprinting**: By default, write commands print a SHA256 content fingerprint draft and exit without modifying the account state.
2. **Explicit Confirmation**: To execute, pass `--execute --fingerprint <FP>`. Any difference in price or parameters invalidates the fingerprint immediately.
3. **Direct Execution**: When running in an authorized workflow, set `XIANYU_WRITE_ALLOWED=1` or pass `--direct`.

---

## Quick Reference

```sh
skills/xianyu/scripts/xianyu.py <subcommand> [options]
```

Common flags: `--format markdown|json|jsonl` (or `--json`).

| Subcommand | Description | Safety Gate |
|---|---|---|
| `login [--manual COOKIES\|--check]` | Save cookies or validate session | Read |
| `whoami` | Inspect current account ID and authentication state | Read |
| `products list [--status ALL\|ONLINE\|OFFLINE]` | List my products | Read |
| `products get <item_id>` | View product details | Read |
| `products publish --title T --desc D --price P` | Publish new commodity | Write (Dual-gate) |
| `products update <id> [--price P] [--title T]` | Change price or listing details | Write (Dual-gate) |
| `products shelf <id> (--on\|--off)` | Toggle online / offline status | Write (Dual-gate) |
| `products delete <id>` | Delete product listing | Write (Dual-gate) |
| `orders list [--role seller\|buyer]` | List orders | Read |
| `orders get <order_id>` | View order details | Read |
| `orders ship <id> --waybill NO --company COMP` | Fulfill order with tracking number | Write (Dual-gate) |
| `orders cancel <id> [--reason R]` | Cancel order | Write (Dual-gate) |
| `orders evaluate <id> [--content C]` | Rate transaction | Write (Dual-gate) |
| `search <query> [--min-price P] [--max-price P]` | Search listings | Read |
| `suggest <query>` | Auto-completion suggestions | Read |
| `categories` | View category tree | Read |
| `address list` | View shipping addresses | Read |
| `address add --name N --phone P ...` | Add shipping address | Write (Dual-gate) |
| `address delete <id>` | Remove shipping address | Write (Dual-gate) |

---

## Usage Examples

### 1. Check Account Status
```bash
skills/xianyu/scripts/xianyu.py whoami --json
```

### 2. Search Listings with Price Filter
```bash
skills/xianyu/scripts/xianyu.py search "Mac mini M4" --min-price 3000 --max-price 4500
```

### 3. Update Listing Price (Draft & Execute)
```bash
# Generate draft fingerprint
skills/xianyu/scripts/xianyu.py products update 88291029381 --price 3499.0

# Execute with confirmed fingerprint
skills/xianyu/scripts/xianyu.py products update 88291029381 --price 3499.0 --execute --fingerprint 3a7f82b1...
```

### 4. Ship an Order
```bash
skills/xianyu/scripts/xianyu.py orders ship 992817263541 --waybill SF123456789 --company SF --direct
```
