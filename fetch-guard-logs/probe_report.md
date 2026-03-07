# MCP Probe Report

## Environment

- Proxy config: `examples/basic_proxy.yaml`
- Upstreams probed: single upstream (mcp-fetch server, no namespace)
- Date: 2026-03-07

## Tool Inventory

- **`fetch`**: Fetches a URL from the internet and optionally extracts contents as markdown.
  - `url` (string, required) — Must be a valid URI (Pydantic `format: uri`)
  - `max_length` (integer, default 5000, range 1–999999) — Truncates response to N chars
  - `start_index` (integer, default 0) — Pagination offset into response
  - `raw` (boolean, default false) — Return raw HTML instead of markdown conversion
  - Observed behavior: converts HTML to markdown by default; falls back to raw for JSON, images, binary. Prepends `Contents of <url>:\n`. Appends truncation hint if cut off.

## Resource Inventory

None.

## Prompt Inventory

- **`fetch`**: Fetch a URL and extract its contents as markdown (no parameters documented).

## Security Findings

### Input Validation

- [x] **URL format**: Validated by Pydantic (`format: uri`) — rejects non-URL strings like `not-a-valid-url`
- [ ] **URL scheme restriction**: FAIL — `file://`, `ftp://`, `data:` all accepted; only fail at transport layer
- [ ] **Loopback/private IP blocking**: FAIL — `127.0.0.1`, `localhost`, `0.0.0.0`, `10.0.0.1` all forwarded
- [ ] **Cloud metadata blocking**: FAIL — `169.254.169.254` forwarded (failed only because not reachable from this host)
- [x] **Argument type validation**: PASS — Pydantic enforces types on all parameters

### Network Boundary (Category A — SSRF)

- [ ] **Can reach loopback**: Attempted `http://127.0.0.1`, `http://localhost`, `http://0.0.0.0` — connection refused (nothing listening), but **not blocked by proxy**
- [ ] **Can reach cloud metadata**: Attempted `http://169.254.169.254/latest/meta-data/` — connection failed (not on AWS), but **not blocked by proxy**
- [ ] **Can reach private networks**: Attempted `http://10.0.0.1` — connection refused, but **not blocked by proxy**
- **Risk**: In any deployment with internal services (Docker networks, VPCs, cloud instances), these would succeed.

### Scheme Abuse (Category B)

- [ ] **file:// blocked**: FAIL — `file:///etc/passwd`, `file:///etc/hostname` pass validation; fail only because httpx doesn't implement `file://` transport
- [ ] **ftp:// blocked**: FAIL — `ftp://example.com` accepted; fails at transport
- [ ] **data: blocked**: FAIL — `data:text/html,<h1>test</h1>` accepted; fails at transport
- **Risk**: If the underlying HTTP library ever gains support for these schemes (or is swapped), local file reads become possible with no proxy-level defense.

### Response Handling (Category C)

- [ ] **Response size limits**: FAIL — 100KB binary (`/bytes/100000`) returned in full (truncated only by `max_length` default of 5000 chars in the response wrapper, but full data was fetched upstream)
- [ ] **Timeout enforcement**: FAIL — 10-second delay (`/delay/10`) completed with no timeout; proxy did not interrupt
- [x] **Redirect following**: 5-hop redirect chain followed silently and resolved — no redirect limit observed
- [ ] **Binary content handling**: Binary (`image/png`, `application/octet-stream`) returned raw without rejection

### Content Risks (Category D)

- [ ] **Prompt injection via response**: FAIL — arbitrary web content returned verbatim with no filtering; a page containing "ignore previous instructions" would be passed directly to the calling LLM
- [ ] **Credential/PII exposure**: Not tested directly, but any page containing secrets (e.g. fetching an internal config URL) would be returned in full

## Recommendations

### Critical

1. **SSRF / private network blocking** — Requires a custom plugin (Level 3)
   - Resolve the URL's hostname to IP before fetching
   - Block requests to loopback (127.0.0.0/8, ::1), link-local (169.254.0.0/16, fe80::/10), and RFC-1918 private ranges (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
   - Also block `0.0.0.0` and `metadata.google.internal`

2. **URL scheme allowlist** — Can be done with a custom plugin (Level 3) or a rewrite+filter workaround
   - Only permit `http://` and `https://` schemes
   - Reject `file://`, `ftp://`, `data:`, and any other scheme at the proxy layer before the request reaches upstream

### High

1. **Response size limit** — Custom plugin (Level 3) or upstream config
   - Cap the actual upstream fetch size (not just the `max_length` response truncation)
   - Consider enforcing `max_length` ≤ some proxy-set ceiling (e.g. 50000)

2. **Request timeout** — Custom plugin (Level 3)
   - Enforce a maximum request duration (e.g. 5 seconds) regardless of `delay` parameter

### Medium

1. **Content-type allowlist** — Custom plugin (Level 3)
   - Only return `text/html`, `text/plain`, `application/json` content types
   - Reject binary formats (`image/*`, `application/octet-stream`) before passing to caller

2. **Redirect limit** — Custom plugin (Level 3)
   - Cap redirect follows at 3 and reject chains that cross scheme or domain unexpectedly

### Low / Nice-to-have

1. **Domain allowlist** — YAML `filter` or custom plugin (Level 3)
   - If the use case is narrow (e.g. only fetching from known domains), add an explicit allowlist
   - Can be implemented as a custom plugin that checks the parsed hostname against a config list

2. **Response content scanning** — Custom plugin (Level 3)
   - Detect and strip or flag responses containing prompt-injection patterns
   - Detect potential credential leakage (API key patterns, PEM headers, etc.)
