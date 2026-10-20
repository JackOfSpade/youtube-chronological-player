"""Exercise shipped ES modules in Chromium; all service calls are local fixtures."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

from playwright.sync_api import sync_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def main():
    root = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=str(root))
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    unexpected = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def route_request(route):
                if route.request.url.startswith(origin + "/api/comments/"):
                    route.fulfill(json={"comments": [], "nextPageToken": None})
                elif route.request.url.startswith(origin + "/"):
                    route.continue_()
                else:
                    unexpected.append(route.request.url)
                    route.abort()

            page.route("**/*", route_request)
            page.goto(origin + "/tests/browser/player-fixture.html")
            page.wait_for_function("window.testApp")
            page.evaluate("""() => {
                const {state, rebuildQueueIndex, renderQueue, updateResumeButton} = testApp;
                const image = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';
                state.queue = ['new', 'middle', 'old'].map((id, i) => ({id, title: i ? id : '<img src=x onerror=alert(1)>', channelTitle: 'fixture', publishedAt: `2026-01-0${3-i}`, thumbnail: image}));
                state.watchHistory.watched_video_ids = new Set(['middle']);
                state.pageSize = 2;
                rebuildQueueIndex(); renderQueue(); updateResumeButton();
            }""")
            assert (
                page.locator(".video-title").first.text_content()
                == "<img src=x onerror=alert(1)>"
            )
            assert page.locator(".video-title img").count() == 0
            page.get_by_text("Resume", exact=True).click()
            assert page.evaluate("played") == ["old"]
            assert page.locator("#page-indicator").inner_text() == "Page 2 of 2"
            page.evaluate("playerEvents.onStateChange({data: 0})")
            assert page.evaluate("played") == ["old", "new"]
            assert page.evaluate("beacons") == ["/api/watched/old"]
            assert page.locator("#page-indicator").inner_text() == "Page 1 of 2"
            page.evaluate("playerEvents.onStateChange({data: 0})")
            assert page.evaluate("beacons") == ["/api/watched/old", "/api/watched/new"]
            assert page.locator("#resume-btn").is_hidden()
            assert not errors, errors
            assert not unexpected, unexpected
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    print(
        "Chromium chronological playback, watched skipping, pagination and escaping passed."
    )


if __name__ == "__main__":
    main()
