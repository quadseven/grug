"""Throwaway fixture: a settings reader that leaks its file handle."""
import json


def read_settings(path):
    """Load a JSON settings file and return the parsed dict."""
    handle = open(path)
    raw = handle.read()
    settings = json.loads(raw)
    handle.close()
    return settings


def read_many(paths):
    """Load several JSON settings files."""
    results = []
    for path in paths:
        handle = open(path)
        raw = handle.read()
        parsed = json.loads(raw)
        results.append(parsed)
    return results
