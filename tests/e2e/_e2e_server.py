"""Helpers for the e2e browser-test WSGI servers.

Shared by ``conftest.py``'s ``live_server`` fixture and
``test_new_images_pipeline.py``'s ``fresh_server`` fixture.
"""

import threading
import time


class InFlightMiddleware:
    """Track in-flight requests so a fixture can drain them at teardown.

    Werkzeug's ``ThreadedWSGIServer`` (what ``make_server(..., threaded=True)``
    returns) sets ``daemon_threads = True``, so ``socketserver.ThreadingMixIn``
    skips its ``_threads`` bookkeeping and ``server.server_close()`` does not
    join active request handlers. Tests that use ``page.expect_request(...)``
    return once the request is *sent*, not once its response arrives — so a
    handler like ``/api/files/reveal`` (up to 5s inside ``subprocess.run``) can
    still be executing while the fixture proceeds to close the app instance
    and the ``Database`` connection. Wrap the app in this middleware and call
    ``drain(...)`` before those closes.
    """

    def __init__(self, app):
        self._app = app
        self._cond = threading.Condition()
        self._in_flight = 0

    def __call__(self, environ, start_response):
        with self._cond:
            self._in_flight += 1
        try:
            iterable = self._app(environ, start_response)
        except BaseException:
            self._release()
            raise
        return _TrackedIterable(iterable, self._release)

    def _release(self):
        with self._cond:
            self._in_flight -= 1
            if self._in_flight == 0:
                self._cond.notify_all()

    def drain(self, timeout=10.0):
        """Wait for all in-flight requests to finish. Returns True if drained."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._in_flight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=remaining)
        return True


class _TrackedIterable:
    """WSGI iterable wrapper that fires ``on_close`` exactly once.

    Werkzeug always calls ``close()`` on the response iterable after the
    handler finishes — normally after iteration completes, or early if the
    client disconnects — so this reliably releases the in-flight counter.
    """

    def __init__(self, wrapped, on_close):
        self._wrapped = wrapped
        self._on_close = on_close
        self._done = False

    def __iter__(self):
        return iter(self._wrapped)

    def close(self):
        if self._done:
            return
        self._done = True
        try:
            close = getattr(self._wrapped, "close", None)
            if close is not None:
                close()
        finally:
            self._on_close()
