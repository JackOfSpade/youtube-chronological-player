"""Queue ordering/cache merge and persisted watch history, with no external services."""

import json

import pytest

import config_manager as cfg
import storage_manager as storage
import sync_service as sync


@pytest.fixture(autouse=True)
def isolated_services(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(storage, "_history_cache", None)
    monkeypatch.setattr(storage, "_history_mtime", 0)
    monkeypatch.setattr(
        "requests.sessions.Session.request",
        lambda *_a, **_k: pytest.fail("Unexpected network call"),
    )
    sync._sync_manager.reset()
    yield
    sync._sync_manager.reset()


def video(identifier, date, title="fixture"):
    return {"id": identifier, "publishedAt": date, "title": title}


def test_forced_sync_deduplicates_sorts_and_merges_cache(monkeypatch):
    fresh = [
        video("old", "2026-01-01"),
        video("new", "2026-03-01", "updated"),
        video("new", "2026-03-01"),
        {"id": None},
        {"id": "undefined"},
    ]
    cache = [video("new", "2026-03-01", "stale"), video("middle", "2026-02-01")]
    saved = []
    monkeypatch.setattr(
        cfg,
        "get_sync_params",
        lambda: ({"enabled": True}, "fixture-key", "2026-01-01", ["channel"]),
    )
    monkeypatch.setattr(cfg, "load_cache", lambda: cache)
    monkeypatch.setattr(cfg, "save_cache", lambda value: saved.append(value))
    monkeypatch.setattr(
        sync.youtube_api, "get_uploads_playlist_id", lambda *_a: "playlist"
    )
    monkeypatch.setattr(
        sync.youtube_api, "fetch_videos_from_playlist", lambda *_a: fresh
    )
    monkeypatch.setattr(
        sync.ai_filter,
        "filter_videos",
        lambda videos, **_k: iter([{"type": "result", "videos": videos}]),
    )
    result = sync.get_queue(force_sync=True)
    assert [v["id"] for v in result] == ["new", "middle", "old"]
    assert result[0]["title"] == "updated"
    assert saved == [result]
    assert not sync._sync_manager.is_running


def test_cached_queue_does_not_call_upstream(monkeypatch):
    cached = [video("cached", "2026-01-01")]
    monkeypatch.setattr(cfg, "load_cache", lambda: cached)
    monkeypatch.setattr(
        cfg, "get_sync_params", lambda: pytest.fail("cache read must not sync")
    )
    assert sync.get_queue() == cached
    events = [
        json.loads(event.removeprefix("data: ")) for event in sync.get_queue_stream()
    ]
    assert events[0] == {"type": "videos", "videos": cached}
    assert events[-1]["type"] == "done"


def test_watch_history_persists_deduplicates_and_evicts_oldest(monkeypatch):
    monkeypatch.setattr(storage, "MAX_WATCHED_HISTORY", 3)
    for identifier in ["a", "b", "a", "c", "d", "null", "  ", None]:
        storage.mark_watched(identifier)
    assert storage.get_watched_ids() == ["a", "c", "d"]
    assert storage.load_history()["last_watched_video_id"] == "d"
    storage._history_cache = None
    assert storage.get_watched_ids() == ["a", "c", "d"]
    copy = storage.load_history()
    copy["watched_video_ids"].clear()
    assert storage.get_watched_ids() == ["a", "c", "d"]


def test_corrupt_history_recovers_without_losing_next_write():
    with open(storage.HISTORY_FILE, "w") as stream:
        stream.write("{invalid")
    assert storage.load_history() == {
        "watched_video_ids": [],
        "last_watched_video_id": None,
    }
    storage.mark_watched("recovered")
    storage._history_cache = None
    assert storage.get_watched_ids() == ["recovered"]
