#!/usr/bin/env python3
"""BlackRoad Streaming Hub - Manage, monitor, and configure media streams."""

import argparse
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

DB_PATH = Path.home() / ".blackroad" / "streaming_hub.db"

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class StreamConfig:
    segment_duration_secs: int = 6
    playlist_size: int = 5
    buffer_secs: int = 10
    low_latency: bool = False
    encryption: bool = False


@dataclass
class Stream:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    source_url: str = ""
    target_url: str = ""
    protocol: str = "hls"          # hls | rtmp | dash | srt
    bitrate_kbps: int = 2000
    status: str = "stopped"        # stopped | running | error | buffering
    config: StreamConfig = field(default_factory=StreamConfig)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    started_at: Optional[str] = None
    stopped_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _conn(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    _init_db(con)
    return con


def _init_db(con: sqlite3.Connection) -> None:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS streams (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            source_url      TEXT NOT NULL DEFAULT '',
            target_url      TEXT NOT NULL DEFAULT '',
            protocol        TEXT NOT NULL DEFAULT 'hls',
            bitrate_kbps    INTEGER DEFAULT 2000,
            status          TEXT NOT NULL DEFAULT 'stopped',
            config          TEXT NOT NULL DEFAULT '{}',
            created_at      TEXT NOT NULL,
            started_at      TEXT,
            stopped_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS segments (
            id          TEXT PRIMARY KEY,
            stream_id   TEXT NOT NULL REFERENCES streams(id),
            sequence    INTEGER NOT NULL,
            url         TEXT NOT NULL,
            duration_secs REAL DEFAULT 6.0,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS health_checks (
            id              TEXT PRIMARY KEY,
            stream_id       TEXT NOT NULL REFERENCES streams(id),
            status          TEXT NOT NULL,
            bitrate_kbps    INTEGER DEFAULT 0,
            dropped_frames  INTEGER DEFAULT 0,
            buffer_secs     REAL DEFAULT 0,
            latency_ms      INTEGER DEFAULT 0,
            checked_at      TEXT NOT NULL
        );
    """)
    con.commit()


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def create_stream(stream: Stream, db: Path = DB_PATH) -> Stream:
    with _conn(db) as con:
        con.execute(
            "INSERT OR REPLACE INTO streams VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                stream.id,
                stream.name,
                stream.source_url,
                stream.target_url,
                stream.protocol,
                stream.bitrate_kbps,
                stream.status,
                json.dumps(asdict(stream.config)),
                stream.created_at,
                stream.started_at,
                stream.stopped_at,
            ),
        )
    return stream


def start_stream(stream_id: str, db: Path = DB_PATH) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with _conn(db) as con:
        row = con.execute("SELECT * FROM streams WHERE id=?", (stream_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"Stream {stream_id} not found"}
        if row["status"] == "running":
            return {"ok": False, "error": "Stream already running"}

        con.execute(
            "UPDATE streams SET status='running', started_at=?, stopped_at=NULL WHERE id=?",
            (now, stream_id),
        )
        # Emit an initial health check record
        con.execute(
            "INSERT INTO health_checks VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), stream_id, "running", row["bitrate_kbps"], 0, 10.0, 0, now),
        )
    return {"ok": True, "stream_id": stream_id, "status": "running", "started_at": now}


def stop_stream(stream_id: str, db: Path = DB_PATH) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with _conn(db) as con:
        rows_affected = con.execute(
            "UPDATE streams SET status='stopped', stopped_at=? WHERE id=?",
            (now, stream_id),
        ).rowcount
    if rows_affected == 0:
        return {"ok": False, "error": f"Stream {stream_id} not found"}
    return {"ok": True, "stream_id": stream_id, "status": "stopped", "stopped_at": now}


def monitor_health(
    stream_id: str,
    bitrate_kbps: Optional[int] = None,
    dropped_frames: int = 0,
    buffer_secs: float = 0.0,
    latency_ms: int = 0,
    db: Path = DB_PATH,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with _conn(db) as con:
        row = con.execute("SELECT * FROM streams WHERE id=?", (stream_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"Stream {stream_id} not found"}

        effective_bitrate = bitrate_kbps if bitrate_kbps is not None else row["bitrate_kbps"]
        status = "healthy"
        if dropped_frames > 50:
            status = "degraded"
        if dropped_frames > 200 or latency_ms > 5000:
            status = "critical"

        con.execute(
            "INSERT INTO health_checks VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), stream_id, status,
                effective_bitrate, dropped_frames, buffer_secs, latency_ms, now,
            ),
        )
    return {
        "ok": True,
        "stream_id": stream_id,
        "status": status,
        "checked_at": now,
        "metrics": {
            "bitrate_kbps": effective_bitrate,
            "dropped_frames": dropped_frames,
            "buffer_secs": buffer_secs,
            "latency_ms": latency_ms,
        },
    }


def get_stream_stats(db: Path = DB_PATH) -> dict:
    with _conn(db) as con:
        total = con.execute("SELECT COUNT(*) n FROM streams").fetchone()["n"]
        by_status = con.execute(
            "SELECT status, COUNT(*) n FROM streams GROUP BY status"
        ).fetchall()
        by_protocol = con.execute(
            "SELECT protocol, COUNT(*) n FROM streams GROUP BY protocol"
        ).fetchall()
        recent_health = con.execute(
            """SELECT hc.stream_id, hc.status, hc.bitrate_kbps, hc.dropped_frames,
                      hc.latency_ms, hc.checked_at, s.name
               FROM health_checks hc
               JOIN streams s ON s.id = hc.stream_id
               ORDER BY hc.checked_at DESC LIMIT 20"""
        ).fetchall()

    return {
        "total_streams": total,
        "by_status": {r["status"]: r["n"] for r in by_status},
        "by_protocol": {r["protocol"]: r["n"] for r in by_protocol},
        "recent_health": [dict(r) for r in recent_health],
    }


def generate_hls_config(stream: Stream) -> dict:
    """Return an HLS-style configuration block for a stream."""
    cfg = stream.config
    return {
        "stream_id": stream.id,
        "type": "HLS",
        "source": stream.source_url,
        "output": stream.target_url or f"/streams/{stream.id}/playlist.m3u8",
        "segment_duration": cfg.segment_duration_secs,
        "playlist_size": cfg.playlist_size,
        "buffer_seconds": cfg.buffer_secs,
        "low_latency": cfg.low_latency,
        "encryption": cfg.encryption,
        "target_bitrate_kbps": stream.bitrate_kbps,
        "variants": [
            {"bitrate_kbps": stream.bitrate_kbps, "suffix": "high"},
            {"bitrate_kbps": stream.bitrate_kbps // 2, "suffix": "med"},
            {"bitrate_kbps": stream.bitrate_kbps // 4, "suffix": "low"},
        ],
    }


def m3u8_playlist(streams: List[dict], db: Path = DB_PATH) -> str:
    """Generate a multi-bitrate M3U8 master playlist."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for s in streams:
        bw = s.get("bitrate_kbps", 2000) * 1000
        name = s.get("name", "stream")
        url = s.get("target_url", "") or s.get("source_url", "")
        lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bw},NAME=\"{name}\"")
        lines.append(url)
    return "\n".join(lines)


def add_segment(
    stream_id: str,
    sequence: int,
    url: str,
    duration_secs: float = 6.0,
    db: Path = DB_PATH,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with _conn(db) as con:
        con.execute(
            "INSERT INTO segments VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), stream_id, sequence, url, duration_secs, now),
        )
    return {"ok": True, "stream_id": stream_id, "sequence": sequence}


def list_streams(status: Optional[str] = None, db: Path = DB_PATH) -> List[dict]:
    with _conn(db) as con:
        if status:
            rows = con.execute(
                "SELECT * FROM streams WHERE status=? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM streams ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_health_history(stream_id: str, limit: int = 50, db: Path = DB_PATH) -> List[dict]:
    with _conn(db) as con:
        rows = con.execute(
            "SELECT * FROM health_checks WHERE stream_id=? ORDER BY checked_at DESC LIMIT ?",
            (stream_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="BlackRoad Streaming Hub")
    sub = p.add_subparsers(dest="cmd", required=True)

    cs = sub.add_parser("create", help="Create a stream")
    cs.add_argument("--name", required=True)
    cs.add_argument("--source", default="")
    cs.add_argument("--target", default="")
    cs.add_argument("--protocol", default="hls", choices=["hls", "rtmp", "dash", "srt"])
    cs.add_argument("--bitrate", type=int, default=2000)

    start = sub.add_parser("start", help="Start a stream")
    start.add_argument("stream_id")

    stop = sub.add_parser("stop", help="Stop a stream")
    stop.add_argument("stream_id")

    health = sub.add_parser("health", help="Record health check")
    health.add_argument("stream_id")
    health.add_argument("--bitrate", type=int, default=None)
    health.add_argument("--dropped-frames", type=int, default=0)
    health.add_argument("--buffer-secs", type=float, default=0.0)
    health.add_argument("--latency-ms", type=int, default=0)

    stats = sub.add_parser("stats", help="Overall stream stats")

    hls_cfg = sub.add_parser("hls-config", help="Generate HLS config")
    hls_cfg.add_argument("stream_id")

    playlist = sub.add_parser("playlist", help="Generate M3U8 master playlist")

    ls = sub.add_parser("list", help="List streams")
    ls.add_argument("--status", default=None)

    hist = sub.add_parser("health-history", help="Health check history")
    hist.add_argument("stream_id")
    hist.add_argument("--limit", type=int, default=20)

    return p


def main(argv=None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "create":
        stream = Stream(
            name=args.name,
            source_url=args.source,
            target_url=args.target,
            protocol=args.protocol,
            bitrate_kbps=args.bitrate,
        )
        print(json.dumps(asdict(create_stream(stream)), indent=2))

    elif args.cmd == "start":
        print(json.dumps(start_stream(args.stream_id), indent=2))

    elif args.cmd == "stop":
        print(json.dumps(stop_stream(args.stream_id), indent=2))

    elif args.cmd == "health":
        print(json.dumps(monitor_health(
            args.stream_id,
            bitrate_kbps=args.bitrate,
            dropped_frames=args.dropped_frames,
            buffer_secs=args.buffer_secs,
            latency_ms=args.latency_ms,
        ), indent=2))

    elif args.cmd == "stats":
        print(json.dumps(get_stream_stats(), indent=2))

    elif args.cmd == "hls-config":
        with _conn() as con:
            row = con.execute("SELECT * FROM streams WHERE id=?", (args.stream_id,)).fetchone()
        if not row:
            print(json.dumps({"error": "not found"}))
            return
        stream = Stream(**{k: row[k] for k in ["id", "name", "source_url", "target_url",
                                                 "protocol", "bitrate_kbps", "status",
                                                 "created_at", "started_at", "stopped_at"]})
        print(json.dumps(generate_hls_config(stream), indent=2))

    elif args.cmd == "playlist":
        streams = list_streams(status="running")
        print(m3u8_playlist(streams))

    elif args.cmd == "list":
        print(json.dumps(list_streams(args.status), indent=2))

    elif args.cmd == "health-history":
        print(json.dumps(get_health_history(args.stream_id, args.limit), indent=2))


if __name__ == "__main__":
    main()
