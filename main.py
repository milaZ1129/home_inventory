from contextlib import asynccontextmanager
import hmac
import logging
import secrets
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.sessions import SessionMiddleware

from config import settings
from database import (
    add_item,
    add_location,
    check_database,
    create_database_backup,
    delete_item,
    delete_location,
    ensure_daily_backup,
    get_audit_log,
    get_all_locations,
    get_deleted_items,
    get_item_by_id,
    get_location_by_id,
    get_locations_with_item_counts,
    init_db,
    list_data_backups,
    permanently_delete_item,
    purge_deleted_items,
    restore_item,
    restore_database_from_file,
    search_items,
    update_item,
    update_location,
)
from security import LoginRateLimiter, load_security_config, verify_credentials


BASE_DIR = Path(__file__).resolve().parent
FLOORS = ("一楼", "二楼", "三楼", "地下室")
MAX_BACKUP_UPLOAD_BYTES = settings.max_backup_upload_bytes
ACTION_LABELS = {
    "create": "新增",
    "update": "修改",
    "trash": "移入回收站",
    "restore": "恢复",
    "permanent_delete": "永久删除",
    "auto_purge": "自动清理",
    "delete": "删除",
    "restore_database": "恢复数据库",
}
ENTITY_LABELS = {
    "item": "物品",
    "location": "位置",
    "database": "数据库",
}
SECURITY_CONFIG = load_security_config()
login_limiter = LoginRateLimiter(max_attempts=5, window_seconds=300)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    if settings.enable_auto_backup:
        try:
            ensure_daily_backup()
        except (OSError, ValueError):
            logger.exception("自动数据库备份失败")
    try:
        purge_deleted_items(
            days=settings.recycle_bin_retention_days,
            actor="system",
        )
    except (OSError, ValueError):
        logger.exception("自动清理回收站失败")
    yield


app = FastAPI(
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECURITY_CONFIG.session_secret,
    session_cookie="home_inventory_session",
    max_age=settings.session_max_age_seconds,
    same_site="strict",
    https_only=settings.https_only,
)
if settings.allowed_hosts:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(settings.allowed_hosts),
    )
app.mount(
    "/static",
    StaticFiles(directory=BASE_DIR / "static"),
    name="static",
)


def get_or_create_csrf_token(request):
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def template_security_context(request):
    return {
        "csrf_token": get_or_create_csrf_token(request),
        "current_user": request.session.get("username"),
    }


templates = Jinja2Templates(
    directory=BASE_DIR / "templates",
    context_processors=[template_security_context],
)


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def safe_next_path(value):
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def require_login(request: Request):
    username = request.session.get("username")
    if username != SECURITY_CONFIG.username:
        next_path = request.url.path
        if request.url.query:
            next_path += f"?{request.url.query}"
        location = f"/login?next={quote(next_path, safe='')}"
        raise HTTPException(
            status_code=303,
            headers={"Location": location},
        )
    return username


def verify_csrf(
    request: Request,
    csrf_token: str = Form("", max_length=200),
):
    expected_token = request.session.get("csrf_token")
    if (
        not expected_token
        or not csrf_token
        or not hmac.compare_digest(expected_token, csrf_token)
    ):
        raise HTTPException(status_code=403, detail="CSRF 验证失败")


def clean_required_text(value, field_name):
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail=f"{field_name}不能为空")
    return cleaned


def group_locations(locations, selected_location_id=None):
    grouped_floors = []

    for floor in FLOORS:
        floor_locations = [
            dict(location)
            for location in locations
            if location["floor"] == floor
        ]
        if not floor_locations:
            continue

        room_names = []
        for location in floor_locations:
            if location["room"] not in room_names:
                room_names.append(location["room"])

        rooms = []
        for room_name in room_names:
            room_locations = [
                location
                for location in floor_locations
                if location["room"] == room_name
            ]
            rooms.append({
                "name": room_name,
                "locations": room_locations,
                "open": any(
                    location["id"] == selected_location_id
                    for location in room_locations
                ),
            })

        grouped_floors.append({
            "name": floor,
            "rooms": rooms,
            "open": any(
                location["id"] == selected_location_id
                for location in floor_locations
            ),
        })

    return grouped_floors


def remove_temporary_file(path):
    Path(path).unlink(missing_ok=True)


async def save_uploaded_backup(upload):
    filename = upload.filename or ""
    if not filename.lower().endswith(".db"):
        raise HTTPException(status_code=400, detail="请选择 .db 备份文件")

    temporary = tempfile.NamedTemporaryFile(
        prefix="home_inventory_restore_",
        suffix=".db",
        delete=False,
    )
    temporary_path = Path(temporary.name)
    total_bytes = 0

    try:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break

            total_bytes += len(chunk)
            if total_bytes > MAX_BACKUP_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="备份文件不能超过 100 MB",
                )
            temporary.write(chunk)
    except Exception:
        temporary.close()
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

    temporary.close()
    return temporary_path


