#!/usr/bin/env python3
"""RedisMap: a bounded, read-only Redis inventory and audit utility.

Use only against Redis instances you own or are explicitly authorised to assess.
The interactive console intentionally accepts read-only diagnostic commands only.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterable

VERSION = "2.0.0"
DEFAULT_DB_COUNT = 16
DEFAULT_MAX_KEYS = 10_000
DEFAULT_MAX_ITEMS = 50
DEFAULT_TIMEOUT = 3.0
SCAN_COUNT = 200


class RedisMapError(Exception):
    """Base error for RedisMap failures."""


class RedisProtocolError(RedisMapError):
    """Raised when the server sends an invalid or unsupported RESP2 response."""


class RedisCommandError(RedisMapError):
    """Raised when Redis replies with a command error."""


def read_resp(reader: BinaryIO) -> Any:
    """Read one RESP2 value from a buffered binary stream."""
    line = reader.readline()
    if not line or not line.endswith(b"\r\n"):
        raise RedisProtocolError("Unexpected end of Redis response")

    marker, payload = line[:1], line[1:-2]
    if marker == b"+":
        return payload
    if marker == b"-":
        raise RedisCommandError(payload.decode("utf-8", errors="replace"))
    if marker == b":":
        try:
            return int(payload)
        except ValueError as exc:
            raise RedisProtocolError("Invalid RESP integer") from exc
    if marker == b"$":
        try:
            length = int(payload)
        except ValueError as exc:
            raise RedisProtocolError("Invalid RESP bulk-string length") from exc
        if length == -1:
            return None
        if length < -1:
            raise RedisProtocolError("Invalid negative RESP bulk-string length")
        data = reader.read(length)
        terminator = reader.read(2)
        if len(data) != length or terminator != b"\r\n":
            raise RedisProtocolError("Incomplete RESP bulk string")
        return data
    if marker == b"*":
        try:
            count = int(payload)
        except ValueError as exc:
            raise RedisProtocolError("Invalid RESP array length") from exc
        if count == -1:
            return None
        if count < -1:
            raise RedisProtocolError("Invalid negative RESP array length")
        return [read_resp(reader) for _ in range(count)]

    raise RedisProtocolError(f"Unsupported RESP2 marker: {marker!r}")


def encode_command(parts: Iterable[str | bytes | int]) -> bytes:
    """Encode arguments as a binary-safe RESP command."""
    encoded: list[bytes] = []
    for part in parts:
        if isinstance(part, bytes):
            encoded.append(part)
        else:
            encoded.append(str(part).encode("utf-8"))
    payload = [f"*{len(encoded)}\r\n".encode("ascii")]
    for item in encoded:
        payload.extend((f"${len(item)}\r\n".encode("ascii"), item, b"\r\n"))
    return b"".join(payload)


def as_text(value: Any) -> Any:
    """Convert Redis byte strings recursively to JSON-safe text."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, list):
        return [as_text(item) for item in value]
    if isinstance(value, dict):
        return {str(as_text(key)): as_text(item) for key, item in value.items()}
    return value


def truncate_text(value: Any, limit: int) -> tuple[str, bool]:
    """Return a printable, bounded text preview and whether it was truncated."""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    if len(text) <= limit:
        return text, False
    return text[:limit] + "…[truncated]", True


def valid_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return number


def non_negative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be a number") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("value must be zero or greater")
    return number


