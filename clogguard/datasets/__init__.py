"""Open-dataset registry, downloader and loaders (workstream B).

Open-dataset runs carry ``data_source="open_dataset"`` and a citation, and are never reported
as ClogGuard hardware performance.
"""

from clogguard.datasets.registry import REGISTRY, DatasetInfo, RemoteFile, get

__all__ = ["REGISTRY", "DatasetInfo", "RemoteFile", "get"]
