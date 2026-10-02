import asyncio
from collections.abc import Awaitable, Callable

import httpx


async def retry[T](operation: Callable[[], Awaitable[T]], attempts: int = 3) -> T:
    for attempt in range(attempts):
        try:
            return await operation()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as error:
            if isinstance(error, httpx.HTTPStatusError):
                status = error.response.status_code
                if status not in (429, 500, 502, 503, 504):
                    raise
            if attempt == attempts - 1:
                raise
            delay = 2**attempt
            if isinstance(error, httpx.HTTPStatusError):
                try:
                    delay = min(30.0, float(error.response.headers.get("retry-after", delay)))
                except ValueError:
                    pass
            await asyncio.sleep(delay)
    raise RuntimeError("Unreachable")
