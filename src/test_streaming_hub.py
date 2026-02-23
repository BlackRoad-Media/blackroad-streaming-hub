"""Tests for streaming_hub."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from streaming_hub import (
    Stream,
    StreamConfig,
    add_segment,
    create_stream,
    generate_hls_config,
    get_health_history,
    get_stream_stats,
    list_streams,
    m3u8_playlist,
    monitor_health,
    start_stream,
    stop_stream,
)


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def sample_stream(tmp_db):
    s = Stream(
        name="Test Stream",
        source_url="rtmp://live.blackroad.ai/input",
        target_url="https://cdn.blackroad.ai/stream/playlist.m3u8",
        protocol="hls",
        bitrate_kbps=3000,
    )
    return create_stream(s, db=tmp_db), tmp_db


def test_create_stream(sample_stream):
    stream, db = sample_stream
    streams = list_streams(db=db)
    assert any(s["id"] == stream.id for s in streams)


def test_start_stop(sample_stream):
    stream, db = sample_stream
    result = start_stream(stream.id, db=db)
    assert result["ok"] is True
    assert result["status"] == "running"

    result2 = stop_stream(stream.id, db=db)
    assert result2["ok"] is True
    assert result2["status"] == "stopped"


def test_start_already_running(sample_stream):
    stream, db = sample_stream
    start_stream(stream.id, db=db)
    result = start_stream(stream.id, db=db)
    assert result["ok"] is False


def test_monitor_health(sample_stream):
    stream, db = sample_stream
    start_stream(stream.id, db=db)
    result = monitor_health(
        stream.id, bitrate_kbps=2800, dropped_frames=5,
        buffer_secs=2.5, latency_ms=120, db=db
    )
    assert result["ok"] is True
    assert result["status"] == "healthy"


def test_monitor_health_critical(sample_stream):
    stream, db = sample_stream
    start_stream(stream.id, db=db)
    result = monitor_health(stream.id, dropped_frames=300, latency_ms=6000, db=db)
    assert result["status"] == "critical"


def test_generate_hls_config(sample_stream):
    stream, db = sample_stream
    config = generate_hls_config(stream)
    assert config["type"] == "HLS"
    assert config["target_bitrate_kbps"] == 3000
    assert len(config["variants"]) == 3


def test_m3u8_playlist():
    streams = [
        {"name": "High", "bitrate_kbps": 4000, "target_url": "https://cdn.example.com/high.m3u8"},
        {"name": "Low", "bitrate_kbps": 800, "target_url": "https://cdn.example.com/low.m3u8"},
    ]
    playlist = m3u8_playlist(streams)
    assert playlist.startswith("#EXTM3U")
    assert "BANDWIDTH=4000000" in playlist
    assert "High" in playlist


def test_stream_stats(sample_stream):
    stream, db = sample_stream
    start_stream(stream.id, db=db)
    stats = get_stream_stats(db=db)
    assert stats["total_streams"] >= 1
    assert "running" in stats["by_status"]


def test_add_segment(sample_stream):
    stream, db = sample_stream
    result = add_segment(stream.id, sequence=0, url="https://cdn.example.com/seg0.ts", db=db)
    assert result["ok"] is True
    hist = get_health_history(stream.id, db=db)
    # health_checks table populated by start or monitor calls
    assert isinstance(hist, list)
