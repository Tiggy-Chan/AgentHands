# Xianyu (Goofish) Protocol Reference

## Overview

Xianyu (Goofish) operates primarily on Alibaba's MTOP (Mobile Taobao Open Platform) gateway.
Web and mobile clients authenticate via Taobao SSO cookies and sign requests with an MD5 parameter digest.

---

## MTOP Gateway Architecture

- **Base Gateway**: `https://h5api.m.goofish.com/h5/{api_name}/{version}/`
- **Application Key (Web H5)**: `34839810`
- **HTTP Method**: Primarily `POST` with form data and query parameters.

### Request Query / Body Parameters

| Parameter | Value | Purpose |
|---|---|---|
| `jsv` | `2.7.2` | Client JS runtime version |
| `appKey` | `34839810` | Xianyu H5 Application Key |
| `t` | Epoch timestamp in milliseconds | Prevents replay attacks |
| `sign` | 32-character lowercase hex MD5 | Request signature |
| `v` | Version (e.g. `1.0`, `3.0`) | API schema version |
| `type` | `originaljson` | Prevents JSONP wrapping |
| `accountSite` | `xianyu` | Target business domain |
| `dataType` | `json` | Expected payload format |
| `api` | Full MTOP endpoint name | RPC target identifier |
| `data` | JSON serialized string | Endpoint business arguments |

---

## Signature Generation Algorithm

The signature is computed locally using standard MD5:

```text
sign = MD5(token & timestamp_ms & appKey & data_json_string)
```

Where:
- `&` is a literal ampersand separator.
- `token`: Extracted from the `_m_h5_tk` cookie. Format is `<token>_<expiry_timestamp>`; only the string preceding the first underscore is used.
- `timestamp_ms`: Current Unix timestamp in milliseconds (matching parameter `t`).
- `appKey`: Constant `34839810`.
- `data_json_string`: Exact JSON string passed in the `data` parameter.

---

## Key Endpoints Reference

### 1. Product & Listing
- `mtop.taobao.idlefish.item.list`: Query seller's published listings.
- `mtop.taobao.idle.pc.detail`: Fetch full listing specifications and price.
- `mtop.taobao.idlefish.item.publish`: Publish new commodity.
- `mtop.taobao.idlefish.item.update`: Modify price, title, or description.
- `mtop.taobao.idlefish.item.online` / `offline`: Toggle listing shelf state.
- `mtop.taobao.idlefish.item.delete`: Remove product listing.

### 2. Orders & Fulfillment
- `mtop.taobao.idlefish.order.list`: Retrieve buy/sell transaction lists.
- `mtop.taobao.idlefish.order.detail`: Full order details including shipping address.
- `mtop.taobao.idlefish.shipment.ship`: Dispatch shipment with express company and tracking number.
- `mtop.taobao.idlefish.order.cancel`: Request transaction cancellation.
- `mtop.taobao.idlefish.order.rate`: Submit seller/buyer review and score.

### 3. Search & Categories
- `mtop.taobao.idlemtopsearch.pc.search`: Keyword search with filters (price, sort).
- `mtop.taobao.idlefish.search.suggest`: Live auto-completion suggestions.
- `mtop.taobao.idlefish.category.list`: Category tree.

---

## Error Handling & Status Codes

| Ret Code | Meaning | Mitigation |
|---|---|---|
| `SUCCESS::调用成功` | Request succeeded | Process returned `data` object |
| `FAIL_SYS_TOKEN_EXOIRED` / `FAIL_SYS_TOKEN_EMPTY` | MTOP token expired | Warm up request to receive fresh `_m_h5_tk` |
| `FAIL_SYS_SESSION_EXPIRED` | SSO session expired | Re-authenticate via `xianyu login --manual` |
| `FAIL_BIZ_USER_NOT_LOGIN` | Not logged in | Update cookies in configuration |
| `FAIL_BIZ_ITEM_NOT_FOUND` | Item does not exist | Verify item ID |
