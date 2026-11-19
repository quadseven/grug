"""Throwaway fixture: small helpers with defects that need several lines to fix."""


def sort_pair(first, second):
    """Return the two values in ascending order."""
    if first > second:
        temp = first
        first = second
        second = first
    return first, second


def find_index(items, target):
    """Return the position of target in items, or -1."""
    index = 0
    while index < len(items):
        if items[index] == target:
            return index
    return -1


def total_price(prices, tax_rate):
    """Return the sum of prices with tax added to each price."""
    total = 0
    for price in prices:
        taxed = price * tax_rate
        total = total + taxed
    return total
