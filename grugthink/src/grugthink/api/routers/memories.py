"""Memory management API endpoints.

The store calls (get_server_db / add_fact / search_facts / get_all_facts /
delete_fact) are BLOCKING: first use lazily loads the gateway embedder (a
network self-test), and the pgvector backend embeds via a synchronous HTTP call
that can take up to its 30s timeout when the Spark is busy loading the chat
model. Run them in the threadpool -- executed inline on the event loop they
starve /api/health, the readiness probe flips (ingress 502s), and repeated
liveness failures get the container killed. Observed live during the pgvector
cutover.

Capacity note: anyio's default worker-thread limiter is 40 tokens shared across
the whole app, so a burst of slow-embedding requests queues other offloaded work
rather than growing unbounded -- acceptable for this single-admin surface. A
client disconnect does not cancel an in-flight threadpool call; it just runs to
completion in the background.
"""

from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from ...bot_manager import BotManager
from ...logging_config import get_logger
from ..dependencies import get_bot_manager, memory_manager_required

router = APIRouter(prefix="/api/bots", tags=["memories"])
log = get_logger(__name__)


async def _get_server_name(bot_instance, server_id: str) -> str:
    """Get friendly server name for display."""
    if server_id == "dm":
        return "Direct Messages"

    # Try to get server name from Discord client
    if hasattr(bot_instance, "client") and bot_instance.client and bot_instance.client.is_ready():
        try:
            guild = bot_instance.client.get_guild(int(server_id))
            if guild:
                return guild.name
        except (ValueError, AttributeError):
            pass

    return f"Server {server_id}"


@router.get("/{bot_id}/memories", dependencies=[Depends(memory_manager_required)])
async def get_bot_memories(
    bot_id: str,
    server_id: str = None,
    search: str = None,
    limit: int = 100,
    bot_manager: BotManager = Depends(get_bot_manager),
):
    """Get memories for a specific bot, optionally filtered by server."""
    try:
        # Get the bot's server manager and database
        bot = bot_manager.bots.get(bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail=f"Bot '{bot_id}' not found")

        server_manager = getattr(bot, "server_manager", None)
        if not server_manager:
            raise HTTPException(status_code=500, detail="Server manager not available")

        if server_id:
            # Get memories from specific server
            server_db = await run_in_threadpool(server_manager.get_server_db, server_id)

            all_facts = await run_in_threadpool(server_db.get_all_facts)
            if search:
                facts = await run_in_threadpool(server_db.search_facts, search, limit)
            else:
                facts = all_facts[:limit]

            return {
                "bot_id": bot_id,
                "server_id": server_id,
                "total_memories": len(all_facts),
                "memories": [{"id": i, "content": fact, "server_id": server_id} for i, fact in enumerate(facts)],
                "search": search,
                "limit": limit,
            }
        else:
            # Get memories from all servers (aggregated view)
            all_memories = []
            total_memories = 0

            # Access the internal server_dbs dict to get all servers this bot knows about
            if hasattr(server_manager, "server_dbs"):
                # Snapshot under the manager lock (in the threadpool -- the lock
                # can be held for a slow first-time embedder load): a concurrent
                # get_server_db() mutating the dict mid-iteration would raise
                # "dictionary changed size during iteration".
                def _snapshot_server_dbs():
                    lock = getattr(server_manager, "lock", None)
                    if lock is not None:
                        with lock:
                            return list(server_manager.server_dbs.items())
                    return list(server_manager.server_dbs.items())

                for srv_id, srv_db in await run_in_threadpool(_snapshot_server_dbs):
                    srv_facts = await run_in_threadpool(srv_db.get_all_facts)
                    total_memories += len(srv_facts)

                    # Add server context to each fact
                    for i, fact in enumerate(srv_facts):
                        all_memories.append(
                            {
                                "id": len(all_memories),
                                "content": fact,
                                "server_id": srv_id,
                                "server_name": await _get_server_name(bot, srv_id),
                            }
                        )

            # Apply search filter if provided
            if search:
                search_lower = search.lower()
                all_memories = [m for m in all_memories if search_lower in m["content"].lower()]

            # Apply limit
            all_memories = all_memories[:limit]

            return {
                "bot_id": bot_id,
                "server_id": None,
                "total_memories": total_memories,
                "memories": all_memories,
                "search": search,
                "limit": limit,
                "servers": list(server_manager.server_dbs.keys()) if hasattr(server_manager, "server_dbs") else [],
            }

    except HTTPException:
        raise
    except Exception as e:
        log.error("Failed to get bot memories", extra={"bot_id": bot_id, "error": str(e)})
        raise HTTPException(status_code=500, detail=f"Failed to get memories: {str(e)}")


@router.post("/{bot_id}/memories", dependencies=[Depends(memory_manager_required)])
async def add_bot_memory(bot_id: str, memory: Dict[str, str], bot_manager: BotManager = Depends(get_bot_manager)):
    """Add a new memory to a bot."""
    content = memory.get("content", "").strip()
    server_id = memory.get("server_id", "admin")  # Default to "admin" server for manually added facts

    if not content:
        raise HTTPException(status_code=400, detail="Memory content is required")

    try:
        bot = bot_manager.bots.get(bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail=f"Bot '{bot_id}' not found")

        server_manager = getattr(bot, "server_manager", None)
        if not server_manager:
            raise HTTPException(status_code=500, detail="Server manager not available")

        server_db = await run_in_threadpool(server_manager.get_server_db, server_id)
        success = await run_in_threadpool(server_db.add_fact, content)

        # Audit log for memory management
        log.info(
            "Memory management: Added memory",
            extra={
                "action": "add_memory",
                "bot_id": bot_id,
                "server_id": server_id,
                "content_preview": content[:100],
                "success": success,
            },
        )

        if success:
            return {"message": "Memory added successfully", "content": content}
        else:
            return {"message": "Memory already exists", "content": content}

    except HTTPException:
        raise
    except Exception as e:
        log.error("Failed to add bot memory", extra={"bot_id": bot_id, "error": str(e)})
        raise HTTPException(status_code=500, detail=f"Failed to add memory: {str(e)}")


@router.delete("/{bot_id}/memories", dependencies=[Depends(memory_manager_required)])
async def delete_bot_memory(bot_id: str, memory: Dict[str, str], bot_manager: BotManager = Depends(get_bot_manager)):
    """Delete a memory from a bot."""
    content = memory.get("content", "").strip()
    server_id = memory.get("server_id", "admin")  # Default to admin server if not specified

    if not content:
        raise HTTPException(status_code=400, detail="Memory content is required")

    try:
        bot = bot_manager.bots.get(bot_id)
        if not bot:
            raise HTTPException(status_code=404, detail=f"Bot '{bot_id}' not found")

        server_manager = getattr(bot, "server_manager", None)
        if not server_manager:
            raise HTTPException(status_code=500, detail="Server manager not available")

        server_db = await run_in_threadpool(server_manager.get_server_db, server_id)

        # Delete fact from database
        success = await run_in_threadpool(server_db.delete_fact, content)

        # Audit log for memory management
        log.info(
            "Memory management: Deleted memory",
            extra={"action": "delete_memory", "bot_id": bot_id, "content_preview": content[:100], "success": success},
        )

        if success:
            return {"message": "Memory deleted successfully"}
        else:
            raise HTTPException(status_code=404, detail="Memory not found")

    except HTTPException:
        raise
    except Exception as e:
        log.error("Failed to delete bot memory", extra={"bot_id": bot_id, "error": str(e)})
        raise HTTPException(status_code=500, detail=f"Failed to delete memory: {str(e)}")
