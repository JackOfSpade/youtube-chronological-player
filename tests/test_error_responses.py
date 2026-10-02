"""Regression tests for keeping server-side exception details out of API responses."""

import inspect
import requests

import app as app_module
import youtube_api


SECRET = "internal-path-and-api-key-should-not-reach-clients"


def _client():
    app_module._last_request_times.clear()
    app_module.app.config.update(TESTING=False)
    return app_module.app.test_client()


def test_queue_exception_is_logged_but_not_returned(monkeypatch):
    def fail_queue(*_args, **_kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(app_module.sync_service, "get_queue", fail_queue)

    response = _client().get("/api/queue")

    assert response.status_code == 500
    assert response.get_json() == {
        "status": "error",
        "message": "Internal Server Error",
    }
    assert SECRET not in response.get_data(as_text=True)


def test_config_and_channel_write_errors_are_not_returned(monkeypatch):
    def fail_config():
        raise RuntimeError(SECRET)

    monkeypatch.setattr(app_module.cfg, "load_config", fail_config)
    response = _client().get("/api/config")
    assert response.status_code == 500
    assert SECRET not in response.get_data(as_text=True)

    def invalid_channels(_channels):
        raise ValueError(SECRET)

    monkeypatch.setattr(app_module.cfg, "save_channels", invalid_channels)
    response = _client().post("/api/channels", json={"channels": []})
    assert response.status_code == 400
    assert response.get_json()["message"] == "Invalid channel data"
    assert SECRET not in response.get_data(as_text=True)

    def fail_channel_save(_channels):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(app_module.cfg, "save_channels", fail_channel_save)
    response = _client().post("/api/channels", json={"channels": []})
    assert response.status_code == 500
    assert response.get_json()["message"] == "Internal Server Error"
    assert SECRET not in response.get_data(as_text=True)


def test_watched_endpoint_does_not_return_storage_error(monkeypatch):
    def fail_write(_video_id):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(app_module.storage_manager, "mark_watched", fail_write)

    response = _client().post("/api/watched/video-id")

    assert response.status_code == 500
    assert response.get_json()["message"] == "Internal Server Error"
    assert SECRET not in response.get_data(as_text=True)


def test_upstream_error_details_are_replaced_for_search_and_comments(monkeypatch):
    monkeypatch.setattr(app_module.cfg, "load_config", lambda: {"api_key": "key"})
    unsafe_result = {"error": "REQUEST_FAILED", "message": SECRET, "status_code": 500}
    monkeypatch.setattr(app_module.youtube_api, "search_channels", lambda *_args: unsafe_result)
    monkeypatch.setattr(
        app_module.youtube_api, "fetch_video_comments", lambda *_args: unsafe_result
    )

    search_response = _client().get("/api/search_channels?q=channel")
    comments_response = _client().get("/api/comments/video-id")

    for response in (search_response, comments_response):
        assert response.status_code == 502
        assert response.get_json() == {
            "error": "REQUEST_FAILED",
            "message": "The YouTube service is temporarily unavailable.",
        }
        assert SECRET not in response.get_data(as_text=True)


def test_request_exception_details_stay_in_youtube_api_logs():
    class FailingSession:
        def get(self, *_args, **_kwargs):
            raise requests.RequestException(SECRET)

    result = youtube_api._api_get(
        "https://example.invalid/test", params={"key": SECRET}, session=FailingSession()
    )

    assert result == {"error": "REQUEST_FAILED"}
    assert SECRET not in result.values()


def test_entry_point_never_enables_the_flask_debugger():
    source = inspect.getsource(app_module)

    assert "app.run(debug=True" not in source
