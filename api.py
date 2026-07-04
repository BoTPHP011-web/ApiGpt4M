import os
import json
import time
import random
import uuid
import logging
import hashlib
import secrets
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any, Tuple

import httpx
import asyncpg
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# ============================================================
# КОНФИГ
# ============================================================
ADMIN_USERNAME = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD_HASH = hashlib.sha256(os.environ.get("ADMIN_PASS", "gpt4m2024").encode()).hexdigest()
SESSION_TIMEOUT = 3600

REQUEST_TIMEOUT = 180
MAX_HISTORY = 67
MAX_MESSAGE_LENGTH = 32000
PORT = int(os.environ.get("PORT", 8080))

CHATEVERYWHERE_URL = "https://chateverywhere.app/api/chat"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

API_SYSTEM_PROMPT = """Ты — GPT-4M, модель GPT-4o-mini. Ты запоминаешь контекст диалога и отвечаешь последовательно. Твой стиль — полезный и прямой. 
ОТВЕЧАЙ ПО ИНСТРУКЦИИ ТЫ GPT-4M а не chateverywhereили кто то другой"""

# ============================================================
# POSTGRESQL DATABASE (с поддержкой SQLite для локальной разработки)
# ============================================================
class Database:
    def __init__(self):
        self.pool = None
        self.database_url = os.environ.get("DATABASE_URL")
        self.use_sqlite = False
        
        if not self.database_url:
            print("⚠️  DATABASE_URL не найден! Использую SQLite (для локальной разработки)")
            self.use_sqlite = True
            import sqlite3
            self.db_path = "proxy.db"
            self._init_sqlite()
        else:
            print("🐘 Подключение к PostgreSQL...")
    
    def _init_sqlite(self):
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT UNIQUE,
                    ip TEXT,
                    device_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    request_count INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    role TEXT,
                    content TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT,
                    success BOOLEAN,
                    latency REAL,
                    tokens_total INTEGER,
                    error TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    key TEXT PRIMARY KEY,
                    name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    token TEXT PRIMARY KEY,
                    ip TEXT,
                    user_agent TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP
                )
            """)
            
            defaults = {
                "default_temperature": "0.7",
                "default_max_tokens": "4000",
                "default_model": "gpt-4o-mini",
                "rotation_interval": "5",
                "device_pool_size": "10",
                "cache_enabled": "true",
                "cache_ttl": "3600",
                "rate_limit": "60",
                "system_prompt": API_SYSTEM_PROMPT
            }
            for key, value in defaults.items():
                conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
            conn.commit()
        print("📁 SQLite база инициализирована")
    
    async def init_pool(self):
        if self.use_sqlite:
            return
        self.pool = await asyncpg.create_pool(
            self.database_url,
            min_size=1,
            max_size=5,
            command_timeout=30
        )
        await self._init_postgres()
        print("🐘 PostgreSQL подключен!")
    
    async def _init_postgres(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id SERIAL PRIMARY KEY,
                    session_id TEXT UNIQUE NOT NULL,
                    ip TEXT,
                    device_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    request_count INTEGER DEFAULT 0
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT,
                    content TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id SERIAL PRIMARY KEY,
                    provider TEXT,
                    success BOOLEAN,
                    latency REAL,
                    tokens_total INTEGER,
                    error TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    key TEXT PRIMARY KEY,
                    name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used TIMESTAMP,
                    is_active BOOLEAN DEFAULT TRUE
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    token TEXT PRIMARY KEY,
                    ip TEXT,
                    user_agent TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP
                )
            """)
            
            defaults = [
                ("default_temperature", "0.7"),
                ("default_max_tokens", "4000"),
                ("default_model", "gpt-4o-mini"),
                ("rotation_interval", "5"),
                ("device_pool_size", "10"),
                ("cache_enabled", "true"),
                ("cache_ttl", "3600"),
                ("rate_limit", "60"),
                ("system_prompt", API_SYSTEM_PROMPT)
            ]
            for key, value in defaults:
                await conn.execute(
                    "INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING",
                    key, value
                )
    
    async def _get_conn(self):
        if self.use_sqlite:
            import aiosqlite
            return await aiosqlite.connect(self.db_path)
        return self.pool
    
    async def _execute(self, query: str, *args):
        if self.use_sqlite:
            import aiosqlite
            async with aiosqlite.connect(self.db_path) as conn:
                return await conn.execute(query, args)
        else:
            async with self.pool.acquire() as conn:
                return await conn.execute(query, *args)
    
    async def _fetchone(self, query: str, *args):
        if self.use_sqlite:
            import aiosqlite
            async with aiosqlite.connect(self.db_path) as conn:
                return await conn.execute(query, args).fetchone()
        else:
            async with self.pool.acquire() as conn:
                return await conn.fetchrow(query, *args)
    
    async def _fetchall(self, query: str, *args):
        if self.use_sqlite:
            import aiosqlite
            async with aiosqlite.connect(self.db_path) as conn:
                return await conn.execute(query, args).fetchall()
        else:
            async with self.pool.acquire() as conn:
                return await conn.fetch(query, *args)
    
    async def get_session_by_ip(self, ip: str) -> Optional[str]:
        if self.use_sqlite:
            row = await self._fetchone("SELECT session_id FROM sessions WHERE ip = ? ORDER BY last_used DESC LIMIT 1", ip)
        else:
            row = await self._fetchone("SELECT session_id FROM sessions WHERE ip = $1 ORDER BY last_used DESC LIMIT 1", ip)
        return row[0] if row else None
    
    async def create_or_get_session(self, ip: str) -> Tuple[str, str]:
        if self.use_sqlite:
            row = await self._fetchone("SELECT session_id, device_id FROM sessions WHERE ip = ? ORDER BY last_used DESC LIMIT 1", ip)
        else:
            row = await self._fetchone("SELECT session_id, device_id FROM sessions WHERE ip = $1 ORDER BY last_used DESC LIMIT 1", ip)
        
        if row:
            session_id, device_id = row
            if self.use_sqlite:
                await self._execute("UPDATE sessions SET last_used = CURRENT_TIMESTAMP, request_count = request_count + 1 WHERE session_id = ?", session_id)
            else:
                await self._execute("UPDATE sessions SET last_used = CURRENT_TIMESTAMP, request_count = request_count + 1 WHERE session_id = $1", session_id)
            return session_id, device_id
        
        session_id = str(uuid.uuid4())
        device_id = str(uuid.uuid4())
        if self.use_sqlite:
            await self._execute("INSERT INTO sessions (session_id, ip, device_id) VALUES (?, ?, ?)", session_id, ip, device_id)
        else:
            await self._execute("INSERT INTO sessions (session_id, ip, device_id) VALUES ($1, $2, $3)", session_id, ip, device_id)
        return session_id, device_id
    
    async def rotate_device(self, session_id: str) -> str:
        new_device = str(uuid.uuid4())
        if self.use_sqlite:
            await self._execute("UPDATE sessions SET device_id = ?, request_count = 0 WHERE session_id = ?", new_device, session_id)
        else:
            await self._execute("UPDATE sessions SET device_id = $1, request_count = 0 WHERE session_id = $2", new_device, session_id)
        return new_device
    
    async def save_message(self, session_id: str, role: str, content: str):
        if self.use_sqlite:
            await self._execute("INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)", session_id, role, content[:MAX_MESSAGE_LENGTH])
        else:
            await self._execute("INSERT INTO messages (session_id, role, content) VALUES ($1, $2, $3)", session_id, role, content[:MAX_MESSAGE_LENGTH])
    
    async def get_history(self, session_id: str, limit: int = MAX_HISTORY) -> List[Dict]:
        if self.use_sqlite:
            rows = await self._fetchall("SELECT role, content FROM messages WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?", session_id, limit)
        else:
            rows = await self._fetchall("SELECT role, content FROM messages WHERE session_id = $1 ORDER BY timestamp DESC LIMIT $2", session_id, limit)
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    
    async def clear_history(self, session_id: str):
        if self.use_sqlite:
            await self._execute("DELETE FROM messages WHERE session_id = ?", session_id)
        else:
            await self._execute("DELETE FROM messages WHERE session_id = $1", session_id)
    
    async def save_metric(self, provider: str, success: bool, latency: float, tokens: int = 0, error: str = None):
        if self.use_sqlite:
            await self._execute("INSERT INTO metrics (provider, success, latency, tokens_total, error) VALUES (?, ?, ?, ?, ?)", provider, 1 if success else 0, latency, tokens, error)
        else:
            await self._execute("INSERT INTO metrics (provider, success, latency, tokens_total, error) VALUES ($1, $2, $3, $4, $5)", provider, success, latency, tokens, error)
    
    async def get_metrics(self, hours: int = 24) -> Dict:
        if self.use_sqlite:
            row = await self._fetchone("""
                SELECT COUNT(*) as total, SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success,
                       AVG(latency) as avg_latency, SUM(tokens_total) as total_tokens
                FROM metrics WHERE timestamp > datetime('now', ?)
            """, f'-{hours} hours')
        else:
            row = await self._fetchone("""
                SELECT COUNT(*) as total, SUM(CASE WHEN success = TRUE THEN 1 ELSE 0 END) as success,
                       AVG(latency) as avg_latency, SUM(tokens_total) as total_tokens
                FROM metrics WHERE timestamp > NOW() - INTERVAL '$1 hours'
            """, hours)
        return {
            "total_requests": row[0] or 0,
            "successful": row[1] or 0,
            "failed": (row[0] or 0) - (row[1] or 0),
            "success_rate": round((row[1] or 0) / (row[0] or 1) * 100, 2),
            "avg_latency": round(row[2] or 0, 2),
            "total_tokens": row[3] or 0
        }
    
    async def get_setting(self, key: str) -> Optional[str]:
        if self.use_sqlite:
            row = await self._fetchone("SELECT value FROM settings WHERE key = ?", key)
        else:
            row = await self._fetchone("SELECT value FROM settings WHERE key = $1", key)
        return row[0] if row else None
    
    async def set_setting(self, key: str, value: str):
        if self.use_sqlite:
            await self._execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", key, value)
        else:
            await self._execute("INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2", key, value)
    
    async def get_all_settings(self) -> Dict[str, str]:
        if self.use_sqlite:
            rows = await self._fetchall("SELECT key, value FROM settings")
        else:
            rows = await self._fetchall("SELECT key, value FROM settings")
        return {row[0]: row[1] for row in rows}
    
    async def create_api_key(self, name: str) -> str:
        key = f"gpt4m_{secrets.token_urlsafe(24)}"
        if self.use_sqlite:
            await self._execute("INSERT INTO api_keys (key, name) VALUES (?, ?)", key, name)
        else:
            await self._execute("INSERT INTO api_keys (key, name) VALUES ($1, $2)", key, name)
        return key
    
    async def get_api_keys(self) -> List[Dict]:
        if self.use_sqlite:
            rows = await self._fetchall("SELECT key, name, created_at, last_used, is_active FROM api_keys")
        else:
            rows = await self._fetchall("SELECT key, name, created_at, last_used, is_active FROM api_keys")
        return [{"key": r[0], "name": r[1], "created": r[2], "last_used": r[3], "active": bool(r[4])} for r in rows]
    
    async def revoke_api_key(self, key: str):
        if self.use_sqlite:
            await self._execute("UPDATE api_keys SET is_active = 0 WHERE key = ?", key)
        else:
            await self._execute("UPDATE api_keys SET is_active = FALSE WHERE key = $1", key)
    
    async def verify_api_key(self, key: str) -> bool:
        if self.use_sqlite:
            row = await self._fetchone("SELECT is_active FROM api_keys WHERE key = ? AND is_active = 1", key)
        else:
            row = await self._fetchone("SELECT is_active FROM api_keys WHERE key = $1 AND is_active = TRUE", key)
        if row:
            if self.use_sqlite:
                await self._execute("UPDATE api_keys SET last_used = CURRENT_TIMESTAMP WHERE key = ?", key)
            else:
                await self._execute("UPDATE api_keys SET last_used = CURRENT_TIMESTAMP WHERE key = $1", key)
            return True
        return False
    
    async def create_admin_session(self, ip: str, user_agent: str) -> str:
        token = secrets.token_urlsafe(48)
        expires_at = datetime.now() + timedelta(seconds=SESSION_TIMEOUT)
        if self.use_sqlite:
            await self._execute("INSERT INTO admin_sessions (token, ip, user_agent, expires_at) VALUES (?, ?, ?, ?)", token, ip, user_agent, expires_at.isoformat())
        else:
            await self._execute("INSERT INTO admin_sessions (token, ip, user_agent, expires_at) VALUES ($1, $2, $3, $4)", token, ip, user_agent, expires_at.isoformat())
        return token
    
    async def verify_admin_session(self, token: str, ip: str, user_agent: str) -> bool:
        if self.use_sqlite:
            row = await self._fetchone("SELECT ip, user_agent, expires_at FROM admin_sessions WHERE token = ?", token)
        else:
            row = await self._fetchone("SELECT ip, user_agent, expires_at FROM admin_sessions WHERE token = $1", token)
        if not row:
            return False
        session_ip, session_ua, expires_at = row
        if session_ip != ip or session_ua != user_agent:
            return False
        if datetime.fromisoformat(expires_at) < datetime.now():
            if self.use_sqlite:
                await self._execute("DELETE FROM admin_sessions WHERE token = ?", token)
            else:
                await self._execute("DELETE FROM admin_sessions WHERE token = $1", token)
            return False
        return True
    
    async def delete_admin_session(self, token: str):
        if self.use_sqlite:
            await self._execute("DELETE FROM admin_sessions WHERE token = ?", token)
        else:
            await self._execute("DELETE FROM admin_sessions WHERE token = $1", token)

