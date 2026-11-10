# Xiaohongshu (RedNote) Protocol Reference

## Overview

Xiaohongshu does not offer public open platform APIs for personal accounts.
All automation relies on authenticated Web sessions (`creator.xiaohongshu.com` and `www.xiaohongshu.com`).

---

## Authentication & Session Lifecycle

### Key Cookies

| Cookie Name | Purpose |
|---|---|
| `web_session` | Primary login session token for Web and Creator Center |
| `a1` | Device and browser instance identifier |
| `webId` | Frontend browser session tracking |

---

## Creator Center Publishing Workflows

### 1. Image-Text Notes (`target=image`)
- **URL**: `https://creator.xiaohongshu.com/publish/publish?target=image`
- **File Input**: `<input type="file">` supporting 1–9 images (JPG, PNG, WEBP).
- **Title Constraint**: Max 20 UTF-8 characters.
- **Topics**: Entered as `#topic#` tags in the content editor.
- **Card Styles**: `基础` (default), `边框`, `备忘`, `清新`, `涂写`, `便签`, `光影`, `简约`.

### 2. Video Notes (`target=video`)
- **URL**: `https://creator.xiaohongshu.com/publish/publish?target=video`
- **Video Upload**: Native file input supporting MP4/MOV.
- **Publish Trigger (Shadow DOM)**:
  The submit button is encapsulated inside a custom Web Component `<xhs-publish-btn>` with a closed shadow root.
  Standard host `.click()` events do not trigger Vue/React internal form validation.
  Invocation must be dispatched via JavaScript:
  ```js
  document.querySelector('xhs-publish-btn')._onPublish();
  ```

---

## Platform Anti-Scraping & Cooldown Handling

1. **High-Frequency Rate Limiting ("请求太频繁，请一分钟后再试")**:
   Sending high-frequency search requests without delay triggers a 60-second cooldown page.
   Automated batches should maintain 2.0s–3.0s pacing intervals between requests.
2. **Account Security QR Challenge**:
   When triggered, requires scanning via the mobile app to verify identity.
