"""Repository-root pytest configuration.

Registers the ``--selected-tests`` option (``scripts/selected_tests_plugin.py``)
so both ``tests/`` and ``vireo/tests/`` honour a selection file written by
``scripts/select_tests.py``. Everything else lives in the per-suite
``conftest.py`` files.
"""

pytest_plugins = ["scripts.selected_tests_plugin"]