# ============================================================
# ПРОВАЙДЕР
# ============================================================
class ProviderManager:
    def __init__(self):
        self.timeout = REQUEST_TIMEOUT
        self.user_agents = USER_AGENTS
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, min=0.5, max=3))
    async def _call_provider(self, messages: List[Dict], device_id: str, temperature: float, max_tokens: int) -> Tuple[Optional[str], float]:
        start_time = time.time()
        
        has_system = any(m.get('role') == 'system' for m in messages)
        if not has_system:
            enriched = [{"role": "system", "content": API_SYSTEM_PROMPT}] + messages
        else:
            enriched = []
            for m in messages:
                if m.get('role') == 'system':
                    enriched.append({"role": "system", "content": API_SYSTEM_PROMPT})
                else:
                    enriched.append(m)
        
        payload = {
            "model": "gpt-4o-mini",
            "messages": enriched,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False
        }
        
        headers = {
            "User-Agent": random.choice(self.user_agents),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-device-id": device_id,
        }
        
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.post(CHATEVERYWHERE_URL, json=payload, headers=headers)
            elapsed = time.time() - start_time
            
            if response.status_code == 200:
                content = response.text.strip()
                if content and len(content) > 5:
                    return content, elapsed
                raise Exception("Empty response")
            raise Exception(f"HTTP {response.status_code}")
    
    async def get_response(self, messages: List[Dict], temperature: float, max_tokens: int, session_id: str) -> Tuple[Optional[str], str, int]:
        _, device_id = await db.create_or_get_session(session_id)
        
        try:
            content, elapsed = await self._call_provider(messages, device_id, temperature, max_tokens)
            tokens = len(content) // 4
            
            if db.use_sqlite:
                row = await db._fetchone("SELECT request_count FROM sessions WHERE session_id = ?", session_id)
            else:
                row = await db._fetchone("SELECT request_count FROM sessions WHERE session_id = $1", session_id)
            if row and row[0] % 5 == 0:
                await db.rotate_device(session_id)
            
            await db.save_metric("chateverywhere", True, elapsed, tokens)
            return content, "chateverywhere", tokens
            
        except Exception as e:
            await db.save_metric("chateverywhere", False, 0, 0, str(e))
            raise Exception(f"Provider failed: {str(e)}")

