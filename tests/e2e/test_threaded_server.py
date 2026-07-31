"""Tests for the browser suite's WSGI server helper.

No browser involved — these pin the two properties the fixtures depend on:
requests run concurrently, and teardown still waits for them.
"""
import threading
import time
import urllib.request

import pytest
from flask import Flask

from e2e.threaded_server import DrainingWSGIServer, start_server, stop_server


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

    The handshake is the assertion — ``/blocker`` holds a thread until
    ``/fast`` has been served, so ``/fast`` completing at all proves the two
    ran at once. A single-threaded server would deadlock here: ``/fast``
    would wait on ``/blocker``, which only returns once ``/fast`` is done.

    Deliberately no wall-clock threshold. A "``/fast`` must return within
    1s" check would fail on a stalled runner for exactly the machine-load
    condition this change exists to tolerate.
    """
    app = Flask(__name__)
    entered = threading.Event()
    release = threading.Event()

    @app.route("/blocker")
    def blocker():
        entered.set()
        release.wait(timeout=30)
        return "blocker"

    @app.route("/fast")
    def fast():
        return "fast"

    server, thread, url = start_server(app)
    try:
        blocker_thread = _get(f"{url}/blocker")
        assert entered.wait(timeout=10), "blocker handler never started"

        assert urllib.request.urlopen(f"{url}/fast", timeout=30).read() == b"fast"

        release.set()
        blocker_thread.join(timeout=10)
    finally:
        release.set()
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
    assert entered.wait(timeout=10), "slow handler never started"

    stop_server(server, thread)

    assert completed.is_set(), "teardown returned while a handler was running"
    request_thread.join(timeout=10)
    assert results.get("slow") == "done"


class _LateHandlerServer(DrainingWSGIServer):
    """Simulates the scheduler not having run a freshly started handler.

    ``Thread.start()`` returns once ``_bootstrap_inner`` reaches
    ``_started.set()``, which is before it calls ``run()``. Sleeping at the
    top of ``process_request_thread`` — ahead of the base class's own
    bookkeeping — reproduces that window deterministically.

    The delay has to clear ``serve_forever``'s 0.5s poll interval, since
    ``shutdown()`` can itself take that long to return and would otherwise
    hide the window: at 0.3s a drain that only knows about started handlers
    still passes these tests, and at 1.5s it does not.
    """

    HANDLER_START_DELAY = 1.5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accepted = threading.Event()
        self.count_at_accept = None

    def process_request(self, request, client_address):
        super().process_request(request, client_address)
        # Reached only after Thread.start() has returned.
        self.count_at_accept = self.in_flight_count()
        self.accepted.set()

    def process_request_thread(self, request, client_address):
        time.sleep(self.HANDLER_START_DELAY)
        super().process_request_thread(request, client_address)


def test_request_is_counted_before_its_handler_body_runs():
    """A request accepted but not yet running still counts as in flight.

    Counting inside the handler would read zero for this whole window, and
    a drain landing in it would wave teardown through.
    """
    app = Flask(__name__)

    @app.route("/x")
    def x():
        return "x"

    server = _LateHandlerServer("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.socket.getsockname()[1]
    try:
        _get(f"http://127.0.0.1:{port}/x")
        assert server.accepted.wait(timeout=10), "request was never accepted"
        assert server.count_at_accept == 1
    finally:
        stop_server(server, thread)


def test_stop_server_waits_for_a_handler_that_has_not_started_yet():
    """The drain covers the accepted-but-not-yet-running window too."""
    app = Flask(__name__)
    completed = threading.Event()

    @app.route("/x")
    def x():
        completed.set()
        return "x"

    server = _LateHandlerServer("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.socket.getsockname()[1]

    _get(f"http://127.0.0.1:{port}/x")
    assert server.accepted.wait(timeout=10), "request was never accepted"

    stop_server(server, thread)

    assert completed.is_set(), "teardown skipped a handler that had not begun"


def test_stop_server_gives_up_on_a_wedged_handler():
    """The drain is bounded, so one stuck handler can't hang the suite.

    An SSE stream pins its thread for the life of the connection, so an
    unbounded wait would never return. Give up and warn instead.
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
    assert entered.wait(timeout=10), "wedged handler never started"

    started = time.monotonic()
    try:
        with pytest.warns(UserWarning, match="request handler"):
            stop_server(server, thread, drain_timeout=0.5)
        assert time.monotonic() - started < 10.0
    finally:
        release.set()
