


























from __future__ import annotations

import re






DIGITS = re.compile(r"^[0-9]+\Z")


def is_ascii_int(text: str) -> bool:






    return bool(DIGITS.match(text))
