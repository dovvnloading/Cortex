"""Versioned local API boundary for the opt-in web preview."""

from .app import BackendDependencies, create_app

__all__ = ["BackendDependencies", "create_app"]
