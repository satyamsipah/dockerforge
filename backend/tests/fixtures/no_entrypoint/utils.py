"""Utility functions — no application entrypoint here."""


def slugify(text: str) -> str:
    return text.lower().replace(" ", "-")


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))
