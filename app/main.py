from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.chats import router as chats_router
from app.api.models import ApiErrorDetail, ApiErrorResponse, ApiValidationIssue
from app.api.runtime import TaskRuntime
from app.api.service import TaskApiServiceError, TaskService
from app.api.tasks import router as tasks_router
from app.chat.dispatch import StandaloneChatDispatcher
from app.chat.service import ChatApiError, StandaloneChatService
from app.config import get_settings


def create_app(
    *, task_service: TaskService | None = None, runtime: TaskRuntime | None = None,
    chat_service: StandaloneChatService | None = None,
    chat_dispatcher: StandaloneChatDispatcher | None = None,
) -> FastAPI:
    if task_service is not None and runtime is not None:
        raise ValueError("provide either task_service or runtime, not both")
    if chat_dispatcher is not None and chat_service is None:
        raise ValueError("chat dispatcher requires a chat service")

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        if runtime is not None:
            await runtime.service.startup(runtime.recovery)
        if chat_dispatcher is not None:
            await chat_dispatcher.startup()
        try:
            yield
        finally:
            if runtime is not None:
                await runtime.service.shutdown()
            if chat_dispatcher is not None:
                await chat_dispatcher.shutdown()

    application = FastAPI(title="CodeCrew", version="0.1.0", lifespan=lifespan)
    if runtime is not None:
        task_service = runtime.service
    if task_service is not None:
        application.state.task_service = task_service
    if chat_service is not None:
        application.state.chat_service = chat_service
    if chat_dispatcher is not None:
        application.state.chat_dispatcher = chat_dispatcher

    @application.exception_handler(ChatApiError)
    async def chat_service_error(_request: Request, error: ChatApiError) -> JSONResponse:
        response = ApiErrorResponse(error=ApiErrorDetail(code=error.code, message=str(error)))
        return JSONResponse(
            status_code=error.status_code,
            content=response.model_dump(mode="json", exclude_none=True),
        )

    @application.exception_handler(TaskApiServiceError)
    async def task_service_error(
        _request: Request, error: TaskApiServiceError
    ) -> JSONResponse:
        response = ApiErrorResponse(
            error=ApiErrorDetail(code=error.code, message=str(error))
        )
        return JSONResponse(
            status_code=error.status_code,
            content=response.model_dump(mode="json", exclude_none=True),
        )

    @application.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: Request, error: RequestValidationError
    ) -> JSONResponse:
        response = ApiErrorResponse(
            error=ApiErrorDetail(
                code="validation_error",
                message="request validation failed",
                issues=tuple(
                    ApiValidationIssue(
                        location=tuple(item["loc"]),
                        message=item["msg"],
                        type=item["type"],
                    )
                    for item in error.errors()
                ),
            )
        )
        return JSONResponse(
            status_code=422,
            content=response.model_dump(mode="json", exclude_none=True),
        )

    @application.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        settings = get_settings()
        return {"status": "ok", "environment": settings.environment}

    application.include_router(tasks_router)
    application.include_router(chats_router)
    web_root = Path(__file__).resolve().parent / "web"
    application.mount("/ui/assets", StaticFiles(directory=web_root), name="ui-assets")

    @application.get("/ui", include_in_schema=False)
    @application.get("/ui/", include_in_schema=False)
    def task_ui() -> FileResponse:
        return FileResponse(web_root / "index.html")

    @application.get("/ui/chat", include_in_schema=False)
    @application.get("/ui/chat/", include_in_schema=False)
    def chat_ui() -> FileResponse:
        return FileResponse(web_root / "chat.html")

    return application


app = create_app()
