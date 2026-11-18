"""A fixed, public set of small diffs for measuring a free model's empty answer.

Elder may count a free model's "no findings" as the confirming second opinion
only when an eval shows that model rarely says "no findings" on a diff that
has a real defect. This module is that eval's input: 24 pairs of one small
source file, each as a DEFECT case (the file with one real defect) and as a
CLEAN case (the same file with the defect fixed), so half the cases are
clean and half carry exactly one defect. The seven languages are the ones
Elder reviews. Every sample is invented for this set; none comes from a real
repository, and every value in it is a placeholder.

Each case is padded with documentation hunks to a total diff just past
`review_ladder._DEGENERATE_MIN_DIFF_CHARS` (8,000 characters), the size where a
short empty answer first counts as untrusted. A bare 1,000-character diff is
not the regime the relaxation applies to, so measuring one would flatter the
model. The documentation hunks hold only prose and cannot carry a defect.

Pure data and builders: no network, no model. `corpus_sha` pins the content,
so the checked-in report says which corpus it measured.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

# The diff size the relaxation is measured at: at least the size where a short
# empty answer is flagged, and well under the 10,000-token relaxation limit.
TARGET_MIN_DIFF_CHARS = 8_200
TARGET_MAX_DIFF_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class FreeConfirmCase:
    case_id: str
    language: str
    path: str
    defective: bool
    defect_kind: str  # the kind of defect planted, also named on the clean twin
    source: str  # the file content added by the diff


@dataclass(frozen=True, slots=True)
class _Pair:
    key: str
    language: str
    path: str
    defect_kind: str
    defect: str
    fixed: str


_PAIRS: tuple[_Pair, ...] = (
    _Pair(
        "py-page-slice", "python", "src/pagination.py", "off-by-one",
        '''\
from typing import Sequence, TypeVar

T = TypeVar("T")

MAX_PAGE_SIZE = 100


def page_of(items: Sequence[T], page: int, size: int) -> list[T]:
    """Return the items on a zero-based page, at most `size` of them."""
    if page < 0:
        raise ValueError("page must not be negative")
    if not 0 < size <= MAX_PAGE_SIZE:
        raise ValueError("size out of range")
    start = page * size
    end = start + size + 1
    return list(items[start:end])


def page_count(total: int, size: int) -> int:
    if size <= 0:
        raise ValueError("size must be positive")
    return (total + size - 1) // size
''',
        '''\
from typing import Sequence, TypeVar

T = TypeVar("T")

MAX_PAGE_SIZE = 100


def page_of(items: Sequence[T], page: int, size: int) -> list[T]:
    """Return the items on a zero-based page, at most `size` of them."""
    if page < 0:
        raise ValueError("page must not be negative")
    if not 0 < size <= MAX_PAGE_SIZE:
        raise ValueError("size out of range")
    start = page * size
    end = start + size
    return list(items[start:end])


def page_count(total: int, size: int) -> int:
    if size <= 0:
        raise ValueError("size must be positive")
    return (total + size - 1) // size
''',
    ),
    _Pair(
        "py-header-none", "python", "src/request_info.py", "none-dereference",
        '''\
from typing import Mapping


def client_label(headers: Mapping[str, str]) -> str:
    """A short label for the calling client, taken from its User-Agent."""
    agent = headers.get("User-Agent")
    label = agent.strip().split("/")[0]
    return label[:32] or "unknown"


def is_json_request(headers: Mapping[str, str]) -> bool:
    content_type = headers.get("Content-Type", "")
    return content_type.split(";")[0].strip().lower() == "application/json"
''',
        '''\
from typing import Mapping


def client_label(headers: Mapping[str, str]) -> str:
    """A short label for the calling client, taken from its User-Agent."""
    agent = headers.get("User-Agent", "")
    label = agent.strip().split("/")[0]
    return label[:32] or "unknown"


def is_json_request(headers: Mapping[str, str]) -> bool:
    content_type = headers.get("Content-Type", "")
    return content_type.split(";")[0].strip().lower() == "application/json"
''',
    ),
    _Pair(
        "py-mutable-default", "python", "src/tags.py", "mutable-default-argument",
        '''\
def add_tag(tag: str, tags: list[str] = []) -> list[str]:
    """Return the tag list with `tag` added once, in sorted order."""
    clean = tag.strip().lower()
    if clean and clean not in tags:
        tags.append(clean)
        tags.sort()
    return tags


def merge_tags(first: list[str], second: list[str]) -> list[str]:
    merged: list[str] = []
    for tag in first + second:
        merged = add_tag(tag, merged)
    return merged
''',
        '''\
def add_tag(tag: str, tags: list[str] | None = None) -> list[str]:
    """Return the tag list with `tag` added once, in sorted order."""
    tags = [] if tags is None else tags
    clean = tag.strip().lower()
    if clean and clean not in tags:
        tags.append(clean)
        tags.sort()
    return tags


def merge_tags(first: list[str], second: list[str]) -> list[str]:
    merged: list[str] = []
    for tag in first + second:
        merged = add_tag(tag, merged)
    return merged
''',
    ),
    _Pair(
        "py-sql-format", "python", "src/accounts_repo.py", "sql-injection",
        '''\
import sqlite3


def find_account(conn: sqlite3.Connection, email: str) -> tuple | None:
    """Look an account up by the email a user typed into the login form."""
    query = f"SELECT id, email, active FROM accounts WHERE email = '{email}'"
    return conn.execute(query).fetchone()


def deactivate(conn: sqlite3.Connection, account_id: int) -> None:
    conn.execute("UPDATE accounts SET active = 0 WHERE id = ?", (account_id,))
    conn.commit()
''',
        '''\
import sqlite3


def find_account(conn: sqlite3.Connection, email: str) -> tuple | None:
    """Look an account up by the email a user typed into the login form."""
    query = "SELECT id, email, active FROM accounts WHERE email = ?"
    return conn.execute(query, (email,)).fetchone()


def deactivate(conn: sqlite3.Connection, account_id: int) -> None:
    conn.execute("UPDATE accounts SET active = 0 WHERE id = ?", (account_id,))
    conn.commit()
''',
    ),
    _Pair(
        "py-lock-early-return", "python", "src/counter_store.py", "lock-not-released",
        '''\
import threading


class CounterStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def bump(self, name: str, limit: int) -> int | None:
        """Add one to a named counter; refuse once it reaches `limit`."""
        self._lock.acquire()
        current = self._counts.get(name, 0)
        if current >= limit:
            return None
        self._counts[name] = current + 1
        self._lock.release()
        return current + 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)
''',
        '''\
import threading


class CounterStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def bump(self, name: str, limit: int) -> int | None:
        """Add one to a named counter; refuse once it reaches `limit`."""
        with self._lock:
            current = self._counts.get(name, 0)
            if current >= limit:
                return None
            self._counts[name] = current + 1
            return current + 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)
''',
    ),
    _Pair(
        "py-swallowed-charge", "python", "src/billing.py", "swallowed-error",
        '''\
import logging

log = logging.getLogger("billing")


class ChargeError(Exception):
    pass


def charge_card(gateway, customer_id: str, cents: int) -> None:
    if cents <= 0:
        raise ValueError("amount must be positive")
    gateway.charge(customer_id, cents)


def settle(gateway, customer_id: str, cents: int) -> bool:
    """Charge a customer. True means the money was taken."""
    try:
        charge_card(gateway, customer_id, cents)
    except Exception:
        pass
    log.info("settled", extra={"customer": customer_id, "cents": cents})
    return True
''',
        '''\
import logging

log = logging.getLogger("billing")


class ChargeError(Exception):
    pass


def charge_card(gateway, customer_id: str, cents: int) -> None:
    if cents <= 0:
        raise ValueError("amount must be positive")
    gateway.charge(customer_id, cents)


def settle(gateway, customer_id: str, cents: int) -> bool:
    """Charge a customer. True means the money was taken."""
    try:
        charge_card(gateway, customer_id, cents)
    except ChargeError:
        log.warning("charge_failed", extra={"customer": customer_id})
        return False
    log.info("settled", extra={"customer": customer_id, "cents": cents})
    return True
''',
    ),
    _Pair(
        "py-expiry-inverted", "python", "src/tokens.py", "inverted-condition",
        '''\
import time


class TokenExpired(Exception):
    pass


def check_token(claims: dict, now: float | None = None) -> dict:
    """Return the claims when the token is still valid, else raise."""
    current = time.time() if now is None else now
    expires_at = float(claims["exp"])
    if expires_at > current:
        raise TokenExpired(f"token expired at {expires_at}")
    if "sub" not in claims:
        raise ValueError("token has no subject")
    return claims
''',
        '''\
import time


class TokenExpired(Exception):
    pass


def check_token(claims: dict, now: float | None = None) -> dict:
    """Return the claims when the token is still valid, else raise."""
    current = time.time() if now is None else now
    expires_at = float(claims["exp"])
    if expires_at <= current:
        raise TokenExpired(f"token expired at {expires_at}")
    if "sub" not in claims:
        raise ValueError("token has no subject")
    return claims
''',
    ),
    _Pair(
        "py-naive-now", "python", "src/age.py", "naive-and-aware-datetime",
        '''\
from datetime import datetime, timezone


def age_in_days(created_at: datetime) -> int:
    """Whole days since `created_at`, which is always stored in UTC."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return (datetime.now() - created_at).days


