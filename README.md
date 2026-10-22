# RedisMap 🛡️

**RedisMap** is a lightweight, read-only Redis inventory and audit utility for systems you own or are explicitly authorised to assess. It uses Python's standard library, understands RESP2 responses, and avoids the blocking `KEYS *` command.

## Highlights

- Check connectivity with `PING`.
- Enumerate keys with incremental `SCAN`.
- Inspect DB0 or a configurable range of logical databases.
- Preview strings, hashes, lists, sets, sorted sets, and streams without writing to Redis.
- Bound scans, collection previews, and displayed string lengths.
- Use Redis ACL credentials through a password prompt, avoiding passwords in shell history.
- Connect over TLS, optionally using a custom CA certificate.
- Run concurrent database inventory jobs with a configurable upper limit.
- Save structured JSON reports atomically with a UTC scan timestamp.
- Use the interactive diagnostic console, which only allows a small set of read-only commands.

> **Scope:** RedisMap does not attempt authentication bypass, exploit Redis misconfiguration, or issue write commands. A read-only client can still expose sensitive data, so use it only with explicit authorisation and protect generated reports.

## Requirements

- Python 3.10 or newer
- Network access to the Redis service
- Appropriate Redis ACL permissions

No third-party Python packages are required.

## Setup

```bash
git clone https://github.com/giriaryan694-a11y/redismap.git
cd redismap
python3 redismap.py --help
```

## Examples

Connectivity check only:

```bash
python3 redismap.py -t 127.0.0.1
```

List DB0 keys:

```bash
python3 redismap.py -t 127.0.0.1 --keys
```

Preview values from DB0:

```bash
python3 redismap.py -t 127.0.0.1 --values
```

Inventory DB0 through DB15, without reading values:

```bash
python3 redismap.py -t 127.0.0.1 --dump-all --threads 4
```

Inventory multiple databases with bounded value previews:

```bash
python3 redismap.py -t 127.0.0.1 --dump-all --values --max-keys 2000 --max-items 25
```

Use an ACL user and securely prompt for the password:

```bash
python3 redismap.py -t redis.example.internal --username auditor --password-prompt
```

Use TLS (with system trust roots):

```bash
python3 redismap.py -t redis.example.internal --tls
```

Use TLS with a custom CA certificate:

```bash
python3 redismap.py -t redis.example.internal --tls --ca-cert ./redis-ca.pem
```

Use a custom port, limit scan rate, and choose a report path:

```bash
python3 redismap.py -t 127.0.0.1 -p 6380 --dump-all -T 0.1 --output ./audit-report.json
```

Open the restricted diagnostic console:

```bash
python3 redismap.py -t 127.0.0.1 --shell
```

## Options

| Option | Purpose |
|---|---|
| `-t`, `--target` | Redis hostname or IP (required) |
| `-p`, `--port` | Port, default `6379` |
| `--keys` | List keys in DB0 |
| `--values` | Include bounded value previews from DB0, or with `--dump-all` |
| `--dbs` | Inventory keys in each database in the configured range |
| `--dump-all` | Inventory databases in the configured range |
| `--db-count N` | Number of database indexes to inspect, default `16` |
| `--threads N` | Concurrent database workers, maximum `32` |
| `--max-keys N` | Maximum keys scanned per database, default `10000` |
| `--max-items N` | Maximum collection items in each value preview, default `50` |
| `--max-value-chars N` | Maximum characters per displayed string value, default `1000` |
| `-T`, `--delay` | Delay between scanned keys in seconds |
| `--username` | Redis ACL username, used with `--password-prompt` |
| `--password-prompt` | Securely prompt for a Redis password |
| `--tls` | Enable TLS |
| `--ca-cert PATH` | Custom CA PEM file for TLS validation |
| `--timeout SECONDS` | Per-socket timeout, default `3` |
| `-o`, `--output PATH` | Report output path; default is a timestamped file under `reports/` |
| `--no-color` | Disable terminal colors |

`--values` can be combined with `--dump-all`; value previews are otherwise for DB0. Large values and collections are truncated by the configured limits. SCAN is incremental but Redis may return duplicate keys during a scan if the dataset changes, as documented by Redis.

## Reports

Each completed inventory writes JSON containing the target, port, UTC timestamp, selected limits, results, errors, and duration. Treat reports as sensitive data, especially when `--values` is used. Reports are local files and are not uploaded anywhere.

## Testing

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile redismap.py
```

## License

See [LICENSE](LICENSE).
