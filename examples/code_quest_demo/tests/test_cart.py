import pytest
from cart import Cart, Item, apply_discount, shipping_cents, total_cents


def test_empty_cart_costs_nothing():
    assert Cart().subtotal_cents() == 0


def test_subtotal_counts_quantity():
    cart = Cart()
    cart.add(Item("pen", 150, quantity=4))
    cart.add(Item("notebook", 700))
    assert cart.subtotal_cents() == 1300


def test_discount_takes_a_percentage():
    assert apply_discount(10000, 25) == 7500


def test_discount_rounds_down_to_whole_cents():
    assert apply_discount(999, 10) == 899


@pytest.mark.parametrize("percent", [-1, 101])
def test_discount_rejects_impossible_percentages(percent):
    with pytest.raises(ValueError):
        apply_discount(1000, percent)


def test_small_orders_pay_shipping():
    assert shipping_cents(4999) == 599


def test_shipping_is_free_at_exactly_the_threshold():
    assert shipping_cents(5000) == 0


def test_total_applies_discount_before_shipping():
    cart = Cart()
    cart.add(Item("headphones", 3000, quantity=2))
    assert total_cents(cart, discount_percent=25) == 4500 + 599
