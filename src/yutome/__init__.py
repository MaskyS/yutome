"""YouTube channel knowledge base indexer."""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

__all__ = ["__version__"]

try:
    __version__ = _pkg_version("yutome")
except PackageNotFoundError:
    __version__ = "0.0.0+local"