class RedisClient:
    """Small RESP2 client with one authenticated, database-specific connection."""

    def __init__(
        self,
        host: str,
        port: int,
        db: int = 0,
        timeout: float = DEFAULT_TIMEOUT,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = False,
        ca_cert: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.db = db
        self.timeout = timeout
        self.sock: socket.socket | ssl.SSLSocket | None = None
        self.reader: BinaryIO | None = None
        try:
            raw_sock = socket.create_connection((host, port), timeout=timeout)
            raw_sock.settimeout(timeout)
            if use_tls:
                context = ssl.create_default_context(cafile=ca_cert)
                self.sock = context.wrap_socket(raw_sock, server_hostname=host)
            else:
                self.sock = raw_sock
            self.reader = self.sock.makefile("rb")
            if password is not None:
                if username:
                    self.execute("AUTH", username, password)
                else:
                    self.execute("AUTH", password)
            if db:
                self.execute("SELECT", db)
        except Exception:
            self.close()
            raise

    def execute(self, *parts: str | bytes | int) -> Any:
        if self.sock is None or self.reader is None:
            raise RedisMapError("Redis connection is closed")
        self.sock.sendall(encode_command(parts))
        return read_resp(self.reader)

    def close(self) -> None:
        if self.reader is not None:
            try:
                self.reader.close()
            except OSError:
                pass
            self.reader = None
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def __enter__(self) -> "RedisClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def make_client(args: argparse.Namespace, db: int = 0) -> RedisClient:
    return RedisClient(
        host=args.target,
        port=args.port,
        db=db,
        timeout=args.timeout,
        username=args.username,
        password=args.password,
        use_tls=args.tls,
        ca_cert=args.ca_cert,
    )


def scan_keys(client: RedisClient, max_keys: int, delay: float = 0) -> tuple[list[bytes], bool]:
    """Enumerate keys incrementally, never using the blocking KEYS command."""
    cursor = 0
    keys: list[bytes] = []
    seen: set[bytes] = set()
    while True:
        response = client.execute("SCAN", cursor, "COUNT", SCAN_COUNT)
        if not isinstance(response, list) or len(response) != 2:
            raise RedisProtocolError("Unexpected SCAN response")
        cursor = int(response[0])
        batch = response[1] or []
        for key in batch:
            if not isinstance(key, bytes):
                key = str(key).encode("utf-8")
            if key in seen:
                continue
            seen.add(key)
            keys.append(key)
            if delay:
                time.sleep(delay)
            if len(keys) >= max_keys:
                return keys, cursor != 0 or len(keys) < int(client.execute("DBSIZE"))
        if cursor == 0:
            return keys, False


def value_preview(client: RedisClient, key: bytes, key_type: str, max_items: int, max_value_chars: int) -> dict[str, Any]:
    """Fetch a bounded preview of a key, avoiding unbounded collection reads."""
    if key_type == "string":
        # GET would transfer the entire value. STRLEN + GETRANGE keeps large
        # strings bounded on the wire as well as in the report.
        total_bytes = int(client.execute("STRLEN", key))
        byte_limit = max_value_chars * 4  # UTF-8 uses at most four bytes per code point.
        raw = client.execute("GETRANGE", key, 0, byte_limit - 1) or b""
        preview, text_truncated = truncate_text(raw, max_value_chars)
        return {
            "value": preview,
            "truncated": total_bytes > len(raw) or text_truncated,
            "total_bytes": total_bytes,
        }

    if key_type == "hash":
        response = client.execute("HSCAN", key, 0, "COUNT", min(max_items, SCAN_COUNT))
        entries = response[1] if isinstance(response, list) and len(response) == 2 else []
        def hash_items(raw_entries: list[Any]) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            for i in range(0, len(raw_entries) - 1, 2):
                field, field_cut = truncate_text(raw_entries[i], max_value_chars)
                value, value_cut = truncate_text(raw_entries[i + 1], max_value_chars)
                items.append({"field": field, "value": value, "field_truncated": field_cut, "value_truncated": value_cut})
            return items

        result = hash_items(entries)[:max_items]
        cursor = int(response[0]) if isinstance(response, list) and len(response) == 2 else 0
        # Continue only while necessary to fill the preview limit.
        while cursor and len(result) < max_items:
            response = client.execute("HSCAN", key, cursor, "COUNT", min(max_items - len(result), SCAN_COUNT))
            cursor = int(response[0])
            entries = response[1] or []
            result.extend(hash_items(entries))
            result = result[:max_items]
        total = int(client.execute("HLEN", key))
        values_truncated = any(item["field_truncated"] or item["value_truncated"] for item in result)
        return {"entries": result, "truncated": total > len(result) or values_truncated, "total": total}

    if key_type == "list":
        total = int(client.execute("LLEN", key))
        values = client.execute("LRANGE", key, 0, max_items - 1) or []
        result: list[str] = []
        was_truncated = False
        for item in values:
            text, trunc = truncate_text(item, max_value_chars)
            result.append(text)
            was_truncated = was_truncated or trunc
        return {"items": result, "truncated": total > len(result) or was_truncated, "total": total}

    if key_type == "set":
        total = int(client.execute("SCARD", key))
        cursor = 0
        values: list[Any] = []
        while len(values) < max_items:
            response = client.execute("SSCAN", key, cursor, "COUNT", min(max_items - len(values), SCAN_COUNT))
            cursor = int(response[0])
            values.extend(response[1] or [])
            values = values[:max_items]
            if cursor == 0:
                break
        previews = [truncate_text(item, max_value_chars) for item in values]
        items = [item[0] for item in previews]
        return {"items": items, "truncated": total > len(items) or any(item[1] for item in previews), "total": total}

    if key_type == "zset":
        total = int(client.execute("ZCARD", key))
        cursor = 0
        values: list[Any] = []
        while len(values) // 2 < max_items:
            response = client.execute("ZSCAN", key, cursor, "COUNT", min(max_items - len(values) // 2, SCAN_COUNT))
            cursor = int(response[0])
            values.extend(response[1] or [])
            values = values[:max_items * 2]
            if cursor == 0:
                break
        pairs = []
        member_was_truncated = False
        for i in range(0, len(values) - 1, 2):
            member, member_cut = truncate_text(values[i], max_value_chars)
            score, score_cut = truncate_text(values[i + 1], max_value_chars)
            member_was_truncated = member_was_truncated or member_cut or score_cut
            pairs.append({"member": member, "score": score, "member_truncated": member_cut, "score_truncated": score_cut})
        return {"members_with_scores": pairs, "truncated": total > len(pairs) or member_was_truncated, "total": total}

    if key_type == "stream":
        total = int(client.execute("XLEN", key))
        events = client.execute("XRANGE", key, "-", "+", "COUNT", max_items) or []
        items = []
        field_was_truncated = False
        for event in events:
            if isinstance(event, list) and len(event) == 2:
                fields = event[1] or []
                field_items = []
                for i in range(0, len(fields) - 1, 2):
                    field, field_cut = truncate_text(fields[i], max_value_chars)
                    value, value_cut = truncate_text(fields[i + 1], max_value_chars)
                    field_was_truncated = field_was_truncated or field_cut or value_cut
                    field_items.append({"field": field, "value": value, "truncated": field_cut or value_cut})
                items.append({"id": as_text(event[0]), "fields": field_items})
        return {"items": items, "truncated": total > len(items) or field_was_truncated, "total": total}

    return {"note": f"Value preview is not implemented for Redis type {key_type!r}.", "truncated": False}


def inspect_database(args: argparse.Namespace, db: int, include_values: bool) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {"database": db, "ok": False, "keys": [], "errors": []}
    try:
        with make_client(args, db) as client:
            size = int(client.execute("DBSIZE"))
            keys, scan_truncated = scan_keys(client, args.max_keys, args.delay)
            result.update({"ok": True, "key_count": size, "keys_scanned": len(keys), "keys_truncated": scan_truncated})
            entries: list[dict[str, Any]] = []
            for key in keys:
                key_text = as_text(key)
                try:
                    key_type = as_text(client.execute("TYPE", key))
                    item: dict[str, Any] = {"key": key_text, "type": key_type}
                    if include_values:
                        item.update(value_preview(client, key, str(key_type), args.max_items, args.max_value_chars))
                    entries.append(item)
                except (RedisMapError, OSError, ssl.SSLError, TimeoutError) as exc:
                    result["errors"].append({"key": key_text, "error": str(exc)})
            result["keys"] = entries
    except (RedisMapError, OSError, ssl.SSLError, TimeoutError) as exc:
        result["errors"].append({"error": str(exc)})
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result


def display_json(value: Any) -> None:
    print(json.dumps(as_text(value), ensure_ascii=False, indent=2, sort_keys=True))


def print_database(result: dict[str, Any], include_values: bool) -> None:
    db = result["database"]
    if not result.get("ok"):
        print(f"[!] DB{db}: unable to inspect")
    else:
        print(f"[+] DB{db}: {result.get('key_count', 0)} key(s), scanned {result.get('keys_scanned', 0)}")
        for item in result.get("keys", []):
            print(f"  - {json.dumps(item['key'], ensure_ascii=False)} [{item.get('type', 'unknown')}]")
            if include_values and ("value" in item or "entries" in item or "items" in item or "members_with_scores" in item):
                print("    " + json.dumps({k: v for k, v in item.items() if k not in ("key", "type")}, ensure_ascii=False))
    if result.get("keys_truncated"):
        print("  [!] Key scan reached --max-keys; report is incomplete.")
    for error in result.get("errors", []):
        label = f" key {json.dumps(error['key'], ensure_ascii=False)}" if "key" in error else ""
        print(f"  [!] Error{label}: {error['error']}")


def safe_shell(client: RedisClient, command_line: str, max_items: int, max_value_chars: int) -> Any:
    """Execute a small allowlist of non-mutating Redis diagnostic commands."""
    import shlex

    try:
        parts = shlex.split(command_line)
    except ValueError as exc:
        raise RedisMapError(f"Could not parse command: {exc}") from exc
    if not parts:
        return None
    command = parts[0].upper()
    args = parts[1:]

    if command == "PING" and not args:
        return client.execute("PING")
    if command == "INFO" and len(args) <= 1:
        return client.execute("INFO", *args)
    if command == "DBSIZE" and not args:
        return client.execute("DBSIZE")
    if command == "TYPE" and len(args) == 1:
        return client.execute("TYPE", args[0])
    if command == "GET" and len(args) == 1:
        return value_preview(client, args[0].encode("utf-8"), "string", max_items, max_value_chars)
    if command == "HGETALL" and len(args) == 1:
        return value_preview(client, args[0].encode("utf-8"), "hash", max_items, max_value_chars)
    if command in {"LLEN", "SCARD", "ZCARD", "XLEN"} and len(args) == 1:
        return client.execute(command, args[0])
    if command == "LRANGE" and len(args) == 3:
        try:
            start, stop = int(args[1]), int(args[2])
        except ValueError as exc:
            raise RedisMapError("LRANGE indexes must be integers") from exc
        if start < 0 or stop < start or stop - start + 1 > max_items:
            raise RedisMapError(f"LRANGE must request between 1 and {max_items} items")
        return client.execute("LRANGE", args[0], start, stop)
    if command == "ZRANGE" and len(args) in (3, 4):
        try:
            start, stop = int(args[1]), int(args[2])
        except ValueError as exc:
            raise RedisMapError("ZRANGE indexes must be integers") from exc
        if start < 0 or stop < start or stop - start + 1 > max_items:
            raise RedisMapError(f"ZRANGE must request between 1 and {max_items} items")
        if len(args) == 4 and args[3].upper() != "WITHSCORES":
            raise RedisMapError("The only supported ZRANGE option is WITHSCORES")
        return client.execute("ZRANGE", args[0], start, stop, *(["WITHSCORES"] if len(args) == 4 else []))

    raise RedisMapError(
        "Read-only command not allowed. Supported: PING, INFO [section], DBSIZE, TYPE key, "
        "GET key, HGETALL key, LLEN key, LRANGE key start stop, SCARD key, ZCARD key, XLEN key, ZRANGE key start stop [WITHSCORES]."
    )


def interactive_shell(args: argparse.Namespace) -> None:
    print("[*] Read-only Redis diagnostic console. Type help or exit.")
    print("[*] Mutating commands such as SET, DEL, CONFIG, FLUSHDB and FLUSHALL are blocked.")
    try:
        with make_client(args, 0) as client:
            while True:
                try:
                    command = input("redis> ").strip()
                except EOFError:
                    print()
                    break
                if command.lower() in {"exit", "quit"}:
                    break
                if command.lower() == "help":
                    print("PING | INFO [section] | DBSIZE | TYPE key | GET key | HGETALL key | LLEN key | LRANGE key start stop | SCARD key | ZCARD key | XLEN key | ZRANGE key start stop [WITHSCORES]")
                    continue
                try:
                    response = safe_shell(client, command, args.max_items, args.max_value_chars)
                    if response is not None:
                        display_json(response)
                except (RedisMapError, OSError, ssl.SSLError, TimeoutError) as exc:
                    print(f"[!] {exc}")
    except (RedisMapError, OSError, ssl.SSLError, TimeoutError) as exc:
        print(f"[!] Could not open Redis console: {exc}", file=sys.stderr)


def save_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically so an interrupted run does not leave a half-written report.
    fd, temporary_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_path, path)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="RedisMap: bounded, read-only Redis inventory and audit utility",
        epilog="Use only against systems you own or are explicitly authorised to assess.",
    )
    parser.add_argument("-t", "--target", required=True, help="Redis hostname or IP address")
    parser.add_argument("-p", "--port", type=valid_port, default=6379, help="Redis port (default: 6379)")
    parser.add_argument("--dbs", action="store_true", help="Inventory keys in each configured database")
    parser.add_argument("--keys", action="store_true", help="List keys in DB0")
    parser.add_argument("--values", action="store_true", help="Include bounded value previews (DB0, or all DBs with --dump-all)")
    parser.add_argument("--dump-all", action="store_true", help="Inventory all databases in the configured range")
    parser.add_argument("--shell", action="store_true", help="Open the read-only diagnostic console")
    parser.add_argument("--username", help="Redis ACL username (requires --password-prompt)")
    parser.add_argument("--password-prompt", action="store_true", help="Prompt securely for a Redis password")
    parser.add_argument("--tls", action="store_true", help="Use TLS for the Redis connection")
    parser.add_argument("--ca-cert", help="CA certificate PEM path when using TLS")
    parser.add_argument("--timeout", type=non_negative_float, default=DEFAULT_TIMEOUT, help="Socket timeout in seconds (default: 3)")
    parser.add_argument("--threads", type=positive_int, default=1, help="Concurrent database workers for --dump-all/--dbs (default: 1)")
    parser.add_argument("-T", "--delay", type=non_negative_float, default=0, help="Delay between scanned keys in seconds")
    parser.add_argument("--db-count", type=positive_int, default=DEFAULT_DB_COUNT, help="Number of database indexes to inspect (default: 16)")
    parser.add_argument("--max-keys", type=positive_int, default=DEFAULT_MAX_KEYS, help=f"Maximum keys per database (default: {DEFAULT_MAX_KEYS})")
    parser.add_argument("--max-items", type=positive_int, default=DEFAULT_MAX_ITEMS, help=f"Maximum collection items in a value preview (default: {DEFAULT_MAX_ITEMS})")
    parser.add_argument("--max-value-chars", type=positive_int, default=1000, help="Maximum characters per string preview (default: 1000)")
    parser.add_argument("-o", "--output", help="Report path (default: reports/<target>_<port>_<timestamp>.json)")
    parser.add_argument("--no-color", action="store_true", help="Disable terminal colors")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout == 0:
        parser.error("--timeout must be greater than zero")
    if args.threads > 32:
        parser.error("--threads must not exceed 32")
    if args.username and not args.password_prompt:
        parser.error("--username requires --password-prompt")
    if args.ca_cert and not args.tls:
        parser.error("--ca-cert requires --tls")
    if args.shell and any((args.dbs, args.keys, args.values, args.dump_all)):
        parser.error("--shell cannot be combined with inventory options")
    if args.dbs and args.dump_all:
        parser.error("--dbs and --dump-all are redundant; choose one")
    if args.dbs and args.keys:
        parser.error("--dbs and --keys are separate modes; choose one")
    if args.keys and (args.values or args.dump_all):
        parser.error("--keys cannot be combined with --values or --dump-all; use --values for DB0 previews")
    if args.dbs and args.values:
        parser.error("--dbs lists keys only; use --dump-all --values for value previews")

    if args.password_prompt:
        args.password = getpass.getpass("Redis password: ")
    else:
        args.password = None

    color_enabled = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    cyan, green, yellow, reset = ("\033[96m", "\033[92m", "\033[93m", "\033[0m") if color_enabled else ("", "", "", "")
    print(f"{cyan}RedisMap v{VERSION}{reset}  |  Redis inventory & audit")
    print("Use only on Redis instances you own or are authorised to assess.\n")
    print(f"[*] Connecting to {args.target}:{args.port} ...")

    try:
        with make_client(args, 0) as client:
            pong = client.execute("PING")
            if pong != b"PONG":
                print(f"[!] Unexpected PING response: {as_text(pong)}", file=sys.stderr)
                return 2
    except (RedisMapError, OSError, ssl.SSLError, TimeoutError) as exc:
        print(f"{yellow}[!] Connection failed: {exc}{reset}", file=sys.stderr)
        print("    Check reachability, authentication, TLS settings, and server ACLs.", file=sys.stderr)
        return 2

    print(f"{green}[+] Redis responded to PING.{reset}")
    if args.shell:
        interactive_shell(args)
        return 0

    started = time.monotonic()
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report: dict[str, Any] = {
        "tool": "RedisMap",
        "version": VERSION,
        "target": args.target,
        "port": args.port,
        "scanned_at_utc": timestamp,
        "mode": "dump-all" if args.dump_all else "dbs" if args.dbs else "values" if args.values else "keys" if args.keys else "ping",
        "limits": {"db_count": args.db_count, "max_keys_per_db": args.max_keys, "max_items_per_value": args.max_items, "max_value_chars": args.max_value_chars},
        "results": [],
    }

    if args.dump_all or args.dbs:
        dbs = list(range(args.db_count))
        workers = min(args.threads, len(dbs))
        futures = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for db in dbs:
                futures[pool.submit(inspect_database, args, db, args.dump_all and args.values)] = db
            ordered: dict[int, dict[str, Any]] = {}
            for future in as_completed(futures):
                db = futures[future]
                try:
                    ordered[db] = future.result()
                except Exception as exc:  # Preserve partial results if a worker unexpectedly fails.
                    ordered[db] = {"database": db, "ok": False, "keys": [], "errors": [{"error": str(exc)}]}
        for db in dbs:
            result = ordered[db]
            report["results"].append(result)
            print_database(result, args.dump_all and args.values)
    elif args.keys or args.values:
        result = inspect_database(args, 0, args.values)
        report["results"].append(result)
        print_database(result, args.values)
    else:
        report["results"].append({"database": 0, "ok": True, "ping": "PONG", "note": "Connectivity check only; no keys were read."})
        print("[*] Connectivity check only. Use --keys or --values to inspect DB0.")

    report["duration_seconds"] = round(time.monotonic() - started, 3)
    safe_target = re.sub(r"[^A-Za-z0-9_.-]+", "_", args.target).strip("._") or "target"
    default_path = Path("reports") / f"{safe_target}_{args.port}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output_path = Path(args.output).expanduser() if args.output else default_path
    try:
        save_report(output_path, report)
        print(f"[+] JSON report saved: {output_path}")
    except OSError as exc:
        print(f"[!] Could not save report to {output_path}: {exc}", file=sys.stderr)
        return 3

    return 0 if all(r.get("ok", True) for r in report["results"]) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[!] Interrupted.", file=sys.stderr)
        raise SystemExit(130)
