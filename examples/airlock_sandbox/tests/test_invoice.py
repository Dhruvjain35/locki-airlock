from invoice import round_cents


def test_whole_amounts():
    assert round_cents(12.0) == 1200


def test_cents_are_kept():
    assert round_cents(12.34) == 1234
