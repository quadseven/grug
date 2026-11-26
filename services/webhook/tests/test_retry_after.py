"""Retry-After as delta-seconds or HTTP-date (grug#1849)."""
from __future__ import annotations

import time
from email.utils import formatdate

import httpx
import opencode_go_free as go_free
import pytest
from retry_after import parse_retry_after

_NOW = 1_800_000_000.0


def _date(offset: float) -> str:
    return formatdate(_NOW + offset, usegmt=True)


def _resp(value: str | None) -> httpx.Response:
    headers = {} if value is None else {"retry-after": value}
    return httpx.Response(429, headers=headers)


def test_http_date_is_seconds_from_now() -> None:
    assert parse_retry_after(_date(120), now=_NOW) == pytest.approx(120)


def test_delta_seconds_still_parse() -> None:
    assert parse_retry_after("42", now=_NOW) == 42


@pytest.mark.parametrize("raw", [None, "", "soon", "nan", "inf", "0", "-5", "Wed, 99 Foo 2026 25:61:00 GMT"])
def test_unusable_values_are_none(raw) -> None:
    assert parse_retry_after(raw, now=_NOW) is None


def test_past_date_is_none() -> None:
    assert parse_retry_after(_date(-60), now=_NOW) is None


def test_date_without_timezone_is_none() -> None:
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 -0000", now=_NOW) is None


def test_catalog_parser_reads_an_http_date() -> None:
    value = formatdate(time.time() + 120, usegmt=True)
    assert go_free.retry_after_seconds(_resp(value)) == pytest.approx(120, abs=5)


def test_catalog_parser_caps_a_far_date() -> None:
    value = formatdate(time.time() + 10 * 86400, usegmt=True)
    assert go_free.retry_after_seconds(_resp(value)) == go_free.MAX_RETRY_AFTER_SECONDS


@pytest.mark.parametrize("value", [formatdate(time.time() - 600, usegmt=True), "garbage", None])
def test_catalog_parser_none_when_unusable(value) -> None:
    assert go_free.retry_after_seconds(_resp(value)) is None
