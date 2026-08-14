"""Hermes entry point for the Auxiliary Fallbacks extension."""


def register(_ctx) -> None:
    """Load the extension.

    The extension does not add tools or hooks. Its FastAPI route is in the
    dashboard folder, and its user interface uses the Desktop Plugin SDK.
    """


__all__ = ["register"]
