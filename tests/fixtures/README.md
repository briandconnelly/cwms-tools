# Fixture matrix

Recorded CDA responses used by tests. Each file is a real response body
captured from the live API; most scenarios are instead mocked inline with
`responses`. Add a file here only when a test loads it, and list it below.

| Fixture                            | Exercises |
|------------------------------------|---|
| `swt_webb_locks_list.json`         | Raw `GET /projects/locks?office=SWT&project-id=WEBB` body (recorded 2026-10-11). The list endpoint returns every dimension as `0.0` with no units — only identity is usable. |
| `swt_webb_lock_en.json`            | Raw `GET /projects/locks/WEBB-Lock?office=SWT&unit=EN` body (recorded 2026-10-11). Real dimensions plus the `maximum-lock-lift` -9999 m sentinel (-32805.118 ft). |
| `cda_429_rate_limited.json`        | Recorded 429 + `Retry-After`; drives `test_get_locations_catalog_wraps_429_as_rate_limited_with_retry_after`. |
