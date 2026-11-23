"""Рівні міркування агента Codex, які можна обрати в LazyRef."""

DEFAULT_REASONING_EFFORT = "medium"
REASONING_CHOICES = (("Low", "low"), ("Medium", "medium"), ("High", "high"), ("Extra high", "xhigh"))
REASONING_LABELS = {value: title for title, value in REASONING_CHOICES}


def selected_reasoning(value):
    """Міграція старих/некоректних налаштувань інтерфейсу до явного Medium."""
    return value if isinstance(value, str) and value in REASONING_LABELS else DEFAULT_REASONING_EFFORT


def validate_reasoning(value):
    if not isinstance(value, str) or value not in REASONING_LABELS:
        raise ValueError("Обери рівень міркування: Low, Medium, High або Extra high.")
