"""A threaded test WSGI server whose teardown waits for in-flight requests.

The browser fixtures need ``threaded=True`` — the shipped app serves through
waitress with 16 threads, and a single-threaded test server makes every page
load queue behind whatever else the page requested.

Threading costs one guarantee, though, and this module buys it back.
``BaseServer.shutdown()`` finishes the handler it is currently running before
it returns, so with the old single-threaded server no request could still be
in flight once the fixture moved on to ``_cleanup_app_resources()`` and
``db.close()``. Under ``ThreadingMixIn`` the handler runs on its own thread:
``shutdown()`` only stops the accept loop, and ``server_close()`` skips its
join because werkzeug sets ``daemon_threads = True`` (which makes
``block_on_close`` False).

That gap is reachable. Tests may assert on a *request* rather than its
response — ``test_folder_tree_reveal_fires_endpoint`` returns as soon as the
POST to ``/api/files/reveal`` is observed, while the handler can still spend
up to five seconds inside ``subprocess.run(..., timeout=5)``. Without a drain
the fixture tears the app down underneath that handler and the next test
starts while it is still running.
"""
import threading
import time
import warnings

from werkzeug.serving import ThreadedWSGIServer


class DrainingWSGIServer(ThreadedWSGIServer):
    """``ThreadedWSGIServer`` that can wait for its handler threads."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._live_lock = threading.Lock()
        self._live_threads = set()

    def process_request_thread(self, request, client_address):
        current = threading.current_thread()
        with self._live_lock:
            self._live_threads.add(current)
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._live_lock:
                self._live_threads.discard(current)

    def drain(self, timeout=10.0):
        """Wait for in-flight handlers. Returns the ones still running.

        Bounded on purpose: an unbounded join would hang the whole suite on
        a wedged handler, and an SSE stream (which pins its thread for the
        life of the connection) would never return at all.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._live_lock:
                live = [t for t in self._live_threads if t.is_alive()]
            if not live:
                return []
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return live
            live[0].join(timeout=remaining)


def start_server(app, host="127.0.0.1", port=0):
    """Serve ``app`` on a background thread. Returns ``(server, thread, url)``."""
    server = DrainingWSGIServer(host, port, app)
    bound_port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://{host}:{bound_port}"


def stop_server(server, thread, drain_timeout=10.0):
    """Stop accepting, drain in-flight handlers, then close the socket.

    Call this before tearing down anything a handler might touch (app
    resources, the fixture's Database), so the pre-threading invariant —
    no request is running once teardown proceeds — still holds.
    """
    server.shutdown()
    thread.join(timeout=5)
    stragglers = server.drain(timeout=drain_timeout)
    if stragglers:
        warnings.warn(
            "E2E server still had %d request handler(s) running after %.0fs: %s. "
            "Teardown is proceeding, so this test's app resources may be closed "
            "underneath them." % (
                len(stragglers), drain_timeout,
                ", ".join(sorted(t.name for t in stragglers)),
            ),
            stacklevel=2,
        )
    server.server_close()
