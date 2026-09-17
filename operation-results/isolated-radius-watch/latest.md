# Isolated radius watch operation

> Public redacted summary. Address, coordinates, topic and credentials are intentionally excluded.

| Field | Value |
|---|---|
| Run | `20260917T220442Z-3823765` |
| Status | **FAIL** |
| Exit code | `1` |
| Failed line | `475` |
| Phase | `configure-watch` |
| Radius | `5280 ft` |
| Sources | `ANY` |
| Categories | `ANY` |
| Minimum priority | `1` |
| Transport test | `pass-one-isolated-test-message` |
| Builds | `none` |
| Restarts | `none` |
| Schema changes | `none` |
| Full E2E | `not run` |
| Started UTC | `2026-09-17T22:04:42Z` |
| Finished UTC | `2026-09-17T22:04:58Z` |
| Repository HEAD | `e3ff9897ca3ec9375c4a2305e06fea1a674249dd` |

## Sanitized result

```json
[2026-09-17T22:04:57Z] ERROR: operation failed rc=1 line=475 phase=configure-watch
```

## Failure locator

The full mode-600 log remains on the VPS at `/var/log/city-manager-os/operations/isolated-radius-watch-20260917T220442Z-3823765.log`.
