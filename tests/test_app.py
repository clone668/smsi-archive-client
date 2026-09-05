from __future__ import annotations

import signal

import pytest

import app as entrypoint
from archive_backup.config import ConfigStore


def test_sigterm_stops_the_archive_worker_before_exiting(tmp_path) -> None:
    """A restart must cancel the archive worker, not kill it mid-download.

    systemd stops the client with SIGTERM on every restart, including the one
    deploy/install_ubuntu.sh performs.  Without a handler the process dies on the
    spot and the day stays 下载中 until the next start repairs it.
    """

    store = ConfigStore(tmp_path / "state")
    store.load()
    flask_app = entrypoint.create_app(store)
    service = flask_app.extensions["smsi_archive_service"]
    original_stop = service.stop
    budgets: list[float] = []

    def record_stop(timeout: float = 30) -> bool:
        budgets.append(timeout)
        return original_stop(timeout)

    service.stop = record_stop  # type: ignore[method-assign]
    previous_handler = signal.getsignal(signal.SIGTERM)
    try:
        entrypoint._stop_on_signal(flask_app)
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        with pytest.raises(SystemExit) as exit_info:
            handler(signal.SIGTERM, None)

        assert exit_info.value.code == 0
        # 20 s leaves room inside the unit's TimeoutStopSec=30, so the graceful
        # stop finishes rather than being cut short by SIGKILL.
        assert budgets == [20]
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        original_stop()
