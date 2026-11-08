"""Rounds an invoice total to whole cents."""


def round_cents(amount: float) -> int:
    return int(round(amount * 100))
