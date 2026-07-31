"""Tests for the browser suite's WSGI server helper.

No browser involved — these pin the two properties the fixtures depend on:
requests run concurrently, and teardown still waits for them.
"""
import threading
import time
import urllib.request

import pytest
from flask import Flask

from e2e.threaded_server import start_server, stop_server


def _get(url, results=None, key=None):
    """Fire a GET on a background thread, recording the body when asked."""
    def run():
        body = urllib.request.urlopen(url, timeout=30).read().decode()
        if results is not None:
            results[key] = body

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_requests_are_served_concurrently():
    """The reason for threading: a slow handler must not stall the page.

    With the single-threaded ``make_server`` default this took as long as
    the slow handler; the browser fixtures load a page whose thumbnails
    would queue behind the navbar's polls.
    """
    app = Flask(__name__)
    entered = threading.Event()

    @app.route("/slow")
    def slow():
        entered.set()
        time.sleep(2.0)
        return "slow"

    @app.route("/fast")
    def fast():
        return "fast"

    server, thread, url = start_server(app)
    try:
        slow_thread = _get(f"{url}/slow")
        assert entered.wait(timeout=5), "slow handler never started"

        started = time.monotonic()
        assert urllib.request.urlopen(f"{url}/fast", timeout=10).read() == b"fast"
        assert time.monotonic() - started < 1.0

        slow_thread.join(timeout=10)
    finally:
        stop_server(server, thread)


def test_stop_server_waits_for_in_flight_request():
    """Teardown must not run while a handler is still touching the app.

    ``BaseServer.shutdown()`` finished the current handler on the old
    single-threaded server, so the fixture could close app resources and the
    Database right after it. Threading breaks that unless we drain, and tests
    like ``test_folder_tree_reveal_fires_endpoint`` return while their handler
    is still inside ``subprocess.run(..., timeout=5)``.
    """
    app = Flask(__name__)
    entered = threading.Event()
    completed = threading.Event()

    @app.route("/slow")
    def slow():
        entered.set()
        time.sleep(1.5)
        completed.set()
        return "done"

    server, thread, url = start_server(app)
    results = {}
    request_thread = _get(f"{url}/slow", results, "slow")
    assert entered.wait(timeout=5), "slow handler never started"

    stop_server(server, thread)

    assert completed.is_set(), "teardown returned while a handler was running"
    request_thread.join(timeout=5)
    assert results.get("slow") == "done"


def test_stop_server_gives_up_on_a_wedged_handler():
    """The drain is bounded, so one stuck handler can't hang the suite.

    An SSE stream pins its thread for the life of the connection, so an
    unbounded join would never return. Give up and warn instead.
    """
    app = Flask(__name__)
    entered = threading.Event()
    release = threading.Event()

    @app.route("/wedged")
    def wedged():
        entered.set()
        release.wait(timeout=30)
        return "released"

    server, thread, url = start_server(app)
    _get(f"{url}/wedged")
    assert entered.wait(timeout=5), "wedged handler never started"

    started = time.monotonic()
    try:
        with pytest.warns(UserWarning, match="request handler"):
            stop_server(server, thread, drain_timeout=0.5)
        assert time.monotonic() - started < 5.0
    finally:
        release.set()
