"""Lifecycle defects from #1685.

1. A Discord client task that dies after the startup window must flip the bot
   status to error instead of leaving it reported as running.
2. Shutdown signals must schedule shutdown() on the running loop.
"""

import asyncio
import os
import signal
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("DISCORD_TOKEN", "fake_token")

from src.grugthink.bot_manager import lifecycle  # noqa: E402


def _instance(status="running"):
    return SimpleNamespace(runtime_status=status, logger=MagicMock(), task=None)


async def _settle():
    # Let the task finish and its done-callbacks run.
    for _ in range(5):
        await asyncio.sleep(0)


def test_task_failure_after_startup_window_sets_error():
    instance = _instance()

    async def scenario():

        async def start():
            await asyncio.sleep(0)
            raise RuntimeError("gateway died")

        instance.task = asyncio.create_task(start())
        lifecycle.watch_bot_task(instance)
        await _settle()

    asyncio.run(scenario())

    assert instance.runtime_status == "error"
    instance.logger.error.assert_called_once()


def test_cancelled_task_does_not_set_error():
    instance = _instance()

    async def scenario():

        async def start():
            await asyncio.sleep(60)

        instance.task = asyncio.create_task(start())
        lifecycle.watch_bot_task(instance)
        await asyncio.sleep(0)
        instance.task.cancel()
        await _settle()

    asyncio.run(scenario())

    assert instance.runtime_status == "running"
    instance.logger.error.assert_not_called()


def test_stale_task_does_not_clobber_a_restarted_bot():
    instance = _instance()

    async def scenario():

        async def start():
            await asyncio.sleep(0)
            raise RuntimeError("old task died")

        old = asyncio.create_task(start())
        instance.task = old
        lifecycle.watch_bot_task(instance)
        instance.task = asyncio.create_task(asyncio.sleep(60))
        await _settle()

    asyncio.run(scenario())

    assert instance.runtime_status == "running"


def test_signal_handler_runs_shutdown_on_the_loop(monkeypatch):
    from src.grugthink import main as main_module

    container = main_module.GrugThinkContainer.__new__(main_module.GrugThinkContainer)
    container.running = True
    ran = []

    async def fake_shutdown():
        ran.append(True)

    container.shutdown = fake_shutdown
    container._shutdown_task = None
    registered = {}

    async def scenario():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, cb, *a: registered.setdefault(sig, (cb, a)))
        container._install_signal_handlers()
        assert set(registered) == {signal.SIGINT, signal.SIGTERM}
        cb, args = registered[signal.SIGTERM]
        cb(*args)
        await container._shutdown_task

    asyncio.run(scenario())
    assert ran == [True]


def test_client_start_failing_after_startup_window_sets_error(monkeypatch, tmp_path):
    """Drive the real start path: client.start dies only after _start_with_settings returned."""
    instance = SimpleNamespace(runtime_status="starting", logger=MagicMock(), task=None, client=None)
    config = SimpleNamespace(name="grug", bot_id="b1", data_dir=str(tmp_path), personality=None, force_personality=None)

    class FakeBot:
        def __init__(self, *args, **kwargs):
            self.release = asyncio.Event()

        async def start(self, token):
            await self.release.wait()
            raise RuntimeError("gateway closed after startup")

    async def no_commands(instance, bot_env):
        return None

    monkeypatch.setattr(lifecycle.commands, "Bot", FakeBot)
    monkeypatch.setattr(lifecycle, "PersonalityEngine", lambda **kw: MagicMock())
    monkeypatch.setattr(lifecycle, "make_server_manager", lambda **kw: MagicMock())
    monkeypatch.setattr(lifecycle, "setup_bot_commands", no_commands, raising=False)

    async def scenario():
        await lifecycle._start_with_settings(instance, "b1", config, {"DISCORD_TOKEN": "fake_token"})
        # Startup succeeded; start_bot would now report running.
        assert not instance.task.done()
        instance.runtime_status = "running"
        instance.client.release.set()
        await _settle()

    asyncio.run(scenario())

    assert instance.runtime_status == "error"
    instance.logger.error.assert_called_once()
