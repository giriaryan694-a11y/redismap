# Changelog

## 2.0.0

- Replaced newline-splitting response handling with a RESP2 parser that handles nested arrays, binary-safe bulk strings, nulls, and Redis errors.
- Fixed database selection by keeping authentication and `SELECT` on the same database-specific connection.
- Replaced blocking `KEYS *` enumeration with `SCAN` and de-duplicated scan results.
- Added bounded previews for strings and common Redis collection types.
- Added Redis ACL password prompting, TLS/CA support, socket timeouts, and input validation.
- Restricted the interactive console to read-only diagnostics.
- Added timestamped JSON reports written atomically, plus unit and fake-server integration tests.
- Removed the `pyfiglet` runtime dependency; the project now uses only the Python standard library.
