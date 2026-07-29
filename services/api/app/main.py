from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware

from .chat import ChatService
from .config import get_settings
from .db import init_db
from .providers import get_model_catalog
from .runtime import RunService
from .schemas import (
    ChatCreate,
    ChatMessageInput,
    ChatView,
    ModelCatalog,
    ProjectCreate,
    ProjectView,
    ReplayView,
    RunCreate,
    RunQueueView,
    RunView,
    TokenUsageWindow,
    TraceView,
    UsageSummary,
)

service = RunService()
chat_service = ChatService()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield


app = FastAPI(title="TeamSwarm MVP", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().api_cors_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "provider_mode": get_settings().provider_mode}


@app.get("/models", response_model=ModelCatalog)
async def get_models() -> ModelCatalog:
    return ModelCatalog(
        active_provider=get_settings().provider_mode,
        models=[model.__dict__ for model in await get_model_catalog()],
    )


@app.get("/usage/last-24-hours", response_model=TokenUsageWindow)
async def get_usage_last_24_hours() -> TokenUsageWindow:
    return await service.usage_last_24_hours()


@app.get("/projects", response_model=list[ProjectView])
async def list_projects() -> list[ProjectView]:
    return await chat_service.list_projects()


@app.post("/projects", response_model=ProjectView, status_code=status.HTTP_201_CREATED)
async def create_project(request: ProjectCreate) -> ProjectView:
    try:
        return await chat_service.create_project(request)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/projects/{project_id}/chats", response_model=list[ChatView])
async def list_chats(project_id: str) -> list[ChatView]:
    try:
        return await chat_service.list_chats(project_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Project not found") from error


@app.post("/chats", response_model=ChatView, status_code=status.HTTP_201_CREATED)
async def create_chat(request: ChatCreate) -> ChatView:
    try:
        return await chat_service.create_chat(request)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Project not found") from error


@app.get("/chats/{chat_id}", response_model=ChatView)
async def get_chat(chat_id: str) -> ChatView:
    try:
        return await chat_service.get_chat(chat_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Chat not found") from error


@app.post("/chats/{chat_id}/messages", response_model=ChatView)
async def send_chat_message(chat_id: str, request: ChatMessageInput) -> ChatView:
    try:
        return await chat_service.send_message(chat_id, request)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Chat or project not found") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.post("/runs", response_model=RunView, status_code=status.HTTP_202_ACCEPTED)
async def create_run(request: RunCreate) -> RunView:
    try:
        return await service.create(request)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/runs/{run_id}", response_model=RunView)
async def get_run(run_id: str) -> RunView:
    try:
        return await service.get(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Run not found") from error


@app.get("/runs/{run_id}/queue", response_model=RunQueueView)
async def get_run_queue(run_id: str) -> RunQueueView:
    try:
        return await service.queue(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Run not found") from error


@app.get("/runs/{run_id}/replay", response_model=ReplayView)
async def get_run_replay(run_id: str) -> ReplayView:
    try:
        return await service.replay(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Run not found") from error


@app.post("/runs/{run_id}/cancel", response_model=RunView)
async def cancel_run(run_id: str) -> RunView:
    try:
        return await service.cancel(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Run not found") from error


@app.get("/runs/{run_id}/trace", response_model=TraceView)
async def get_trace(run_id: str) -> TraceView:
    try:
        return await service.trace(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Run not found") from error


@app.get("/runs/{run_id}/usage", response_model=UsageSummary)
async def get_usage(run_id: str) -> UsageSummary:
    try:
        return await service.usage(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Run not found") from error


@app.get("/runs/{run_id}/events")
async def get_events(run_id: str) -> Response:
    try:
        trace = await service.trace(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Run not found") from error
    lines = "".join(
        f"event: {event.kind}\ndata: {event.model_dump_json()}\n\n" for event in trace.events
    )
    return Response(lines, media_type="text/event-stream")
