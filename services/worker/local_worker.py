"""Database-backed TeamSwarm worker.

Run this process with TEAMSWARM_INLINE_WORKER_ENABLED=false in the API process
to let one or more external workers pull leased, dependency-ready tasks.
"""

import asyncio
import os
from uuid import uuid4

from services.api.app.db import init_db
from services.api.app.runtime import RunService


async def main() -> None:
    await init_db()
    worker_id = os.getenv("TEAMSWARM_WORKER_ID", f"worker-{uuid4()}")
    service = RunService(inline_worker_enabled=False)
    while True:
        worked = await service.work_once(worker_id)
        if not worked:
            await asyncio.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(main())
