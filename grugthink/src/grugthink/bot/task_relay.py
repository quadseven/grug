"""Relay implement/fix/review requests from Grug's chat identity to Hermes.

Grug (this personality bot) has no tool-use of its own - implementing code,
fixing bugs, and opening PRs is Hermes' job (infra repo, hermes-discord).
When a mention looks like a work request rather than a chat/verify
statement, Grug posts it into the repo channel Hermes already monitors,
waits, and relays Hermes' own milestone messages back into the original
conversation in character. Grug never touches git or the GitHub API
itself - it is strictly a front door onto an engine that already works.

SECURITY MODEL - this relay sits in front of an agent (Hermes) with real
GH_TOKEN write access, so it is a confused-deputy risk by construction:
anyone who can get a message to Grug could otherwise get Hermes to act on
their behalf with Grug's own channel-send privilege standing in for them.
Three things close that gap:

- ``is_authorized`` gates every relay on ``TASK_RELAY_ALLOWED_USER_IDS``,
  fail-closed (unset/empty = nobody authorized, not "everyone"). Only
  Discord users Evan explicitly lists can trigger a relay at all.
- ``_sanitize_for_relay`` strips @everyone/@here/role-mention syntax out
  of the relayed request before it reaches a channel Grug can post
  broadly in, so an authorized-but-malicious (or just careless) request
  can't mass-ping the server through Grug's own send permission.
- ``_watch_and_relay`` only treats a reply as "from Hermes" if it matches
  ``HERMES_BOT_USER_ID`` specifically, fail-safe (unset = don't
  relay-watch at all). Matching "any bot" would let any other bot in the
  guild impersonate Hermes' reply and have Grug faithfully repeat it as
  if it were real.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Literal, Optional

import discord

from ..logging_config import get_logger

log = get_logger(__name__)

# Discord user IDs allowed to trigger a Hermes relay, comma-separated.
# Fail-closed: unset or empty means nobody is authorized. This is a new
# capability that indirectly grants repo-write access via Hermes, so the
# safe default is "off until Evan configures it", not "on for anyone who
# can talk to Grug".
_ALLOWED_USERS_ENV_VAR = "TASK_RELAY_ALLOWED_USER_IDS"

# The specific Discord user ID of Hermes' own bot account. Required for
# _watch_and_relay to trust a reply as genuinely from Hermes - see the
# module docstring's SECURITY MODEL section.
_HERMES_USER_ID_ENV_VAR = "HERMES_BOT_USER_ID"

# Mirrors infra's production/oke/manifests/hermes-discord/configmap.yaml
# discord.free_response_channels (guild 781626163591249930, category
# "Github"). A small duplicated map rather than a shared config source -
# unifying the two is a later slice, not blocking this one. If this list
# drifts from the configmap, relays silently 404 (handled below) rather
# than reaching the wrong channel.
REPO_CHANNELS: dict[str, int] = {
    "grug": 1524605086263808113,
    "infra": 1524605143210000464,
    "macchina": 1524605185887043584,
    "digital-ledger": 1524605284822155334,
    "claude-stuff": 1524605321790623905,
    "infra-public": 1524607466866868307,
    "grugthink": 1524607693246169230,
    "anna-personal": 1524607738997506099,
    "macchina-ios-certs": 1524607771558023178,
    "holdfast": 1524607875257860219,
    "gemini-plugin-cc": 1524607911450513468,
    "vroom-vroom": 1524608073698902146,
    "conducted": 1524608164991865093,
    "brother-claudius": 1524608246688776242,
    "meow-now": 1524608364485677256,
    "aws-solutions-architect-study": 1524608438120743043,
}

TaskKind = Literal["change", "review"]


@dataclass(frozen=True)
class TaskRequest:
    """A user request that Grug can hand to the coding agent."""

    kind: TaskKind
    content: str


_CHANGE_PATTERN = re.compile(
    r"\b("
    r"implement|fix|build|refactor|patch|"
    r"write (some |the )?code|"
    r"open (a |the )?(pr|pull request)|"
    r"create (a |the )?(pr|pull request)"
    r")\b",
    re.IGNORECASE,
)

_REVIEW_PATTERN = re.compile(
    r"\b("
    r"(review|look at|audit|inspect) (this|the|that|a|my)?\s*(pr|pull request|diff|code|changes)|"
    r"(code|pr|pull request|diff|peer) review|"
    r"review (pr|pull request)\s*#?\d+"
    r")\b",
    re.IGNORECASE,
)

# How often to send a "still waiting" note before Hermes has said anything.
HEARTBEAT_INTERVAL_S = 5 * 60
# How long Hermes must stay quiet, after saying something, before Grug
# assumes the task is finished and stops watching.
QUIET_DONE_S = 3 * 60
# Absolute ceiling regardless of the above.
MAX_WAIT_S = 20 * 60


def looks_like_task(clean_content: str) -> bool:
    """Keyword pre-filter: "this is implement/fix/review work", not chat.

    Deliberately deterministic rather than LLM-classified for v1 - it's
    testable, adds no latency or spark-gateway load to ordinary chat
    messages, and the keyword set can be tuned from real misses once this
    is live. An LLM-based fallback for ambiguous phrasing is a reasonable
    follow-up once there's real usage data to tune it against.
    """
    return classify_task(clean_content) is not None


def classify_task(clean_content: str) -> Optional[TaskRequest]:
    """Classify a work request without asking an LLM.

    Review wins when a sentence contains both change and review language. This
    keeps "review the PR and fix nothing" on the read-only path.
    """
    if _REVIEW_PATTERN.search(clean_content):
        return TaskRequest(kind="review", content=clean_content)
    if _CHANGE_PATTERN.search(clean_content):
        return TaskRequest(kind="change", content=clean_content)
    return None


def format_relay_request(task: TaskRequest, requester: str) -> str:
    """Build the transport-neutral instruction sent to the coding agent."""
    request = _sanitize_for_relay(task.content)
    if task.kind == "review":
        return (
            f"Grug bring review request from {requester}: {request}\n\n"
            "Treat this as read-only review work. Inspect the real diff and repository context. "
            "Use independent peer-review or audit tools when available, consolidate duplicate findings, "
            "and report actionable findings with file and line evidence. Do not edit code or open a PR "
            "unless the requester explicitly asks for fixes."
        )
    return f"Grug bring coding request from {requester}: {request}"


def _parse_id_list(raw: Optional[str]) -> frozenset[int]:
    if not raw:
        return frozenset()
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return frozenset(ids)


def is_authorized(user_id: int) -> bool:
    """Fail-closed check: only IDs listed in TASK_RELAY_ALLOWED_USER_IDS
    may trigger a relay to Hermes. See the module docstring."""
    allowed = _parse_id_list(os.environ.get(_ALLOWED_USERS_ENV_VAR))
    return user_id in allowed


def _get_hermes_user_id() -> Optional[int]:
    raw = os.environ.get(_HERMES_USER_ID_ENV_VAR)
    return int(raw) if raw and raw.isdigit() else None


_MENTION_PATTERN = re.compile(r"@everyone|@here|<@&?!?\d+>")


def _sanitize_for_relay(text: str) -> str:
    """Neutralize @everyone/@here/role/user mention syntax before it's
    embedded in a message Grug sends into a channel it can broadly post
    in - a zero-width space breaks Discord's mention parser without
    changing how the text reads. Same technique grug's own Teller
    persona uses for the same reason (render.py)."""
    return _MENTION_PATTERN.sub(lambda m: m.group(0)[0] + "​" + m.group(0)[1:], text)


def resolve_repo(clean_content: str) -> Optional[str]:
    """Find a known repo name mentioned in the request.

    Longest name first so e.g. "digital-ledger" isn't shadowed by a
    shorter accidental substring match earlier in the dict.
    """
    lowered = clean_content.lower()
    for repo in sorted(REPO_CHANNELS, key=len, reverse=True):
        if repo in lowered:
            return repo
    return None


async def relay_to_hermes(
    client: discord.Client,
    original_message: discord.Message,
    bot_name: str,
    clean_content: str,
) -> None:
    """Post a task request into the resolved repo's Hermes channel, then
    watch for and relay Hermes' reply. Intended to be launched via
    ``asyncio.create_task`` from ``on_message`` - it polls for up to
    ``MAX_WAIT_S`` and must not block the caller.
    """
    if not is_authorized(original_message.author.id):
        log.warning(
            "task_relay: relay attempt from unauthorized user",
            extra={"user_id": original_message.author.id, "user_name": str(original_message.author)},
        )
        await original_message.channel.send(f"{bot_name} no know you well enough for that. Ask Evan to add you first.")
        return

    repo = resolve_repo(clean_content)
    if repo is None:
        await original_message.channel.send(
            f"{bot_name} no know which cave you mean. Say repo name - one of: {', '.join(sorted(REPO_CHANNELS))}."
        )
        return

    channel = client.get_channel(REPO_CHANNELS[repo])
    if channel is None:
        log.warning(
            "task_relay: cannot see Hermes repo channel - likely missing Discord permission grant",
            extra={"repo": repo, "channel_id": REPO_CHANNELS[repo]},
        )
        await original_message.channel.send(
            f"{bot_name} try reach Hermes cave for {repo} but door locked. "
            f"Tell Evan: Grug need Discord permission on that channel."
        )
        return

    task = classify_task(clean_content)
    if task is None:
        log.warning("task_relay: relay called for non-task content")
        await original_message.channel.send(f"{bot_name} no find coding work in that request.")
        return

    requester = original_message.author.display_name or original_message.author.name
    relay_content = format_relay_request(task, requester)

    try:
        relay_message = await channel.send(relay_content)
        thread = await relay_message.create_thread(name=f"grug-relay-{original_message.id}")
    except discord.HTTPException:
        log.exception("task_relay: failed to relay task into Hermes channel", extra={"repo": repo})
        await original_message.channel.send(f"{bot_name} try reach Hermes but stumble. Try again little later.")
        return

    await original_message.channel.send(f"{bot_name} go ask Hermes about {repo}. {bot_name} wait.")

    await _watch_and_relay(client, thread, original_message, bot_name)


async def _watch_and_relay(
    client: discord.Client,
    thread: discord.Thread,
    original_message: discord.Message,
    bot_name: str,
) -> None:
    """Relay each message Hermes posts in the relay thread back to the
    original conversation, in character, until Hermes goes quiet for
    ``QUIET_DONE_S`` after at least one reply, or ``MAX_WAIT_S`` elapses.

    Fail-safe: if HERMES_BOT_USER_ID isn't configured, this does not
    watch at all - relaying an unverified bot's message as if it came
    from Hermes would let anything else in the guild impersonate Hermes'
    reply. Grug still posts the ack in relay_to_hermes either way; the
    human can check the relay thread directly in that case.
    """
    hermes_id = _get_hermes_user_id()
    if hermes_id is None:
        log.warning("task_relay: HERMES_BOT_USER_ID not configured - not watching for a reply")
        await original_message.channel.send(
            f"{bot_name} no know which grug is Hermes yet - check the thread yourself: {thread.jump_url}"
        )
        return

    deadline = time.monotonic() + MAX_WAIT_S
    got_any_reply = False
    last_activity = time.monotonic()

    def is_hermes(m: discord.Message) -> bool:
        return m.author.id == hermes_id

    while time.monotonic() < deadline:
        quiet_budget = QUIET_DONE_S if got_any_reply else HEARTBEAT_INTERVAL_S
        wait_for_s = max(1.0, min(quiet_budget - (time.monotonic() - last_activity), quiet_budget))

        try:
            reply = await client.wait_for(
                "message",
                check=lambda m: m.channel.id == thread.id and is_hermes(m),
                timeout=wait_for_s,
            )
        except asyncio.TimeoutError:
            if got_any_reply:
                # Quiet long enough after at least one reply - call it done.
                await original_message.channel.send(f"{bot_name} done listen. Hermes finish - see above.")
                return
            await original_message.channel.send(f"{bot_name} still wait on Hermes...")
            last_activity = time.monotonic()
            continue
        except Exception:
            # This runs as a detached asyncio.create_task with nothing else
            # awaiting it - an unhandled exception here (a dropped gateway
            # connection, discord.py internals, anything unexpected) would
            # otherwise vanish silently, leaving the earlier "go ask Hermes,
            # wait" ack as a promise that's never fulfilled or explained.
            log.exception("task_relay: unexpected error while watching for Hermes' reply")
            await original_message.channel.send(
                f"{bot_name} lose Hermes' trail - something break. Check the thread yourself: {thread.jump_url}"
            )
            return

        got_any_reply = True
        last_activity = time.monotonic()
        await original_message.channel.send(f"{bot_name} hear from Hermes: {reply.content}")

    await original_message.channel.send(
        f"{bot_name} wait long time. Check thread for latest word from Hermes: {thread.jump_url}"
    )
