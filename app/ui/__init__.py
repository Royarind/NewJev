def create_app():
    """Imported lazily so helpers in this package (hotkey) work without the web server's dependencies."""
    from .server import create_app as _create
    return _create()


__all__ = ["create_app"]
