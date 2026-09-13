"""Throwaway repro for grug#967 - deterministically triggers high-complexity.

Not part of the product; opened as a single-commit PR to observe a real
Elder review comment, then closed unmerged without landing.
"""


def tangled(x: int) -> int:
    if x == 1:
        return 1
    if x == 2:
        return 2
    if x == 3:
        return 3
    if x == 4:
        return 4
    if x == 5:
        return 5
    if x == 6:
        return 6
    if x == 7:
        return 7
    if x == 8:
        return 8
    if x == 9:
        return 9
    if x == 10:
        return 10
    if x == 11:
        return 11
    if x == 12:
        return 12
    if x == 13:
        return 13
    if x == 14:
        return 14
    if x == 15:
        return 15
    if x == 16:
        return 16
    if x == 17:
        return 17
    return 0
