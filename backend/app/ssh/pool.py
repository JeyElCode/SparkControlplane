"""A tiny connection pool so repeated status polls / phase steps reuse one
multiplexed asyncssh connection per node instead of reconnecting each time."""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Node
from .client import NodeConn, SSHClient, SSHError

__all__ = ["SSHError", "SSHClient", "NodeConn", "pool", "ssh_for_node"]


class SSHPool:
    def __init__(self) -> None:
        self._clients: dict[int, SSHClient] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, node_id: int) -> asyncio.Lock:
        return self._locks.setdefault(node_id, asyncio.Lock())

    async def get(self, conn: NodeConn) -> SSHClient:
        async with self._lock(conn.id):
            client = self._clients.get(conn.id)
            # NodeConn is a dataclass (value equality): reconnect only when the
            # connection-affecting parameters actually changed.
            if client is None or client.conn != conn:
                if client is not None:
                    await client.close()
                client = SSHClient(conn)
                self._clients[conn.id] = client
            try:
                await client.connect()
            except SSHError:
                self._clients.pop(conn.id, None)
                raise
            return client

    async def drop(self, node_id: int) -> None:
        client = self._clients.pop(node_id, None)
        if client is not None:
            await client.close()

    async def close_all(self) -> None:
        for client in list(self._clients.values()):
            await client.close()
        self._clients.clear()


pool = SSHPool()


async def ssh_for_node(session: AsyncSession, node: Node) -> SSHClient:
    """Build a fresh :class:`NodeConn` (decrypting secrets) and return a
    connected client from the pool. A fresh NodeConn each call ensures secret
    rotations / edits take effect (the pool reconnects when identity changes)."""
    conn = NodeConn.from_node(node)
    return await pool.get(conn)
