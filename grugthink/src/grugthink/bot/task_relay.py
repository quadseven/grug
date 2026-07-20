"""Relay implement/fix/review requests from Grug's chat identity to Hermes.

Grug (this personality bot) has no tool-use of its own - implementing code,
fixing bugs, and opening PRs is Hermes' job (infra repo, hermes-discord).
When a mention looks like a work request rather than a chat/verify
statement, Grug posts it into the repo channel Hermes already monitors,
waits, and relays Hermes' own milestone messages back into the original
conversation in character. Grug never touches git or the GitHub API
itself - it is strictly a front door onto an engine that already works.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

import discord

from ..logging_config import get_logger

log = get_logger(__name__)

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

_TASK_PATTERN = re.compile(
    r"\b("
    r"implement|fix|build|refactor|patch|"
    r"write (some |the )?code|"
    r"open (a |the )?(pr|pull request)|"
    r"create (a |the )?(pr|pull request)|"
    r"(review|look at) (this|the|that|a) (pr|pull request|diff|code)|"
    r"code review"
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
    return bool(_TASK_PATTERN.search(clean_content))


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

    requester = original_message.author.display_name or original_message.author.name
    relay_content = f"Grug bring word from {requester}: {clean_content}"

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
    """
    deadline = time.monotonic() + MAX_WAIT_S
    got_any_reply = False
    last_activity = time.monotonic()

    def is_hermes(m: discord.Message) -> bool:
        return m.author.bot and m.author != client.user

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

        got_any_reply = True
        last_activity = time.monotonic()
        await original_message.channel.send(f"{bot_name} hear from Hermes: {reply.content}")

    await original_message.channel.send(
        f"{bot_name} wait long time. Check thread for latest word from Hermes: {thread.jump_url}"
    )
