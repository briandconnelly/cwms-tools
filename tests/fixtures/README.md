# Fixture matrix

Recorded CDA responses keyed to the test scenarios in the implementation
plan. Each fixture file holds the request URL(s) and JSON response body
needed to exercise one branch of behavior. Placeholders land at M2; real
recordings land alongside the tool implementations in M3–M6.

| Fixture                            | Exercises |
|------------------------------------|---|
| `swt_foss_happy.json`              | Standard Tulsa flood-control reservoir — `cwms_get_value`, `cwms_get_history`, `cwms_describe_place` happy paths. |
| `nwdm_ftpk_project_format_error.json` | The `get_project` format-error fallback in `core/projects.py`. |
| `nwdm_ftpk_levels_seasonal.json`   | Seasonal-level workaround branch — direct-CDA endpoint, not via wrapper. |
| `nwo_becr_ghost.json`              | Ghost location; verifies `error.code = ghost_location` + repair hint. |
| `swt_chou_lock_zero_params.json`   | Co-located but zero-publishing variant — ghost detection at parameter level. |
| `multi_id_value.json`              | List input to `cwms_get_value`; exercises `core/concurrency.py`. |
| `catalog_truncated_at_page_cap.json` | `get_timeseries` silent-truncation detection. |
| `cda_429_rate_limited.json`        | Recorded 429 + `Retry-After`; drives `test_get_locations_catalog_wraps_429_as_rate_limited_with_retry_after`. |
| `cda_503_upstream_error.json`      | `error.code = upstream_error`, `retryable: true`. |
| `auth_unconfigured.json`           | Anonymous-session verification (`whoami`, `env`). |
