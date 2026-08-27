import re
from decimal import Decimal, localcontext


_DURATION_RE = re.compile(r"(\d+(?:\.\d{1,3})?)(ms|s|m|h)")
_MULTIPLIERS = {"ms": 1, "s": 1_000, "m": 60_000, "h": 3_600_000}


def parse_duration(value: str) -> int:
    """Return a duration string as an exact integer number of milliseconds."""
    if not isinstance(value, str):
        raise TypeError("duration must be a string")

    match = _DURATION_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("invalid duration")

    number, unit = match.groups()
    with localcontext() as context:
        context.prec = len(number.replace(".", "")) + 7
        milliseconds = Decimal(number) * _MULTIPLIERS[unit]
    if milliseconds != milliseconds.to_integral_value():
        raise ValueError("duration must be an exact number of milliseconds")
    return int(milliseconds)
