"""HTTP API package for catalog onboarding."""

from .wsgi import create_app

__all__ = ["create_app"]
