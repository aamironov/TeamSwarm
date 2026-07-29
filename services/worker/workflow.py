"""Durable workflow contract for the next MVP increment.

The API currently uses the local runner in `services.api.app.runtime` so the
project works without Temporal credentials. This workflow is intentionally kept
small and establishes the durable boundary used when WORKFLOW_BACKEND=temporal
is enabled.
"""

from temporalio import workflow


@workflow.defn
class RunWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> str:
        return run_id
