from __future__ import annotations

import argparse

from archive_backup.config import ConfigStore
from archive_backup.web import create_app


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
    serve(app, host=host, port=port, threads=6, channel_timeout=120)


if __name__ == "__main__":
    main()
