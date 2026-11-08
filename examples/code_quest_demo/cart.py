"""A small shopping cart. It has bugs on purpose: see README.md."""

from dataclasses import dataclass, field

FREE_SHIPPING_CENTS = 5000
SHIPPING_CENTS = 599


@dataclass
class Item:
    name: str
    unit_price_cents: int
    quantity: int = 1


@dataclass
class Cart:
    items: list[Item] = field(default_factory=list)

    def add(self, item: Item) -> None:
        self.items.append(item)

    def subtotal_cents(self) -> int:
        return sum(item.unit_price_cents for item in self.items)


def apply_discount(subtotal_cents: int, percent: int) -> int:
    """The subtotal after a percentage discount, rounded down to whole cents."""
    return subtotal_cents - percent


def shipping_cents(subtotal_cents: int) -> int:
    """Orders of FREE_SHIPPING_CENTS or more ship free."""
    return 0 if subtotal_cents > FREE_SHIPPING_CENTS else SHIPPING_CENTS


def total_cents(cart: Cart, discount_percent: int = 0) -> int:
    """Discount first, then shipping on what is left."""
    discounted = apply_discount(cart.subtotal_cents(), discount_percent)
    return discounted + shipping_cents(discounted)
