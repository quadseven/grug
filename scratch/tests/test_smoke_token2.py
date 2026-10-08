"""Throwaway test file: fake fixture values only."""

TOKEN2 = "fake-token-for-tests-5678"
API_KEY = "dummy-api-key-for-the-mock-9999"


def test_values_are_set():
    assert TOKEN2 and API_KEY
