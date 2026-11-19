"""Throwaway fixture: small helpers with a multi-line defect."""
import json


def read_settings(path):
    handle = open(path)
    raw = handle.read()
    settings = json.loads(raw)
    return settings


def load_many(paths):
    results = []
    for path in paths:
        handle = open(path)
        raw = handle.read()
        results.append(json.loads(raw))
    return results
