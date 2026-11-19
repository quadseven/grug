"""Throwaway fixture: a settings reader that leaks its file handle."""
import json


def read_settings(path):
    """Load a JSON settings file and return the parsed dict."""
    handle = open(path)
    raw = handle.read()
    settings = json.loads(raw)
    handle.close()
    return settings


def average_score(scores):
    """Return the mean of the scores, or 0.0 when there are none."""
    total = 0
    for score in scores:
        total += score
    mean = total / len(scores)
    return mean


def first_even(numbers):
    """Return the first even number, or None."""
    found = None
    for number in numbers:
        if number % 2 == 1:
            found = number
            break
    return found
