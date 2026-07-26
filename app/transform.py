import jmespath
from jmespath.exceptions import JMESPathError


def validate_transform(mapping: dict) -> None:
    """Check a transform mapping at DESTINATION-CREATION time, not delivery time.

    A typo should fail loudly with a 4xx while a human is watching, not silently
    produce nulls at 3am inside a delivery nobody is looking at. Raises
    JMESPathError (bad expression) or ValueError (non-string expression).
    """
    for key, expr in mapping.items():
        if not isinstance(expr, str):
            raise ValueError(f"transform value for '{key}' must be a JMESPath string")
        jmespath.compile(expr)  # raises JMESPathError on bad syntax


def apply_transform(mapping: dict, payload: dict) -> dict:
    """Reshape a payload into what a destination expects.

    Empty mapping = pass the payload through unchanged. Otherwise each output
    field is a JMESPath expression evaluated against the payload. JMESPath can
    only read/reshape data — it can't run code or make network calls — so it's
    safe to accept from users, unlike arbitrary code we'd have to sandbox.
    """
    if not mapping:
        return payload
    return {key: jmespath.search(expr, payload) for key, expr in mapping.items()}


__all__ = ["validate_transform", "apply_transform", "JMESPathError"]
