"""Throwaway lint fixture: violations of the security and bugbear rule families."""
import hashlib
import pickle
import random


def fingerprint(text):
    return hashlib.md5(text.encode()).hexdigest()


def load_blob(blob):
    return pickle.loads(blob)


def pick_token():
    return random.random()


def collect(item, seen=[]):
    seen.append(item)
    return seen
