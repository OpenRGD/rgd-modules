"""OpenRGD experimental module registry API (candidate)."""
from .core import RegistryError, catalog, entry, install, resolve_binary, status_of

__all__ = ["RegistryError", "catalog", "entry", "install", "resolve_binary", "status_of"]
