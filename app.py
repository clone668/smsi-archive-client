from __future__ import annotations

import argparse
import signal

from archive_backup.config import ConfigStore
from archive_backup.web import create_app


def _stop_on_signal(app) -> None:
    """Turn systemd's SIGTERM into an ordinary exit.

    Python's default for SIGTERM is to die on the spot, which cuts the archive
    worker off mid-download and leaves the day sitting in 下载中 for the next
    start to repair.  Stopping the service first raises OperationCancelled
    inside the worker, so the day is recorded as 已取消 and the objects already
    finished in .partial are kept for the next pass.  The 20 second budget stays
    under the unit's TimeoutStopSec=30 so the graceful path completes instead of
    racing SIGKILL.
    """

    service = app.extensions.get("smsi_archive_service")

    def shutdown(_signal_number: int, _frame: object) -> None:
        if service is not None:
            service.stop(20)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)


def main() -> None:
    parser = argparse.ArgumentParser(description="SMSI 归档备份客户端")
    parser.add_argument("--host", help="覆盖 Web 监听地址")
    parser.add_argument("--port", type=int, help="覆盖 Web 监听端口")
    parser.add_argument("--debug", action="store_true")
    arguments = parser.parse_args()
    store = ConfigStore()
    config = store.load()
    app = create_app(store)
    host = arguments.host or config.web_host
    port = arguments.port or config.web_port
    if arguments.debug:
        app.run(host=host, port=port, debug=True, use_reloader=False)
        return
    try:
        from waitress import serve
    except ImportError as exc:
        raise SystemExit("缺少 waitress，请先安装 requirements.txt") from exc
    _stop_on_signal(app)
    serve(app, host=host, port=port, threads=6, channel_timeout=120)


if __name__ == "__main__":
    main()