@app.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next_path: str = Query("/", alias="next", max_length=500),
):
    if request.session.get("username") == SECURITY_CONFIG.username:
        return RedirectResponse(
            url=safe_next_path(next_path),
            status_code=303,
        )

    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": None,
            "next_path": safe_next_path(next_path),
        },
    )


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(..., max_length=50),
    password: str = Form(..., max_length=1024),
    next_path: str = Form("/", alias="next", max_length=500),
    _csrf: None = Depends(verify_csrf),
):
    client_key = request.client.host if request.client else "unknown"
    retry_after = login_limiter.retry_after(client_key)

    if retry_after:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "尝试次数过多，请稍后再试。",
                "next_path": safe_next_path(next_path),
            },
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    if not verify_credentials(
        SECURITY_CONFIG,
        username.strip(),
        password,
    ):
        login_limiter.record_failure(client_key)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "用户名或密码错误。",
                "next_path": safe_next_path(next_path),
            },
            status_code=401,
        )

    login_limiter.reset(client_key)
    request.session.clear()
    request.session["username"] = SECURITY_CONFIG.username
    request.session["csrf_token"] = secrets.token_urlsafe(32)

    return RedirectResponse(
        url=safe_next_path(next_path),
        status_code=303,
    )


@app.post("/logout")
def logout(
    request: Request,
    _user: str = Depends(require_login),
    _csrf: None = Depends(verify_csrf),
):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    q: str = Query("", max_length=100),
    _user: str = Depends(require_login),
):
    q = q.strip()
    locations = get_all_locations()

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "q": q,
            "search_results": search_items(q) if q else None,
            "all_items": search_items(""),
            "location_groups": group_locations(locations),
        },
    )


@app.post("/items/add")
def add_new_item(
    name: str = Form(..., max_length=100),
    location_id: int = Form(...),
    quantity: str = Form("", max_length=200),
    tags: str = Form("", max_length=200),
    _user: str = Depends(require_login),
    _csrf: None = Depends(verify_csrf),
):
    name = clean_required_text(name, "物品名称")

    try:
        add_item(
            name,
            location_id,
            quantity.strip(),
            tags.strip(),
            actor=_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url="/", status_code=303)


@app.get("/items/{item_id}/edit", response_class=HTMLResponse)
def edit_item_page(
    request: Request,
    item_id: int,
    _user: str = Depends(require_login),
):
    item = get_item_by_id(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="没有找到这个物品")

    return templates.TemplateResponse(
        request,
        "edit_item.html",
        {
            "item": item,
            "location_groups": group_locations(
                get_all_locations(),
                selected_location_id=item["location_id"],
            ),
        },
    )


@app.post("/items/{item_id}/edit")
def save_edited_item(
    item_id: int,
    name: str = Form(..., max_length=100),
    location_id: int = Form(...),
    quantity: str = Form("", max_length=200),
    tags: str = Form("", max_length=200),
    _user: str = Depends(require_login),
    _csrf: None = Depends(verify_csrf),
):
    name = clean_required_text(name, "物品名称")

    try:
        updated = update_item(
            item_id,
            name,
            location_id,
            quantity.strip(),
            tags.strip(),
            actor=_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not updated:
        raise HTTPException(status_code=404, detail="没有找到这个物品")

    return RedirectResponse(url="/", status_code=303)


@app.post("/items/{item_id}/delete")
def delete_existing_item(
    item_id: int,
    _user: str = Depends(require_login),
    _csrf: None = Depends(verify_csrf),
):
    if not delete_item(item_id, actor=_user):
        raise HTTPException(status_code=404, detail="没有找到这个物品")

    return RedirectResponse(url="/", status_code=303)


@app.get("/trash", response_class=HTMLResponse)
def trash_page(
    request: Request,
    restored: bool = Query(False),
    permanently_deleted: bool = Query(False),
    _user: str = Depends(require_login),
):
    return templates.TemplateResponse(
        request,
        "trash.html",
        {
            "items": get_deleted_items(),
            "restored": restored,
            "permanently_deleted": permanently_deleted,
        },
    )


@app.post("/items/{item_id}/restore")
def restore_deleted_item(
    item_id: int,
    _user: str = Depends(require_login),
    _csrf: None = Depends(verify_csrf),
):
    if not restore_item(item_id, actor=_user):
        raise HTTPException(status_code=404, detail="回收站中没有这个物品")

    return RedirectResponse(url="/trash?restored=true", status_code=303)


@app.post("/items/{item_id}/permanent-delete")
def permanently_delete_deleted_item(
    item_id: int,
    _user: str = Depends(require_login),
    _csrf: None = Depends(verify_csrf),
):
    if not permanently_delete_item(item_id, actor=_user):
        raise HTTPException(status_code=404, detail="回收站中没有这个物品")

    return RedirectResponse(
        url="/trash?permanently_deleted=true",
        status_code=303,
    )


@app.get("/history", response_class=HTMLResponse)
def history_page(
    request: Request,
    _user: str = Depends(require_login),
):
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "entries": get_audit_log(limit=200),
            "action_labels": ACTION_LABELS,
            "entity_labels": ENTITY_LABELS,
        },
    )


