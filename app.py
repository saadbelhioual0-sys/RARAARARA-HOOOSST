
from fastapi import FastAPI, Request, Depends, HTTPException, status, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
import os
import shutil
import asyncio
import random
from datetime import datetime

from database import init_db, get_db, User, Server, FailedLogin, LoginLog
from security import hash_password, verify_password, create_access_token, decode_token
from sandbox_manager import create_user_container, start_python_server, stop_container, get_container_stats, execute_command_in_container

app = FastAPI(title="JAGWAR HOST")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# إنشاء مجلدات المستخدمين
os.makedirs("user_servers", exist_ok=True)
os.makedirs("static", exist_ok=True)
os.makedirs("templates", exist_ok=True)

@app.on_event("startup")
async def startup():
    await init_db()
    # عمل مجلد لكل مستخدم موجود
    async for session in get_db():
        result = await session.execute(select(User))
        users = result.scalars().all()
        for user in users:
            os.makedirs(f"user_servers/user_{user.id}", exist_ok=True)
        break

# Helper functions
async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)):
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401)
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401)
    return user

async def log_failed_login(ip: str, username: str, user_agent: str, db: AsyncSession):
    log = FailedLogin(ip=ip, username_attempted=username, user_agent=user_agent)
    db.add(log)
    await db.commit()

async def log_login(ip: str, username: str, user_agent: str, success: bool, db: AsyncSession):
    log = LoginLog(username=username, ip=ip, user_agent=user_agent, success=success)
    db.add(log)
    await db.commit()

# Routes
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...), db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host
    user_agent = request.headers.get("user-agent", "")
    
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    
    if not user or not verify_password(password, user.password_hash):
        await log_failed_login(client_ip, username, user_agent, db)
        await log_login(client_ip, username, user_agent, False, db)
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})
    
    if username == "JAGWARGG" and password == "JAGWAR12345":
        token = create_access_token({"sub": username, "is_admin": True})
        await log_login(client_ip, username, user_agent, True, db)
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(key="token", value=token, httponly=True)
        return response
    
    token = create_access_token({"sub": username, "is_admin": False})
    await log_login(client_ip, username, user_agent, True, db)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie(key="token", value=token, httponly=True)
    return response

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("token")
    if not token:
        return RedirectResponse("/login", status_code=303)
    payload = decode_token(token)
    if not payload or payload.get("sub") != "JAGWARGG":
        return RedirectResponse("/login", status_code=303)
    
    users_result = await db.execute(select(User))
    users = users_result.scalars().all()
    
    servers_result = await db.execute(select(Server))
    servers = servers_result.scalars().all()
    
    failed_result = await db.execute(select(FailedLogin).order_by(FailedLogin.timestamp.desc()).limit(50))
    failed_logs = failed_result.scalars().all()
    
    login_result = await db.execute(select(LoginLog).order_by(LoginLog.timestamp.desc()).limit(50))
    login_logs = login_result.scalars().all()
    
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "users": users,
        "servers": servers,
        "failed_logs": failed_logs,
        "login_logs": login_logs
    })

@app.post("/admin/add_user")
async def add_user(request: Request, username: str = Form(...), password: str = Form(...), db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("token")
    payload = decode_token(token)
    if not payload or payload.get("sub") != "JAGWARGG":
        raise HTTPException(status_code=403)
    
    existing = await db.execute(select(User).where(User.username == username))
    if existing.scalar_one_or_none():
        return RedirectResponse("/admin?error=User exists", status_code=303)
    
    new_user = User(username=username, password_hash=hash_password(password))
    db.add(new_user)
    await db.commit()
    os.makedirs(f"user_servers/user_{new_user.id}", exist_ok=True)
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/make_premium/{user_id}")
async def make_premium(user_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("token")
    payload = decode_token(token)
    if not payload or payload.get("sub") != "JAGWARGG":
        raise HTTPException(status_code=403)
    
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        user.is_premium = True
        user.max_servers = 5
        await db.commit()
    return RedirectResponse("/admin", status_code=303)

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("token")
    if not token:
        return RedirectResponse("/login", status_code=303)
    payload = decode_token(token)
    if not payload:
        return RedirectResponse("/login", status_code=303)
    
    username = payload.get("sub")
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        return RedirectResponse("/login", status_code=303)
    
    servers_result = await db.execute(select(Server).where(Server.owner_id == user.id))
    servers = servers_result.scalars().all()
    
    # تحديث الإحصائيات للحاويات النشطة
    for server in servers:
        if server.container_id:
            stats = await get_container_stats(server.container_id)
            server.cpu_usage = stats["cpu"]
            server.ram_usage = stats["ram"]
    
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "servers": servers
    })

