"""A threaded test WSGI server whose teardown waits for in-flight requests.

The browser fixtures need a threaded server — the shipped app serves through
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
        self._in_flight_cond = threading.Condition()
        self._in_flight = 0
        self._live_threads = set()

    def process_request(self, request, client_address):
        # Count the request here, on the accept loop, *before* ThreadingMixIn
        # creates the handler thread. Counting inside the handler instead
        # would leave a window with no coverage: Thread.start() waits only
        # for _bootstrap_inner to reach _started.set(), which happens before
        # it calls run(), so start() can return while the handler body has
        # not begun. A drain racing that window would see nothing in flight
        # and let teardown proceed under a request that is about to run.
        with self._in_flight_cond:
            self._in_flight += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._finish()
            raise

    def process_request_thread(self, request, client_address):
        current = threading.current_thread()
        with self._in_flight_cond:
            self._live_threads.add(current)
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._finish(current)

    def _finish(self, thread=None):
        with self._in_flight_cond:
            self._live_threads.discard(thread)
            self._in_flight -= 1
            if self._in_flight <= 0:
                self._in_flight_cond.notify_all()

    def in_flight_count(self):
        with self._in_flight_cond:
            return self._in_flight

    def drain(self, timeout=10.0):
        """Wait for in-flight requests. Returns a label per straggler.

        Bounded on purpose: an unbounded wait would hang the whole suite on
        a wedged handler, and an SSE stream (which pins its thread for the
        life of the connection) would never finish at all.
        """
        deadline = time.monotonic() + timeout
        with self._in_flight_cond:
            while self._in_flight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    named = sorted(t.name for t in self._live_threads)
                    # A counted request whose handler has not reached its
                    # body yet has no thread to name.
                    unnamed = self._in_flight - len(named)
                    return named + ["<handler not started>"] * max(unnamed, 0)
                self._in_flight_cond.wait(timeout=remaining)
        return []


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
                len(stragglers), drain_timeout, ", ".join(stragglers),
            ),
            stacklevel=2,
        )
    server.server_close()
