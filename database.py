from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, Boolean, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_async_engine(DATABASE_URL, echo=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    is_premium = Column(Boolean, default=False)
    max_servers = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)

class Server(Base):
    __tablename__ = "servers"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default="")
    language = Column(String(20), default="python")
    owner_id = Column(Integer, nullable=False)
    container_id = Column(String(100), nullable=True)
    status = Column(String(20), default="stopped")
    port = Column(Integer, unique=True, nullable=True)
    cpu_usage = Column(String(10), default="0%")
    ram_usage = Column(String(10), default="0MB")
    created_at = Column(DateTime, default=datetime.utcnow)

class FailedLogin(Base):
    __tablename__ = "failed_logins"
    id = Column(Integer, primary_key=True)
    ip = Column(String(45), nullable=False)
    username_attempted = Column(String(50))
    user_agent = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)

class LoginLog(Base):
    __tablename__ = "login_logs"
    id = Column(Integer, primary_key=True)
    username = Column(String(50))
    ip = Column(String(45))
    user_agent = Column(Text)
    success = Column(Boolean, default=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