@app.post("/create_server")
async def create_server(request: Request, name: str = Form(...), description: str = Form(...), db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("token")
    payload = decode_token(token)
    if not payload:
        return RedirectResponse("/login", status_code=303)
    
    username = payload.get("sub")
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        return RedirectResponse("/login", status_code=303)
    
    servers_result = await db.execute(select(Server).where(Server.owner_id == user.id))
    current_servers = servers_result.scalars().all()
    if len(current_servers) >= user.max_servers:
        return RedirectResponse("/dashboard?error=Max servers limit reached", status_code=303)
    
    # إنشاء سيرفر جديد مع منفذ عشوائي
    port = random.randint(10000, 60000)
    new_server = Server(
        name=name,
        description=description,
        language="python",
        owner_id=user.id,
        status="stopped",
        port=port
    )
    db.add(new_server)
    await db.commit()
    
    # إنشاء مجلد الكود الخاص بالسيرفر
    server_dir = f"user_servers/user_{user.id}/server_{new_server.id}"
    os.makedirs(server_dir, exist_ok=True)
    
    # إنشاء main.py افتراضي
    with open(f"{server_dir}/main.py", "w") as f:
        f.write("print('Hello from JAGWAR HOST!')\n")
    with open(f"{server_dir}/requirements.txt", "w") as f:
        f.write("# Add your requirements here\n")
    
    return RedirectResponse("/dashboard", status_code=303)

@app.post("/start_server/{server_id}")
async def start_server_route(server_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("token")
    payload = decode_token(token)
    if not payload:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        return JSONResponse({"error": "Server not found"}, status_code=404)
    
    user_result = await db.execute(select(User).where(User.id == server.owner_id))
    user = user_result.scalar_one()
    if user.username != payload.get("sub"):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    server_dir = f"user_servers/user_{user.id}/server_{server.id}"
    container_id = await create_user_container(server.id, server.port, server_dir)
    if container_id:
        server.container_id = container_id
        server.status = "starting"
        await db.commit()
        
        # تشغيل السيرفر في الخلفية
        asyncio.create_task(run_server_async(container_id, server_dir, server.id, db))
        return JSONResponse({"status": "started", "container_id": container_id})
    else:
        return JSONResponse({"error": "Failed to create container"}, status_code=500)

async def run_server_async(container_id, server_dir, server_id, db):
    await start_python_server(container_id, server_dir)
    async with db as session:
        result = await session.execute(select(Server).where(Server.id == server_id))
        server = result.scalar_one()
        server.status = "running"
        await session.commit()

@app.post("/stop_server/{server_id}")
async def stop_server_route(server_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("token")
    payload = decode_token(token)
    if not payload:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if server and server.container_id:
        await stop_container(server.container_id)
        server.container_id = None
        server.status = "stopped"
        await db.commit()
    return JSONResponse({"status": "stopped"})

@app.post("/restart_server/{server_id}")
async def restart_server_route(server_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    await stop_server_route(server_id, request, db)
    await asyncio.sleep(2)
    return await start_server_route(server_id, request, db)

@app.get("/server/{server_id}/control", response_class=HTMLResponse)
async def server_control(request: Request, server_id: int, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("token")
    if not token:
        return RedirectResponse("/login", status_code=303)
    payload = decode_token(token)
    
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        return RedirectResponse("/dashboard", status_code=303)
    
    user_result = await db.execute(select(User).where(User.id == server.owner_id))
    user = user_result.scalar_one()
    if user.username != payload.get("sub"):
        return RedirectResponse("/dashboard", status_code=303)
    
    # جلب إحصائيات حقيقية
    stats = {"cpu": "0%", "ram": "0MB"}
    if server.container_id:
        stats = await get_container_stats(server.container_id)
        server.cpu_usage = stats["cpu"]
        server.ram_usage = stats["ram"]
        await db.commit()
    
    return templates.TemplateResponse("server_control.html", {
        "request": request,
        "server": server,
        "stats": stats
    })

@app.post("/server/{server_id}/command")
async def execute_command(server_id: int, command: str, request: Request, db: AsyncSession = Depends(get_db)):
    token = request.cookies.get("token")
    payload = decode_token(token)
    if not payload:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if server and server.container_id:
        output = await execute_command_in_container(server.container_id, command)
        return JSONResponse({"output": output})
    return JSONResponse({"output": "Server not running"})

@app.get("/server/{server_id}/files")
async def list_files(server_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    # نفس نظام التوثيق ثم إرجاع قائمة الملفات
    token = request.cookies.get("token")
    payload = decode_token(token)
    result = await db.execute(select(Server).where(Server.id == server_id))
    server = result.scalar_one_or_none()
    if not server:
        return JSONResponse({"files": []}, status_code=404)
    
    user_result = await db.execute(select(User).where(User.id == server.owner_id))
    user = user_result.scalar_one()
    if user.username != payload.get("sub"):
        return JSONResponse({"files": []}, status_code=403)
    
    server_dir = f"user_servers/user_{user.id}/server_{server.id}"
    files = []
    if os.path.exists(server_dir):
        for item in os.listdir(server_dir):
            path = os.path.join(server_dir, item)
            files.append({
                "name": item,
                "is_dir": os.path.isdir(path),
                "size": os.path.getsize(path) if os.path.isfile(path) else 0
            })
    return JSONResponse({"files": files})

# استمرار لباقي نقاط النهاية (رفع الملفات، حذف، إعادة تسمية، محرر)
# سأكمل في الرد التالي بسبب طول الكود