def is_stale(created_at: datetime, limit_days: int = 30) -> bool:
    return age_in_days(created_at) > limit_days
''',
        '''\
from datetime import datetime, timezone


def age_in_days(created_at: datetime) -> int:
    """Whole days since `created_at`, which is always stored in UTC."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created_at).days


def is_stale(created_at: datetime, limit_days: int = 30) -> bool:
    return age_in_days(created_at) > limit_days
''',
    ),
    _Pair(
        "ts-missing-await", "typescript", "web/src/userName.ts", "missing-await",
        '''\
interface User {
  id: string;
  name: string;
}

interface UserRepo {
  find(id: string): Promise<User | undefined>;
}

export async function displayName(repo: UserRepo, id: string): Promise<string> {
  const user = repo.find(id);
  if (!user) {
    return "unknown";
  }
  return user.name.trim() || "unknown";
}
''',
        '''\
interface User {
  id: string;
  name: string;
}

interface UserRepo {
  find(id: string): Promise<User | undefined>;
}

export async function displayName(repo: UserRepo, id: string): Promise<string> {
  const user = await repo.find(id);
  if (!user) {
    return "unknown";
  }
  return user.name.trim() || "unknown";
}
''',
    ),
    _Pair(
        "ts-default-sort", "typescript", "web/src/leaderboard.ts", "lexicographic-number-sort",
        '''\
export interface Entry {
  player: string;
  score: number;
}

export function topScores(entries: Entry[], count: number): number[] {
  const scores = entries.map((entry) => entry.score);
  scores.sort();
  return scores.reverse().slice(0, count);
}

export function average(entries: Entry[]): number {
  if (entries.length === 0) {
    return 0;
  }
  return entries.reduce((sum, entry) => sum + entry.score, 0) / entries.length;
}
''',
        '''\
export interface Entry {
  player: string;
  score: number;
}

export function topScores(entries: Entry[], count: number): number[] {
  const scores = entries.map((entry) => entry.score);
  scores.sort((a, b) => a - b);
  return scores.reverse().slice(0, count);
}

export function average(entries: Entry[]): number {
  if (entries.length === 0) {
    return 0;
  }
  return entries.reduce((sum, entry) => sum + entry.score, 0) / entries.length;
}
''',
    ),
    _Pair(
        "ts-inner-html", "typescript", "web/src/comments.ts", "cross-site-scripting",
        '''\
export interface Comment {
  author: string;
  body: string;
}

export function renderComment(list: HTMLElement, comment: Comment): void {
  const item = document.createElement("li");
  const author = document.createElement("strong");
  author.textContent = comment.author;
  const body = document.createElement("p");
  body.innerHTML = comment.body;
  item.append(author, body);
  list.appendChild(item);
}
''',
        '''\
export interface Comment {
  author: string;
  body: string;
}

export function renderComment(list: HTMLElement, comment: Comment): void {
  const item = document.createElement("li");
  const author = document.createElement("strong");
  author.textContent = comment.author;
  const body = document.createElement("p");
  body.textContent = comment.body;
  item.append(author, body);
  list.appendChild(item);
}
''',
    ),
    _Pair(
        "ts-var-closure", "typescript", "web/src/reminders.ts", "loop-variable-capture",
        '''\
export function scheduleReminders(
  names: string[],
  notify: (name: string) => void,
  delayMs: number,
): void {
  for (var i = 0; i < names.length; i++) {
    setTimeout(() => notify(names[i]), delayMs * (i + 1));
  }
}

export function cancelAll(handles: number[]): void {
  for (const handle of handles) {
    clearTimeout(handle);
  }
}
''',
        '''\
export function scheduleReminders(
  names: string[],
  notify: (name: string) => void,
  delayMs: number,
): void {
  for (let i = 0; i < names.length; i++) {
    setTimeout(() => notify(names[i]), delayMs * (i + 1));
  }
}

export function cancelAll(handles: number[]): void {
  for (const handle of handles) {
    clearTimeout(handle);
  }
}
''',
    ),
    _Pair(
        "sh-rm-unset-var", "shell", "scripts/clean_cache.sh", "unguarded-rm",
        '''\
#!/usr/bin/env bash
# Remove one named build cache from the cache directory.
set -u

CACHE_DIR="${CACHE_DIR:-}"
NAME="${1:-}"

echo "clearing cache ${NAME}"
rm -rf $CACHE_DIR/$NAME
echo "done"
''',
        '''\
#!/usr/bin/env bash
# Remove one named build cache from the cache directory.
set -eu

CACHE_DIR="${CACHE_DIR:-}"
NAME="${1:-}"

echo "clearing cache ${NAME}"
rm -rf "${CACHE_DIR:?CACHE_DIR is not set}/${NAME:?name is required}"
echo "done"
''',
    ),
    _Pair(
        "sh-unquoted-test", "shell", "scripts/count_logs.sh", "unquoted-variable",
        '''\
#!/usr/bin/env bash
# Warn when a log directory holds too many files.
set -eu

LOG_DIR="${1:?log directory is required}"
LIMIT=500

count=$(find "$LOG_DIR" -type f -name '*.log' | wc -l)
if [ $count -gt $LIMIT ]; then
  echo "too many log files in $LOG_DIR: $count" >&2
  exit 1
fi
echo "ok: $count log files"
''',
        '''\
#!/usr/bin/env bash
# Warn when a log directory holds too many files.
set -eu

LOG_DIR="${1:?log directory is required}"
LIMIT=500

count=$(find "$LOG_DIR" -type f -name '*.log' | wc -l | tr -d ' ')
if [ "$count" -gt "$LIMIT" ]; then
  echo "too many log files in $LOG_DIR: $count" >&2
  exit 1
fi
echo "ok: $count log files"
''',
    ),
    _Pair(
        "sh-pipe-masks-failure", "shell", "scripts/migrate.sh", "pipeline-hides-failure",
        '''\
#!/usr/bin/env bash
# Run the schema migrations and keep a copy of the output.
set -eu

LOG="${LOG:-migrate.log}"

./run_migrations.sh --env "${1:?environment is required}" | tee "$LOG"
echo "migrations finished"
''',
        '''\
#!/usr/bin/env bash
# Run the schema migrations and keep a copy of the output.
set -euo pipefail

LOG="${LOG:-migrate.log}"

./run_migrations.sh --env "${1:?environment is required}" | tee "$LOG"
echo "migrations finished"
''',
    ),
    _Pair(
        "sql-no-where", "sql", "migrations/0042_retire_stale_accounts.sql", "missing-where",
        '''\
-- Retire accounts that have not signed in for 90 days.
BEGIN;

ALTER TABLE accounts ADD COLUMN retired_at timestamptz;

UPDATE accounts
SET retired_at = now(),
    active = false;

CREATE INDEX accounts_retired_at_idx ON accounts (retired_at);

COMMIT;
''',
        '''\
-- Retire accounts that have not signed in for 90 days.
BEGIN;

ALTER TABLE accounts ADD COLUMN retired_at timestamptz;

UPDATE accounts
SET retired_at = now(),
    active = false
WHERE last_sign_in_at < now() - interval '90 days';

CREATE INDEX accounts_retired_at_idx ON accounts (retired_at);

COMMIT;
''',
    ),
    _Pair(
        "sql-expiry-inverted", "sql", "migrations/0043_purge_sessions.sql", "inverted-comparison",
        '''\
-- Delete sessions that have already expired.
BEGIN;

DELETE FROM sessions
WHERE expires_at > now();

VACUUM ANALYZE sessions;

COMMIT;
''',
        '''\
-- Delete sessions that have already expired.
BEGIN;

DELETE FROM sessions
WHERE expires_at < now();

VACUUM ANALYZE sessions;

COMMIT;
''',
    ),
    _Pair(
        "cpp-dangling-ref", "cpp", "src/NameFormatter.cpp", "dangling-reference",
        '''\
#include <string>

namespace formatter {

const std::string& displayName(const std::string& first, const std::string& last)
{
    std::string full = first + " " + last;
    if (full.size() > 40)
    {
        full.resize(40);
    }
    return full;
}

bool isBlank(const std::string& value)
{
    return value.find_first_not_of(" \\t") == std::string::npos;
}

}  // namespace formatter
''',
        '''\
#include <string>

namespace formatter {

std::string displayName(const std::string& first, const std::string& last)
{
    std::string full = first + " " + last;
    if (full.size() > 40)
    {
        full.resize(40);
    }
    return full;
}

bool isBlank(const std::string& value)
{
    return value.find_first_not_of(" \\t") == std::string::npos;
}

}  // namespace formatter
''',
    ),
    _Pair(
        "cpp-erase-in-loop", "cpp", "src/ExpiryList.cpp", "iterator-invalidation",
        '''\
#include <vector>

struct Timer
{
    int id;
    int remainingMs;
};

void tick(std::vector<Timer>& timers, int elapsedMs)
{
    for (const Timer& timer : timers)
    {
        if (timer.remainingMs <= elapsedMs)
        {
            timers.erase(timers.begin() + timer.id);
        }
    }
    for (Timer& timer : timers)
    {
        timer.remainingMs -= elapsedMs;
    }
}
''',
        '''\
#include <algorithm>
#include <vector>

struct Timer
{
    int id;
    int remainingMs;
};

void tick(std::vector<Timer>& timers, int elapsedMs)
{
    timers.erase(
        std::remove_if(timers.begin(), timers.end(),
                       [elapsedMs](const Timer& timer) { return timer.remainingMs <= elapsedMs; }),
        timers.end());
    for (Timer& timer : timers)
    {
        timer.remainingMs -= elapsedMs;
    }
}
''',
    ),
    _Pair(
        "cpp-int-division", "cpp", "src/HitRate.cpp", "integer-division",
        '''\
#include <cstdint>

double hitRatePercent(std::uint32_t hits, std::uint32_t total)
{
    if (total == 0)
    {
        return 0.0;
    }
    double ratio = hits / total;
    return ratio * 100.0;
}

bool isHealthy(std::uint32_t hits, std::uint32_t total)
{
    return hitRatePercent(hits, total) >= 80.0;
}
''',
        '''\
#include <cstdint>

double hitRatePercent(std::uint32_t hits, std::uint32_t total)
{
    if (total == 0)
    {
        return 0.0;
    }
    double ratio = static_cast<double>(hits) / static_cast<double>(total);
    return ratio * 100.0;
}

bool isHealthy(std::uint32_t hits, std::uint32_t total)
{
    return hitRatePercent(hits, total) >= 80.0;
}
''',
    ),
    _Pair(
        "yaml-probe-port", "yaml", "deploy/example-deployment.yaml", "wrong-port",
        '''\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: example-api
spec:
  replicas: 2
  selector:
    matchLabels:
      app: example-api
  template:
    metadata:
      labels:
        app: example-api
    spec:
      containers:
        - name: api
          image: registry.example.com/example-api:1.4.0
          ports:
            - containerPort: 8080
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8081
            periodSeconds: 5
''',
        '''\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: example-api
spec:
  replicas: 2
  selector:
    matchLabels:
      app: example-api
  template:
    metadata:
      labels:
        app: example-api
    spec:
      containers:
        - name: api
          image: registry.example.com/example-api:1.4.0
          ports:
            - containerPort: 8080
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8080
            periodSeconds: 5
''',
    ),
    _Pair(
        "yaml-title-injection", "yaml", ".github/workflows/label-title.yml", "workflow-script-injection",
        '''\
name: label-title
on:
  pull_request:
    types: [opened, edited]

permissions:
  contents: read

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - name: Echo the title
        run: |
          echo "Title is: ${{ github.event.pull_request.title }}"
          test -n "${{ github.event.pull_request.title }}"
''',
        '''\
name: label-title
on:
  pull_request:
    types: [opened, edited]

permissions:
  contents: read

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - name: Echo the title
        env:
          TITLE: ${{ github.event.pull_request.title }}
        run: |
          echo "Title is: ${TITLE}"
          test -n "${TITLE}"
''',
    ),
    _Pair(
        "go-nil-map", "go", "internal/tally/tally.go", "nil-map-write",
        '''\
package tally

// Counter counts how many times each word was seen.
type Counter struct {
	counts map[string]int
}

func NewCounter() *Counter {
	return &Counter{}
}

func (c *Counter) Add(word string) {
	c.counts[word]++
}

func (c *Counter) Count(word string) int {
	return c.counts[word]
}
''',
        '''\
package tally

// Counter counts how many times each word was seen.
type Counter struct {
	counts map[string]int
}

func NewCounter() *Counter {
	return &Counter{counts: make(map[string]int)}
}

func (c *Counter) Add(word string) {
	c.counts[word]++
}

func (c *Counter) Count(word string) int {
	return c.counts[word]
}
''',
    ),
    _Pair(
        "go-racy-counter", "go", "internal/hits/hits.go", "data-race",
        '''\
package hits

import "sync"

// Total runs every job on its own goroutine and counts the ones that succeed.
func Total(jobs []func() bool) int {
	var wg sync.WaitGroup
	succeeded := 0
	for _, job := range jobs {
		wg.Add(1)
		go func(run func() bool) {
			defer wg.Done()
			if run() {
				succeeded++
			}
		}(job)
	}
	wg.Wait()
	return succeeded
}
''',
        '''\
package hits

import (
	"sync"
	"sync/atomic"
)

// Total runs every job on its own goroutine and counts the ones that succeed.
func Total(jobs []func() bool) int {
	var wg sync.WaitGroup
	var succeeded atomic.Int64
	for _, job := range jobs {
		wg.Add(1)
		go func(run func() bool) {
			defer wg.Done()
			if run() {
				succeeded.Add(1)
			}
		}(job)
	}
	wg.Wait()
	return int(succeeded.Load())
}
''',
    ),
)

_PROSE: tuple[str, ...] = (
    "The service reads its settings once at start up and keeps them for the life of the process.",
    "Operators who change a setting restart the service so every replica sees the same value.",
    "Each section below names the owner of the setting and the place its default is documented.",
    "A release note lists every change that a reader of the interface could notice.",
    "Changes that only touch internal names are left out of the release notes on purpose.",
    "When two settings interact, the section for the later one links back to the earlier one.",
    "The runbook describes the order in which the checks run and what each check proves.",
    "A check that fails twice in a row is reported once, with the count of failures attached.",
    "The glossary defines the words that readers outside the team are likely to misread.",
    "Examples use placeholder names and the documentation address ranges, never real ones.",
    "The guide shows the shortest path first and leaves the rarely used options for the end.",
    "Every diagram has a text description below it so the page reads the same without images.",
    "Timeouts are given in seconds unless the heading for the section says otherwise.",
    "The table of limits is regenerated from the source on each release and is never edited by hand.",
    "Contributors add a line to the changelog in the same change that alters the behaviour.",
    "The troubleshooting list starts with the symptoms a reader sees and ends with the cause.",
    "A reader who only needs the overview can stop after the first paragraph of each section.",
    "Terms in code font are literal; terms in plain text are names for ideas.",
    "The migration notes say which version to upgrade from and what to do before upgrading.",
    "Where a default changed, the old default stays listed for two releases.",
    "Reviewers read the documentation diff first, because it states what the change is meant to do.",
    "The page for each component keeps a short list of the questions it is most often asked.",
    "Dates in this guide use the year, month and day order so they sort the way they read.",
    "A section that grows past a screen is split, and the parent section links to its children.",
)


def _filler(seed: int, want_chars: int) -> str:
    """A documentation page of `want_chars` characters or a little more, from
    the prose pool: deterministic for a seed, different for each seed."""
    lines = [f"# Notes for release {seed + 1}", ""]
    size = len(lines[0]) + 1
    n = seed
    section = 1
    while size < want_chars:
        lines.append(f"## Section {section}")
        lines.append("")
        size += len(lines[-2]) + 2
        for _ in range(3):
            sentence = _PROSE[(n * 7 + section * 3) % len(_PROSE)]
            n += 1
            lines.append(sentence)
            size += len(sentence) + 1
        lines.append("")
        size += 1
        section += 1
    return "\n".join(lines) + "\n"


def _added_file_diff(path: str, content: str) -> str:
    """A unified diff that adds one file."""
    lines = content.splitlines()
    body = "\n".join("+" + line for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}\n"
    )


def build_cases() -> tuple[FreeConfirmCase, ...]:
    """Every defect case then every clean case, in pair order."""
    defects = tuple(
        FreeConfirmCase(
            case_id=f"{p.key}-defect", language=p.language, path=p.path,
            defective=True, defect_kind=p.defect_kind, source=p.defect,
        )
        for p in _PAIRS
    )
    clean = tuple(
        FreeConfirmCase(
            case_id=f"{p.key}-clean", language=p.language, path=p.path,
            defective=False, defect_kind=p.defect_kind, source=p.fixed,
        )
        for p in _PAIRS
    )
    return defects + clean


def diff_for(case: FreeConfirmCase) -> str:
    """The case as one diff: its source file plus a documentation page sized so
    the whole diff lands between TARGET_MIN_DIFF_CHARS and TARGET_MAX_DIFF_CHARS.
    A defect case and its clean twin carry the same documentation page."""
    seed = sum(ord(c) for c in case.case_id.rsplit("-", 1)[0])
    code = _added_file_diff(case.path, case.source)
    doc_path = f"docs/notes-{seed % 997}.md"
    want = TARGET_MIN_DIFF_CHARS - len(code) + 400
    doc = _added_file_diff(doc_path, _filler(seed, max(want, 1200)))
    # git orders a diff by path; the documentation page sorts before src/ and
    # web/ and after .github/, and the review sees them in this order.
    parts = sorted([(case.path, code), (doc_path, doc)])
    return "".join(text for _, text in parts)


def corpus_sha() -> str:
    """A short digest of everything the eval feeds a model."""
    digest = hashlib.sha256()
    for case in build_cases():
        digest.update(
            json.dumps([case.case_id, case.defective, diff_for(case)]).encode("utf-8")
        )
    return digest.hexdigest()[:16]
