"""Server-Sent Events (SSE) hub for live dashboard updates.

New complaints and status changes are broadcast to every connected admin
browser instantly (no page refresh). Works with FastAPI/Starlette and is
proxy-friendly on cloud hosts. A simple in-memory subscriber registry is
enough because uvicorn runs a single process.
"""

import asyncio
import json
from collections import defaultdict

# subscriber_id -> asyncio.Queue
_subscribers = defaultdict(list)
_sub_id = 0


def add_client():
    """Register a client and return (client_id, queue) for the SSE endpoint."""
    global _sub_id
    _sub_id += 1
    cid = _sub_id
    queue = asyncio.Queue(maxsize=200)
    _subscribers[cid] = queue
    return cid, queue


def remove_client(cid: int):
    _subscribers.pop(cid, None)


async def publish(event: str, data: dict):
    """Send an event to all connected clients."""
    payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    stale = []
    for cid, q in list(_subscribers.items()):
        try:
            q.put_nowait(payload)
            if q.qsize() > 150:
                stale.append(cid)
        except asyncio.QueueFull:
            stale.append(cid)
    for cid in stale:
        _subscribers.pop(cid, None)


async def emit_new_complaint(complaint: dict):
    summary = {
        "id": complaint["id"],
        "ticket_no": complaint["ticket_no"],
        "title": complaint["title"] or complaint["description"][:60],
        "department": complaint["department"],
        "fault_type": complaint["fault_type"],
        "priority": complaint["priority"],
        "status": complaint["status"],
        "created_at": complaint["created_at"],
        "reporter_name": complaint["reporter_name"],
    }
    await publish("new_complaint", summary)


async def emit_status_change(cid: int, data: dict):
    await publish("status_change", {"id": cid, **data})