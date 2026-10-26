from fastapi import FastAPI

from app.config import get_settings
from app.m1_api import router as m1_router


app = FastAPI(
    title="MeetPoint M1",
    version="0.1.0",
)

app.include_router(m1_router)

from app.m5_api import router as m5_router
app.include_router(m5_router)


@app.middleware("http")
async def json_utf8_header(request, call_next):
    response = await call_next(request)

    media_type = response.headers.get(
        "content-type", ""
    ).split(";", 1)[0]

    if media_type.strip().lower() == "application/json":
        response.headers["content-type"] = (
            "application/json; charset=utf-8"
        )

    return response


@app.get("/health")
def health():
    settings = get_settings()

    return {
        "status": "ok",
        "project": "MeetPoint",
        "milestone": "M1",
        "amap_key_configured": bool(
            settings.amap_web_key.get_secret_value().strip()
        ),
        "llm_key_configured": bool(
            settings.llm_api_key.get_secret_value().strip()
        ),
        "llm_model": settings.llm_model,
        "note": "此接口只检查服务和配置是否存在；外部接入请运行 M0 命令。",
    }

from app.m6_api import router as m6_router
app.include_router(m6_router)
