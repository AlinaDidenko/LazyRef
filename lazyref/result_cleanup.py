"""Report teardown failures without replacing an already produced result."""

from contextlib import contextmanager
import tempfile


def cleanup_result(result, *actions):
    """Attempt each labeled cleanup once, retaining errors and existing data."""
    for label, action in actions:
        if action is None:
            continue
        try:
            action()
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            message = f"Не вдалося {label}: {detail}"
            notes = [str(note) for note in getattr(exc, "__notes__", ())]
            result.error = "\n".join(filter(None, (result.error, message, *notes)))


@contextmanager
def temporary_directory(result, **kwargs):
    """Preserve exported/session files even if temporary cleanup fails."""
    temporary = tempfile.TemporaryDirectory(**kwargs)
    try:
        yield temporary.name
    finally:
        cleanup_result(result, ("очистити тимчасові файли пошуку", temporary.cleanup))
