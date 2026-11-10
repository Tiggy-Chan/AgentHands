# YouTube InnerTube & Studio Protocol Reference

## Overview

Official Google Cloud YouTube Data API v3 charges severe daily quota costs (10,000 units/day, uploading or updating metadata costs 1,600+ units).
This framework uses YouTube's internal **InnerTube** API and **YouTube Studio** endpoints directly via authenticated browser session cookies.

---

## Authentication Mechanism

### 1. SAPISIDHASH Calculation
Authenticated requests require the `Authorization` header formatted as:
```text
SAPISIDHASH {unix_timestamp}_{sha1(timestamp + " " + SAPISID + " " + origin)}
```
Where:
- `SAPISID` is extracted from the user's Google cookie (`SAPISID` or `__Secure-3PAPISID`).
- `origin` is `https://www.youtube.com` (for viewer APIs) or `https://studio.youtube.com` (for creator APIs).

### 2. InnerTube Client Payload Context
All InnerTube calls pass client context in the JSON body:
```json
{
  "context": {
    "client": {
      "clientName": "WEB",
      "clientVersion": "2.20240101.00.00",
      "hl": "en",
      "gl": "US"
    }
  }
}
```

---

## Key Endpoints Reference

| Operation | Base | Endpoint | HTTP Method |
|---|---|---|---|
| Video Details | InnerTube | `/player` | POST |
| Update Metadata | Studio | `/video_manager/metadata_update` | POST |
| Like / Dislike | InnerTube | `/like/like`, `/like/removelike` | POST |
| Post Comment | InnerTube | `/comment/create_comment` | POST |
| Reply Comment | InnerTube | `/comment/create_comment_reply` | POST |
| Community Post | Studio | `/backstage/create_post` | POST |
| Channel Info | InnerTube | `/browse` | POST |

---

## Safety Architecture

- All mutating actions (`update-video`, `interact`, `comment`, `reply`, `community`) require `YOUTUBE_WRITE_ALLOWED` environment authorization and SHA-256 fingerprint matching.
- Google session cookies and SAPISID values are registered in the global secret register and never leaked in logs.