@app.get("/locations", response_class=HTMLResponse)
def location_management_page(
    request: Request,
    _user: str = Depends(require_login),
):
    return templates.TemplateResponse(
        request,
        "locations.html",
        {
            "locations": get_locations_with_item_counts(),
            "floors": FLOORS,
        },
    )


@app.post("/locations/add")
def add_new_location(
    floor: str = Form(..., max_length=20),
    room: str = Form(..., max_length=50),
    spot: str = Form(..., max_length=50),
    _user: str = Depends(require_login),
    _csrf: None = Depends(verify_csrf),
):
    floor = clean_required_text(floor, "楼层")
    room = clean_required_text(room, "房间/区域")
    spot = clean_required_text(spot, "具体位置")

    if floor not in FLOORS:
        raise HTTPException(status_code=400, detail="楼层不合法")

    try:
        add_location(floor, room, spot, actor=_user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url="/locations", status_code=303)


@app.get("/locations/{location_id}/edit", response_class=HTMLResponse)
def edit_location_page(
    request: Request,
    location_id: int,
    _user: str = Depends(require_login),
):
    location = get_location_by_id(location_id)
    if location is None:
        raise HTTPException(status_code=404, detail="没有找到这个位置")

    return templates.TemplateResponse(
        request,
        "edit_location.html",
        {
            "location": location,
            "floors": FLOORS,
        },
    )


@app.post("/locations/{location_id}/edit")
def save_edited_location(
    location_id: int,
    floor: str = Form(..., max_length=20),
    room: str = Form(..., max_length=50),
    spot: str = Form(..., max_length=50),
    _user: str = Depends(require_login),
    _csrf: None = Depends(verify_csrf),
):
    floor = clean_required_text(floor, "楼层")
    room = clean_required_text(room, "房间/区域")
    spot = clean_required_text(spot, "具体位置")

    if floor not in FLOORS:
        raise HTTPException(status_code=400, detail="楼层不合法")

    try:
        updated = update_location(
            location_id,
            floor,
            room,
            spot,
            actor=_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not updated:
        raise HTTPException(status_code=404, detail="没有找到这个位置")

    return RedirectResponse(url="/locations", status_code=303)


@app.post("/locations/{location_id}/delete")
def delete_existing_location(
    location_id: int,
    _user: str = Depends(require_login),
    _csrf: None = Depends(verify_csrf),
):
    try:
        deleted = delete_location(location_id, actor=_user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not deleted:
        raise HTTPException(status_code=404, detail="没有找到这个位置")

    return RedirectResponse(url="/locations", status_code=303)


@app.get("/backups", response_class=HTMLResponse)
def backup_management_page(
    request: Request,
    restored: bool = Query(False),
    _user: str = Depends(require_login),
):
    return templates.TemplateResponse(
        request,
        "backups.html",
        {
            "backups": list_data_backups(),
            "restored": restored,
            "error": None,
        },
    )


@app.post("/backups/download")
def download_database_backup(
    _user: str = Depends(require_login),
    _csrf: None = Depends(verify_csrf),
):
    temporary = tempfile.NamedTemporaryFile(
        prefix="home_inventory_download_",
        suffix=".db",
        delete=False,
    )
    temporary_path = Path(temporary.name)
    temporary.close()

    try:
        create_database_backup(temporary_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    download_name = (
        "home_inventory_"
        + datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        + ".db"
    )
    return FileResponse(
        temporary_path,
        media_type="application/vnd.sqlite3",
        filename=download_name,
        background=BackgroundTask(
            remove_temporary_file,
            temporary_path,
        ),
    )


@app.post("/backups/restore", response_class=HTMLResponse)
async def restore_database_backup(
    request: Request,
    backup_file: UploadFile = File(...),
    _user: str = Depends(require_login),
    _csrf: None = Depends(verify_csrf),
):
    temporary_path = await save_uploaded_backup(backup_file)
    try:
        restore_database_from_file(temporary_path, actor=_user)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "backups.html",
            {
                "backups": list_data_backups(),
                "restored": False,
                "error": str(exc),
            },
            status_code=400,
        )
    finally:
        temporary_path.unlink(missing_ok=True)

    return RedirectResponse(url="/backups?restored=true", status_code=303)


@app.get("/health")
def health():
    check_database()
    return {"status": "ok", "database": "ok"}
