import asyncio
import os

from temporalio.client import Client
from temporalio.worker import Worker

from .workflow import RunWorkflow


async def main() -> None:
    client = await Client.connect(os.getenv("TEMPORAL_ADDRESS", "localhost:7233"))
    worker = Worker(client, task_queue="teamswarm", workflows=[RunWorkflow])
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
