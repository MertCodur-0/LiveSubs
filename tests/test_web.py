import http.client
import json

import pytest

from livesubs.web.feed import WebFeed
from livesubs.web.server import WebServer, lan_address, render_page


def texts(feed: WebFeed) -> tuple[list[str], str | None]:
    return [line.text for line in feed.history], feed.current.text if feed.current else None


def test_new_line_moves_the_current_one_up() -> None:
    feed = WebFeed()
    feed.show(1, "Bir")
    feed.show(2, "İki")
    assert texts(feed) == (["Bir"], "İki")


def test_history_keeps_only_the_newest_lines() -> None:
    feed = WebFeed(history=2)
    for line_id in range(5):
        feed.show(line_id, str(line_id))
    assert texts(feed) == (["2", "3"], "4")


def test_upgrade_replaces_the_text_in_place() -> None:
    feed = WebFeed()
    feed.show(1, "Bir")
    key = feed.current.key
    feed.show(1, "Bir, iki")
    assert texts(feed) == ([], "Bir, iki")
    assert feed.current.key == key  # same element on the page: no animation


def test_late_upgrade_of_a_line_already_moved_up() -> None:
    feed = WebFeed()
    feed.show(1, "Bir")
    feed.show(2, "İki")
    feed.show(1, "Bir, tam")
    assert texts(feed) == (["Bir, tam"], "İki")


def test_clear_moves_the_line_up_and_a_repeat_brings_it_back() -> None:
    feed = WebFeed()
    feed.show(1, "Bir")
    feed.clear()
    assert texts(feed) == (["Bir"], None)
    feed.show(1, "Bir")  # the subtitle reappeared after a flash
    assert texts(feed) == ([], "Bir")


def test_new_session_never_matches_old_line_ids() -> None:
    feed = WebFeed()
    feed.show(1, "Eski")
    feed.new_session()
    feed.show(1, "Yeni")  # line ids restart with every Start
    assert texts(feed) == (["Eski"], "Yeni")


def test_snapshot_keys_are_unique() -> None:
    feed = WebFeed()
    feed.show(1, "Bir")
    feed.show(2, "İki")
    state = feed.snapshot()
    assert state == {
        "history": [{"key": 0, "text": "Bir"}],
        "current": {"key": 1, "text": "İki"},
    }


def test_page_has_its_text_filled_in() -> None:
    page = render_page().decode("utf-8")
    assert "{{" not in page
    assert "Çeviri bekleniyor" in page


def test_lan_address_is_an_ipv4_address() -> None:
    assert len(lan_address().split(".")) == 4


@pytest.fixture
def server() -> WebServer:
    web = WebServer(0, host="127.0.0.1")  # port 0: any free port
    web.start()
    yield web
    web.stop()


def _get(server: WebServer, path: str) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    conn.request("GET", path)
    return conn.getresponse()


def _next_event(response: http.client.HTTPResponse) -> dict:
    while True:
        line = response.readline().decode("utf-8")
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))


def test_serves_the_page_and_the_font(server: WebServer) -> None:
    page = _get(server, "/")
    assert page.status == 200
    assert b"EventSource" in page.read()
    font = _get(server, "/font.ttf")
    assert font.status == 200
    assert len(font.read()) > 10_000
    assert _get(server, "/../config.json").status == 404


def test_stream_sends_the_state_then_every_change(server: WebServer) -> None:
    server.show(1, "Önceki")
    events = _get(server, "/events")
    assert events.getheader("Content-Type").startswith("text/event-stream")
    assert _next_event(events)["current"]["text"] == "Önceki"  # a reconnect is right at once
    assert server.client_count == 1
    server.show(2, "Yeni satır")
    state = _next_event(events)
    assert [line["text"] for line in state["history"]] == ["Önceki"]
    assert state["current"]["text"] == "Yeni satır"
    server.clear()
    assert _next_event(events)["current"] is None


def test_stop_ends_the_streams(server: WebServer) -> None:
    events = _get(server, "/events")
    _next_event(events)
    server.stop()
    assert events.read().strip() == b""  # the rest of the last event, then closed
    assert not server.running
