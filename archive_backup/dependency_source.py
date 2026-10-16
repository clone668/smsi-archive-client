from __future__ import annotations

from pathlib import Path

from .sources import split_bandwidth_limit


class DependencySource:
    def __init__(self, source, config, cancel):
        self.source, self.config, self.cancel = source, config, cancel

    def list_deliveries(self):
        return self.source.list_deliveries()

    def read_small(self, key: str, maximum: int):
        return self.source.read_small(key, maximum)

    def download(self, key: str, target: Path, size: int, progress):
        return self.source.download(key, target, cancel=self.cancel, progress=progress,
            bandwidth_limit=split_bandwidth_limit(self.config.bandwidth_limit, self.config.download_workers))
