"""HTTP service (FastAPI). Needs the ``[server]`` extra."""

from dravenpdf.server.app import create_app

__all__ = ["create_app"]
