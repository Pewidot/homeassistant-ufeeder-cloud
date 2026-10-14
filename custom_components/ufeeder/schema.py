"""Schema compatibility for Home Assistant before and after probatio adoption."""

try:
    import probatio as vol
except ImportError:
    import voluptuous as vol


def secret(key: str):
    """Mark credentials secret where supported; password selector masks the UI."""
    return getattr(vol, "Secret", lambda value: value)(key)