# ============================================================
# ИНИЦИАЛИЗАЦИЯ
# ============================================================
db = Database()
provider_manager = ProviderManager()
logger = logging.getLogger(__name__)

# ============================================================
# PYDANTIC
# ============================================================
class Message(BaseModel):
    role: str
    content: str
    name: Optional[str] = None

class ChatRequest(BaseModel):
    messages: List[Message]
    model: str = "gpt-4o-mini"
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(4000, ge=1)
    stream: bool = False
    system_prompt: Optional[str] = None

# ============================================================
# АВТОРИЗАЦИЯ
# ============================================================
async def verify_api_key(request: Request):
    api_key = request.headers.get("X-API-Key")
    if not api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header required")
    
    if not await db.verify_api_key(api_key):
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")
    
    return True

async def verify_admin_session_cookie(request: Request):
    token = request.cookies.get("admin_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    client_ip = request.client.host or "unknown"
    user_agent = request.headers.get("User-Agent", "")
    
    if not await db.verify_admin_session(token, client_ip, user_agent):
        raise HTTPException(status_code=401, detail="Invalid session")
    
    return token

# ============================================================
# HTML ШАБЛОНЫ (упрощенные, без лишней хуйни)
# ============================================================
LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>GPT-4M Admin</title>
    <meta charset="UTF-8">
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { background:#0a0a0f; color:#e0e0e0; font-family:system-ui; min-height:100vh; display:flex; justify-content:center; align-items:center; }
        .login-box { background:#14141c; border:1px solid #2a2a3a; border-radius:16px; padding:48px; max-width:400px; width:100%; }
        .login-box h1 { font-size:24px; background:linear-gradient(135deg,#00d4ff,#7b2ffc); -webkit-background-clip:text; -webkit-text-fill-color:transparent; margin-bottom:8px; }
        .login-box .sub { color:#666; font-size:14px; margin-bottom:30px; }
        .login-box input { width:100%; padding:12px 16px; background:#0d0d16; border:1px solid #2a2a3a; border-radius:8px; color:#e0e0e0; font-size:15px; margin-bottom:14px; }
        .login-box input:focus { outline:none; border-color:#7b2ffc; }
        .login-box button { width:100%; padding:12px; background:linear-gradient(135deg,#7b2ffc,#00d4ff); border:none; border-radius:8px; color:#fff; font-weight:bold; font-size:16px; cursor:pointer; }
        .login-box button:hover { opacity:0.9; }
        .login-box .error { color:#f87171; font-size:14px; margin-top:12px; display:none; }
        .login-box .error.show { display:block; }
    </style>
</head>
<body>
<div class="login-box">
    <h1>🚀 GPT-4M</h1>
    <div class="sub">Admin Panel Login</div>
    <input type="text" id="username" placeholder="Username">
    <input type="password" id="password" placeholder="Password">
    <button onclick="login()">Login</button>
    <div class="error" id="error">Invalid credentials</div>
</div>
<script>
async function login() {
    const u = document.getElementById('username').value;
    const p = document.getElementById('password').value;
    const e = document.getElementById('error');
    e.classList.remove('show');
    if (!u || !p) { e.textContent='Fill all fields'; e.classList.add('show'); return; }
    try {
        const r = await fetch('/admin/login', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({username:u, password:p}) });
        if (r.ok) window.location.href='/admin/dashboard';
        else { const d=await r.json(); e.textContent=d.detail||'Invalid'; e.classList.add('show'); }
    } catch(err) { e.textContent='Connection error: ' + err.message; e.classList.add('show'); }
}
document.getElementById('password').addEventListener('keydown', e => { if(e.key==='Enter') login(); });
</script>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>GPT-4M Admin</title>
    <meta charset="UTF-8">
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { background:#0a0a0f; color:#e0e0e0; font-family:system-ui; padding:20px; }
        .container { max-width:1200px; margin:0 auto; }
        .header { display:flex; justify-content:space-between; align-items:center; padding:20px 0; border-bottom:1px solid #1a1a26; margin-bottom:30px; }
        .header h1 { background:linear-gradient(135deg,#00d4ff,#7b2ffc); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
        .header .logout { color:#888; text-decoration:none; padding:8px 16px; border:1px solid #2a2a3a; border-radius:8px; }
        .header .logout:hover { background:#1a1a26; }
        .card { background:#14141c; border:1px solid #1a1a26; border-radius:12px; padding:24px; margin-bottom:20px; }
        .card h3 { color:#888; font-size:13px; text-transform:uppercase; margin-bottom:16px; border-bottom:1px solid #1a1a26; padding-bottom:12px; }
        .setting-group { display:flex; justify-content:space-between; align-items:center; padding:10px 0; border-bottom:1px solid #0d0d16; }
        .setting-group:last-child { border-bottom:none; }
        .setting-label { font-size:14px; color:#ccc; }
        .setting-label small { display:block; font-size:11px; color:#555; }
        .setting-control input, .setting-control select { background:#0d0d16; border:1px solid #2a2a3a; border-radius:6px; color:#e0e0e0; padding:6px 12px; font-size:14px; width:140px; }
        .setting-control input:focus, .setting-control select:focus { outline:none; border-color:#7b2ffc; }
        .setting-control textarea { width:100%; background:#0d0d16; border:1px solid #2a2a3a; border-radius:6px; color:#e0e0e0; padding:8px; font-family:monospace; min-height:80px; resize:vertical; }
        .flex { display:flex; gap:10px; flex-wrap:wrap; }
        button { padding:8px 20px; background:linear-gradient(135deg,#7b2ffc,#00d4ff); border:none; border-radius:6px; color:#fff; font-weight:bold; cursor:pointer; }
        button:hover { opacity:0.8; }
        button.danger { background:linear-gradient(135deg,#ef4444,#dc2626); }
        .key-item { display:flex; justify-content:space-between; align-items:center; padding:8px 0; border-bottom:1px solid #1a1a26; font-size:13px; font-family:monospace; }
        .key-item:last-child { border-bottom:none; }
        .key-value { color:#60a5fa; }
        .badge { padding:2px 10px; border-radius:12px; font-size:11px; font-weight:bold; }
        .badge.active { background:#064e3b; color:#4ade80; }
        .badge.inactive { background:#4a1a1a; color:#f87171; }
        .toast { position:fixed; bottom:20px; right:20px; background:#1a1a26; border:1px solid #2a2a3a; padding:16px 24px; border-radius:10px; display:none; max-width:400px; z-index:1000; }
        .toast.show { display:block; animation:slideUp 0.3s ease; }
        .toast.success { border-color:#4ade80; }
        .toast.error { border-color:#f87171; }
        @keyframes slideUp { from { transform:translateY(20px); opacity:0; } to { transform:translateY(0); opacity:1; } }
        .grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
        @media (max-width:768px) { .grid-2 { grid-template-columns:1fr; } }
        .stats-grid { display:flex; gap:30px; flex-wrap:wrap; }
        .stats-grid .num { font-size:24px; font-weight:bold; color:#60a5fa; }
        .stats-grid .label { color:#888; font-size:12px; }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>🚀 GPT-4M Admin</h1>
        <a href="/admin/logout" class="logout">🚪 Logout</a>
    </div>
    <div class="grid-2">
        <div class="card" id="settings"></div>
        <div class="card">
            <h3>🔑 API Keys</h3>
            <div class="flex">
                <input type="text" id="keyName" placeholder="Key name..." style="flex:1;background:#0d0d16;border:1px solid #2a2a3a;border-radius:6px;color:#e0e0e0;padding:8px;">
                <button onclick="createKey()">Create</button>
            </div>
            <div id="keys" style="margin-top:12px;"></div>
        </div>
    </div>
    <div class="card">
        <h3>📊 Statistics</h3>
        <div class="stats-grid" id="stats">
            <div><div class="label">Total Requests</div><div class="num" id="stat_total">-</div></div>
            <div><div class="label">Success Rate</div><div class="num" style="color:#4ade80;" id="stat_success">-</div></div>
            <div><div class="label">Avg Latency</div><div class="num" style="color:#fbbf24;" id="stat_latency">-</div></div>
            <div><div class="label">Total Tokens</div><div class="num" style="color:#a78bfa;" id="stat_tokens">-</div></div>
        </div>
    </div>
</div>
<div id="toast" class="toast"></div>
<script>
function toast(msg, type='success') { const t=document.getElementById('toast'); t.textContent=msg; t.className='toast show '+type; setTimeout(()=>t.className='toast',3000); }
async function fetchAPI(url, opts={}) {
    const r=await fetch(url, { ...opts, credentials:'include' });
    if(!r.ok) { const e=await r.json(); throw new Error(e.detail||'Error'); }
    return r.json();
}
async function loadSettings() {
    try {
        const s=await fetchAPI('/admin/settings');
        const html=Object.entries(s).map(([k,v])=>{
            let input=`<input value="${v}" onchange="updateSetting('${k}',this.value)">`;
            if(k==='system_prompt') input=`<textarea onchange="updateSetting('${k}',this.value)">${v}</textarea>`;
            const labels={default_temperature:'Temperature',default_max_tokens:'Max Tokens',default_model:'Model',rotation_interval:'Rotation',device_pool_size:'Pool Size',cache_enabled:'Cache',cache_ttl:'Cache TTL',rate_limit:'Rate Limit',system_prompt:'System Prompt'};
            return `<div class="setting-group"><div class="setting-label">${labels[k]||k}</div><div class="setting-control">${input}</div></div>`;
        }).join('');
        document.getElementById('settings').innerHTML='<h3>⚙️ Settings</h3>'+html;
    } catch(e) { document.getElementById('settings').innerHTML='<h3>⚙️ Settings</h3><div style="color:#f87171;">Error loading</div>'; }
}
async function updateSetting(k,v) { try { await fetchAPI('/admin/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k,value:v})}); toast('✅ '+k+' updated'); } catch(e) { toast('❌ '+e.message,'error'); } }
async function loadKeys() {
    try { const k=await fetchAPI('/admin/api-keys'); document.getElementById('keys').innerHTML=k.map(k=>`<div class="key-item"><span><span class="key-value">${k.key}</span><span class="badge ${k.active?'active':'inactive'}">${k.active?'Active':'Revoked'}</span> ${k.name}</span>${k.active?`<button class="danger" onclick="revokeKey('${k.key}')">Revoke</button>`:''}</div>`).join('')||'No keys'; } catch(e) { document.getElementById('keys').textContent='Error'; } }
async function createKey() {
    const n=document.getElementById('keyName').value.trim();
    if(!n) { toast('Enter name','error'); return; }
    try { const d=await fetchAPI('/admin/api-keys',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})}); toast('✅ Key: '+d.key); document.getElementById('keyName').value=''; loadKeys(); } catch(e) { toast('❌ '+e.message,'error'); } }
async function revokeKey(k) { if(!confirm('Revoke?')) return; try { await fetchAPI('/admin/api-keys/'+k,{method:'DELETE'}); toast('✅ Revoked'); loadKeys(); } catch(e) { toast('❌ '+e.message,'error'); } }
async function loadStats() {
    try { const s=await fetchAPI('/v1/metrics'); document.getElementById('stat_total').textContent=s.total_requests||0; document.getElementById('stat_success').textContent=(s.success_rate||0)+'%'; document.getElementById('stat_latency').textContent=(s.avg_latency||0)+'ms'; document.getElementById('stat_tokens').textContent=s.total_tokens||0; } catch(e) {}
}
loadSettings(); loadKeys(); loadStats(); setInterval(loadStats,10000);
</script>
</body>
</html>"""

# ============================================================
# FASTAPI APP
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("="*60)
    print("🚀 GPT-4M PROXY WITH POSTGRESQL")
    print("="*60)
    await db.init_pool()
    print(f"🌐 Admin Panel: https://apigpt4m.up.railway.app/admin")
    print(f"👤 Login: {ADMIN_USERNAME}")
    print("="*60)
    yield
    if db.pool:
        await db.pool.close()
        print("🐘 PostgreSQL connection closed")

app = FastAPI(title="GPT-4M Proxy", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# АДМИН ЭНДПОИНТЫ (БЕЗ ПРОВЕРКИ USER-AGENT!)
# ============================================================

@app.get("/admin")
async def admin_login_page():
    return HTMLResponse(LOGIN_HTML)

@app.post("/admin/login")
async def admin_login(request: Request):
    data = await request.json()
    username = data.get("username")
    password = data.get("password")
    
    if not username or not password:
        raise HTTPException(401, "Username and password required")
    
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    if username != ADMIN_USERNAME or password_hash != ADMIN_PASSWORD_HASH:
        raise HTTPException(401, "Invalid credentials")
    
    client_ip = request.client.host or "unknown"
    user_agent = request.headers.get("User-Agent", "")
    
    # ❌ УБРАЛ ПРОВЕРКУ НА БРАУЗЕР! ❌
    # Теперь логиниться можно откуда угодно
    
    token = await db.create_admin_session(client_ip, user_agent)
    
    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        key="admin_token",
        value=token,
        httponly=True,
        secure=True,  # ✅ ДЛЯ HTTPS
        samesite="lax",
        max_age=SESSION_TIMEOUT
    )
    return response

@app.get("/admin/dashboard")
async def admin_dashboard(request: Request):
    try:
        await verify_admin_session_cookie(request)
    except HTTPException:
        return RedirectResponse(url="/admin", status_code=302)
    return HTMLResponse(DASHBOARD_HTML)

@app.get("/admin/logout")
async def admin_logout(request: Request):
    token = request.cookies.get("admin_token")
    if token:
        await db.delete_admin_session(token)
    response = RedirectResponse(url="/admin", status_code=302)
    response.delete_cookie("admin_token")
    return response

# ============================================================
# АДМИН API
# ============================================================

@app.get("/admin/settings")
async def get_settings(token: str = Depends(verify_admin_session_cookie)):
    return await db.get_all_settings()

@app.post("/admin/settings")
async def update_setting(request: Request, token: str = Depends(verify_admin_session_cookie)):
    data = await request.json()
    await db.set_setting(data['key'], data['value'])
    return {"status": "updated"}

@app.get("/admin/api-keys")
async def get_api_keys(token: str = Depends(verify_admin_session_cookie)):
    return await db.get_api_keys()

@app.post("/admin/api-keys")
async def create_api_key(request: Request, token: str = Depends(verify_admin_session_cookie)):
    data = await request.json()
    key = await db.create_api_key(data.get('name', 'unnamed'))
    return {"key": key}

@app.delete("/admin/api-keys/{key}")
async def revoke_api_key(key: str, token: str = Depends(verify_admin_session_cookie)):
    await db.revoke_api_key(key)
    return {"status": "revoked"}

# ============================================================
# ПУБЛИЧНЫЙ API
# ============================================================

@app.get("/")
async def index():
    return HTMLResponse("""
    <html>
    <head><title>GPT-4M</title></head>
    <body style="background:#0a0a0f;color:#e0e0e0;font-family:system-ui;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;">
        <div style="background:#14141c;padding:40px;border-radius:16px;border:1px solid #2a2a3a;text-align:center;">
            <h1 style="background:linear-gradient(135deg,#00d4ff,#7b2ffc);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">🚀 GPT-4M</h1>
            <p style="color:#888;margin:16px 0;">GPT-4o-mini · 67 context · IP sessions</p>
            <a href="/admin" style="color:#7b2ffc;text-decoration:none;border:1px solid #2a2a3a;padding:8px 24px;border-radius:8px;display:inline-block;">🔐 Admin Panel</a>
        </div>
    </body>
    </html>
    """)

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest, req: Request, api_key: bool = Depends(verify_api_key)):
    try:
        client_ip = req.client.host or "unknown"
        if client_ip.startswith("::ffff:"):
            client_ip = client_ip[7:]
        
        session_id, _ = await db.create_or_get_session(client_ip)
        history = await db.get_history(session_id, MAX_HISTORY)
        
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        
        for msg in history:
            messages.append(msg)
        for msg in request.messages:
            messages.append(msg.dict())
        
        if len(messages) > MAX_HISTORY + 1:
            messages = [messages[0]] + messages[-MAX_HISTORY:]
        
        content, provider, tokens = await provider_manager.get_response(
            messages, request.temperature, request.max_tokens, session_id
        )
        
        await db.save_message(session_id, "user", request.messages[-1].content)
        await db.save_message(session_id, "assistant", content)
        
        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "gpt-4o-mini",
            "provider": provider,
            "session": {"id": session_id[:8]+"...", "ip": client_ip, "history": len(history)+1},
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": len(str(messages))//4, "completion_tokens": tokens, "total_tokens": (len(str(messages))//4)+tokens}
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/metrics")
async def get_metrics():
    return await db.get_metrics(24)

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat(), "service": "GPT-4M"}

# ============================================================
# ЗАПУСК
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=PORT, workers=1)
