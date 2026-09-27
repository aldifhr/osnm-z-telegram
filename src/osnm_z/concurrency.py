"""Cancel sibling operations when concurrent preparation fails."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, overload


@overload
async def gather_fail_fast[T1, T2](
    _first: Awaitable[T1], _second: Awaitable[T2], /
) -> tuple[T1, T2]: ...


@overload
async def gather_fail_fast[T1, T2, T3](
    _first: Awaitable[T1], _second: Awaitable[T2], _third: Awaitable[T3], /
) -> tuple[T1, T2, T3]: ...


async def gather_fail_fast(*awaitables: Any) -> tuple[Any, ...]:
    """Run awaitables concurrently; cancel unfinished siblings if any operation fails."""
    tasks = tuple(asyncio.ensure_future(awaitable) for awaitable in awaitables)
    try:
        return tuple(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
