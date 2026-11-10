# X (Twitter) Internal GraphQL Protocol Reference

## Overview

X (Twitter) official API v2 requires expensive paid developer subscriptions for write access ($100-$200/month).
This framework interfaces directly with X's internal web client GraphQL endpoints using standard browser session cookies (`auth_token`) and CSRF tokens (`ct0`).

---

## Authentication Mechanism

### 1. Credentials
- `auth_token`: HTTP-only cookie representing the authenticated session.
- `ct0`: CSRF token passed both as a cookie and as the `x-csrf-token` HTTP header.
- `Bearer Token`: Constant web client bearer token used across all public web sessions.

### 2. Request Headers
```http
authorization: Bearer AAAAAAAAAAAAAAAAAAAAA...
x-csrf-token: <ct0>
cookie: auth_token=<auth_token>; ct0=<ct0>
x-twitter-active-user: yes
x-twitter-auth-type: OAuth2Session
content-type: application/json
```

---

## Key GraphQL Query IDs & Endpoints

| Operation | Query ID | HTTP Method | Path |
|---|---|---|---|
| `CreateTweet` | `a1p9RWpkYKBkVmGwQRTSjA` | POST | `/i/api/graphql/{query_id}/CreateTweet` |
| `DeleteTweet` | `VaenaVgh5q0AcOCbvVagxQ` | POST | `/i/api/graphql/{query_id}/DeleteTweet` |
| `FavoriteTweet` | `lI07N6Otwv1PhnEgXILM7A` | POST | `/i/api/graphql/{query_id}/FavoriteTweet` |
| `CreateRetweet` | `ojPdsNQgahqBGT паспорта` | POST | `/i/api/graphql/{query_id}/CreateRetweet` |
| `Bookmarks` | `tMD698K218w6A7B_iIq3-g` | GET | `/i/api/graphql/{query_id}/Bookmarks` |
| `UserByScreenName` | `G3KGOASz96M-Qu0nwmGXNg` | GET | `/i/api/graphql/{query_id}/UserByScreenName` |

---

## Safety Architecture

- All mutating operations (`post`, `reply`, `delete`, `interact`) enforce the `X_WRITE_ALLOWED` environment gate and SHA-256 fingerprint verification.
- Sensitive cookies (`auth_token`, `ct0`) are registered in the global secret register and never surfaced in stdout or exception logs.
