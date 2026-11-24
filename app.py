from datetime import datetime, timedelta, timezone
from typing import Optional
import os, time
from collections import defaultdict, deque

import jwt
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, Field
from pydantic_settings import BaseSettings
from pwdlib import PasswordHash
from sqlalchemy import create_engine, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, Session

class Settings(BaseSettings):
    SECRET_KEY: str = "CHANGE_ME_BEFORE_DEPLOYMENT"
    DATABASE_URL: str = "sqlite:///./obito.db"
    ALLOWED_ORIGINS: str = "http://127.0.0.1:5500,http://localhost:5500"
    ACCESS_TOKEN_MINUTES: int = 30
    class Config:
        env_file = ".env"

settings = Settings()
if settings.SECRET_KEY == "CHANGE_ME_BEFORE_DEPLOYMENT":
    print("WARNING: Set a strong SECRET_KEY in .env before deployment.")

connect_args = {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(settings.DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

class Base(DeclarativeBase): pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), default="موقعي الجديد")
    content: Mapped[str] = mapped_column(Text, default='{"title":"موقعي الجديد","subtitle":"ابدأ رحلتك الرقمية","sections":[]}')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

Base.metadata.create_all(bind=engine)
password_hash = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)

app = FastAPI(title="OBITO WEB BUILDER API", version="1.0.0")
origins = [x.strip() for x in settings.ALLOWED_ORIGINS.split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                   allow_methods=["GET","POST","PUT","DELETE"], allow_headers=["Authorization","Content-Type"])

# Lightweight local development limiter. Use Redis-backed limits in production/multi-worker deployments.
buckets = defaultdict(deque)
@app.middleware("http")
async def security_middleware(request: Request, call_next):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    q = buckets[ip]
    while q and q[0] < now - 60: q.popleft()
    if len(q) >= 120:
        return Response("Too many requests", status_code=429)
    q.append(now)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else "public, max-age=300"
    return response

def db():
    session = SessionLocal()
    try: yield session
    finally: session.close()

class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)

class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    content: str = Field(max_length=200000)

def token_for(user_id: int):
    exp = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_MINUTES)
    return jwt.encode({"sub": str(user_id), "exp": exp, "iat": datetime.now(timezone.utc)}, settings.SECRET_KEY, algorithm="HS256")

def current_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer), session: Session = Depends(db)):
    if not creds: raise HTTPException(401, "يلزم تسجيل الدخول")
    try:
        payload = jwt.decode(creds.credentials, settings.SECRET_KEY, algorithms=["HS256"])
        uid = int(payload["sub"])
    except Exception:
        raise HTTPException(401, "جلسة غير صالحة أو منتهية")
    user = session.get(User, uid)
    if not user: raise HTTPException(401, "المستخدم غير موجود")
    return user

@app.get("/api/health")
def health():
    return {"status":"ok","service":"OBITO WEB BUILDER"}

@app.post("/api/auth/register")
def register(data: RegisterIn, session: Session = Depends(db)):
    email = data.email.lower()
    if session.query(User).filter_by(email=email).first():
        raise HTTPException(409, "البريد مستخدم بالفعل")
    user = User(email=email, password_hash=password_hash.hash(data.password))
    session.add(user); session.commit(); session.refresh(user)
    return {"access_token": token_for(user.id), "token_type":"bearer", "email":user.email}

@app.post("/api/auth/login")
def login(data: LoginIn, session: Session = Depends(db)):
    user = session.query(User).filter_by(email=data.email.lower()).first()
    if not user or not password_hash.verify(data.password, user.password_hash):
        raise HTTPException(401, "بيانات الدخول غير صحيحة")
    return {"access_token": token_for(user.id), "token_type":"bearer", "email":user.email}

@app.get("/api/projects")
def list_projects(user: User = Depends(current_user), session: Session = Depends(db)):
    rows = session.query(Project).filter_by(owner_id=user.id).order_by(Project.updated_at.desc()).all()
    return [{"id":p.id,"name":p.name,"content":p.content,"updated_at":p.updated_at.isoformat()} for p in rows]

@app.post("/api/projects")
def create_project(data: ProjectIn, user: User = Depends(current_user), session: Session = Depends(db)):
    p = Project(owner_id=user.id, name=data.name, content=data.content)
    session.add(p); session.commit(); session.refresh(p)
    return {"id":p.id,"name":p.name}

@app.put("/api/projects/{project_id}")
def update_project(project_id: int, data: ProjectIn, user: User = Depends(current_user), session: Session = Depends(db)):
    p = session.query(Project).filter_by(id=project_id, owner_id=user.id).first()
    if not p: raise HTTPException(404, "المشروع غير موجود")
    p.name=data.name; p.content=data.content; session.commit()
    return {"ok":True}

@app.delete("/api/projects/{project_id}")
def delete_project(project_id: int, user: User = Depends(current_user), session: Session = Depends(db)):
    p = session.query(Project).filter_by(id=project_id, owner_id=user.id).first()
    if not p: raise HTTPException(404, "المشروع غير موجود")
    session.delete(p); session.commit()
    return {"ok":True}
