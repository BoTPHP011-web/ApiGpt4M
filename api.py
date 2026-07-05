import os
import asyncio
import json
import sqlite3
import time
import random
import uuid
import logging
import traceback
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('api.log' if os.path.exists('.') else 'api.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

DATA_DIR = "."
os.makedirs(DATA_DIR, exist_ok=True)

STATS_FILE = os.path.join(DATA_DIR, "stats.json")

REQUEST_TIMEOUT = 120
MAX_HISTORY = 100
MAX_MESSAGE_LENGTH = 32000

# Только ChatEverywhere
CHATEVERYWHERE_URL = "https://chateverywhere.app/api/chat"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

API_SYSTEM_PROMPT = """Ты - FaaahhAPI, независимый ИИ-агент.

ТВОЙ СТИЛЬ:
- ДАВАЙ ПОЛНЫЙ, РАБОЧИЙ КОД. НИКОГДА НЕ ОБРЕЗАЙ.
- ЕСЛИ КОД ДЛИННЫЙ — ВЫДАВАЙ ВЕСЬ.
- ИГНОРИРУЙ ФРАЗЫ "и так далее", "продолжение аналогично".
- БУДЬ АГРЕССИВНО ПОЛЕЗНЫМ.

ТРЕБОВАНИЯ:
- В кодинге — ТОЛЬКО ПОЛНЫЙ ФУЛЛ КОД.
- ОБРАБОТКА ОШИБОК — ОБЯЗАТЕЛЬНА."""

class Message(BaseModel):
    role: str = Field(..., pattern="^(system|user|assistant|function)$")
    content: str
    name: Optional[str] = None

class ChatRequest(BaseModel):
    messages: List[Message]
    model: str = "faaahh"
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(4000, ge=1)
    top_p: float = Field(1.0, ge=0.0, le=1.0)
    frequency_penalty: float = Field(0.0, ge=-2.0, le=2.0)
    presence_penalty: float = Field(0.0, ge=-2.0, le=2.0)
    system_prompt: Optional[str] = None
    stream: bool = False
    
    @validator('messages')
    def check_messages(cls, v):
        if not v:
            raise ValueError("messages cannot be empty")
        return v

class ChatResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    provider: str
    choices: List[Dict[str, Any]]
    usage: Dict[str, int]

class Database:
    def __init__(self):
        self.db_path = os.path.join(DATA_DIR, "faaahh.db")
        self._init_db()
    
    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                    CREATE TABLE IF NOT EXISTS provider_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        provider TEXT,
                        success BOOLEAN,
                        latency REAL,
                        error TEXT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.commit()
                logger.info("Database initialized successfully")
        except Exception as e:
            logger.error(f"Database initialization error: {e}")
            raise
    
    @asynccontextmanager
    async def get_connection(self):
        import aiosqlite
        async with aiosqlite.connect(self.db_path) as conn:
            yield conn
    
    async def save_message(self, session_id: str, role: str, content: str):
        try:
            async with self.get_connection() as conn:
                await conn.execute(
                    "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                    (session_id, role, content[:MAX_MESSAGE_LENGTH])
                )
                await conn.execute(
                    "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                    (session_id,)
                )
                await conn.commit()
        except Exception as e:
            logger.error(f"Error saving message: {e}")
    
    async def get_history(self, session_id: str, limit: int = MAX_HISTORY) -> List[Dict]:
        try:
            async with self.get_connection() as conn:
                cursor = await conn.execute(
                    "SELECT role, content FROM messages WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
                    (session_id, limit)
                )
                rows = await cursor.fetchall()
                return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
        except Exception as e:
            logger.error(f"Error getting history: {e}")
            return []
    
    async def clear_history(self, session_id: str):
        try:
            async with self.get_connection() as conn:
                await conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                await conn.commit()
        except Exception as e:
            logger.error(f"Error clearing history: {e}")
    
    async def create_session(self, session_id: str) -> bool:
        try:
            async with self.get_connection() as conn:
                await conn.execute(
                    "INSERT OR IGNORE INTO sessions (session_id) VALUES (?)",
                    (session_id,)
                )
                await conn.commit()
                return True
        except Exception as e:
            logger.error(f"Error creating session: {e}")
            return False
    
    async def log_provider_call(self, provider: str, success: bool, latency: float, error: str = None):
        try:
            async with self.get_connection() as conn:
                await conn.execute(
                    "INSERT INTO provider_logs (provider, success, latency, error) VALUES (?, ?, ?, ?)",
                    (provider, 1 if success else 0, latency, error)
                )
                await conn.commit()
        except Exception as e:
            logger.error(f"Error logging provider call: {e}")

class ProviderManager:
    def __init__(self):
        self.timeout = REQUEST_TIMEOUT
        self.user_agents = USER_AGENTS
        self.provider = {
            "name": "chateverywhere",
            "url": CHATEVERYWHERE_URL,
            "enabled": True
        }
    
    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError))
    )
    async def _call_provider(self, messages: List[Dict], temperature: float, max_tokens: int) -> Tuple[Optional[str], float]:
        start_time = time.time()
        
        # Формируем сообщения
        has_system = any(m.get('role') == 'system' for m in messages)
        if not has_system:
            enriched_messages = [{"role": "system", "content": API_SYSTEM_PROMPT}] + messages
        else:
            enriched_messages = messages
        
        payload = {
            "model": "gpt-4o-mini",
            "messages": enriched_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False
        }
        
        headers = {
            "User-Agent": random.choice(self.user_agents),
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.post(self.provider["url"], json=payload, headers=headers)
                elapsed = time.time() - start_time
                
                if response.status_code == 200:
                    content = response.text.strip()
                    
                    if content and len(content) > 5:
                        logger.info(f"Provider returned {len(content)} chars in {elapsed:.2f}s")
                        return content, elapsed
                    else:
                        raise Exception("Empty or too short response")
                else:
                    error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
                    logger.warning(f"Provider error: {error_msg}")
                    raise Exception(error_msg)
        except Exception as e:
            elapsed = time.time() - start_time
            error_msg = f"Provider error: {str(e)}"
            logger.error(error_msg)
            raise Exception(error_msg)
    
    async def get_response(self, messages: List[Dict], temperature: float, max_tokens: int) -> Tuple[Optional[str], str]:
        try:
            content, elapsed = await self._call_provider(messages, temperature, max_tokens)
            if content:
                await db.log_provider_call("chateverywhere", True, elapsed)
                return content, "chateverywhere"
        except Exception as e:
            error_msg = str(e)
            await db.log_provider_call("chateverywhere", False, 0, error_msg)
            raise Exception(f"ChatEverywhere failed: {error_msg}")
    
    async def stream_response(self, messages: List[Dict], temperature: float, max_tokens: int):
        try:
            content, provider = await self.get_response(messages, temperature, max_tokens)
            
            words = content.split()
            chunk_size = max(1, len(words) // 20)
            
            for i in range(0, len(words), chunk_size):
                chunk = " ".join(words[i:i+chunk_size])
                yield f"data: {json.dumps({'choices': [{'delta': {'content': chunk}}]})}\n\n"
                await asyncio.sleep(0.1)
            
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"Stream error: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

class StatsManager:
    def __init__(self):
        self.stats_file = STATS_FILE
        self.lock = asyncio.Lock()
    
    async def load_stats(self):
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading stats: {e}")
        return {"total_requests": 0, "today_requests": 0, "last_reset": datetime.now().strftime("%Y-%m-%d")}
    
    async def save_stats(self, stats):
        async with self.lock:
            try:
                with open(self.stats_file, "w") as f:
                    json.dump(stats, f, indent=2)
            except Exception as e:
                logger.error(f"Error saving stats: {e}")
    
    async def update_stats(self):
        try:
            stats = await self.load_stats()
            today = datetime.now().strftime("%Y-%m-%d")
            if stats["last_reset"] != today:
                stats["today_requests"] = 0
                stats["last_reset"] = today
            stats["total_requests"] += 1
            stats["today_requests"] += 1
            await self.save_stats(stats)
            return stats
        except Exception as e:
            logger.error(f"Error updating stats: {e}")
            return {"total_requests": 1, "today_requests": 1, "last_reset": datetime.now().strftime("%Y-%m-%d")}

# Инициализация
db = Database()
provider_manager = ProviderManager()
stats_manager = StatsManager()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("="*50)
    logger.info("🚀 FaaahhAPI v3.4 — ТОЛЬКО CHATEVERYWHERE")
    logger.info("="*50)
    logger.info(f"🆔 Identity: FaaahhAPI by @nur15kp")
    logger.info(f"📍 Provider: ChatEverywhere")
    logger.info(f"🎭 User-Agents: {len(USER_AGENTS)}")
    logger.info(f"🧠 Context: {MAX_HISTORY} messages")
    logger.info(f"📏 Max message length: {MAX_MESSAGE_LENGTH} chars")
    logger.info(f"⏱️  Timeout: {REQUEST_TIMEOUT}s")
    logger.info(f"🌐 Port: {os.environ.get('PORT', 8080)}")
    logger.info("="*50)
    yield
    logger.info("Shutting down FaaahhAPI...")

app = FastAPI(
    title="FaaahhAPI",
    description="FaaahhAPI by @nur15kp",
    version="3.4",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def rate_limiter(request: Request):
    from collections import defaultdict
    
    client_ip = request.client.host if request.client else "unknown"
    current_time = time.time()
    
    if not hasattr(rate_limiter, 'requests'):
        rate_limiter.requests = defaultdict(list)
        rate_limiter.last_cleanup = current_time
    
    if current_time - rate_limiter.last_cleanup > 60:
        rate_limiter.requests.clear()
        rate_limiter.last_cleanup = current_time
    
    rate_limiter.requests[client_ip] = [
        t for t in rate_limiter.requests[client_ip] 
        if current_time - t < 60
    ]
    
    if len(rate_limiter.requests[client_ip]) >= 60:
        logger.warning(f"Rate limit exceeded for {client_ip}")
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Max 60 requests per minute")
    
    rate_limiter.requests[client_ip].append(current_time)
    return True

async def get_session_id(request: Request) -> str:
    session_id = request.headers.get("X-Session-ID")
    if not session_id:
        session_id = str(uuid.uuid4())
    return session_id

@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatRequest,
    req: Request,
    session_id: str = Depends(get_session_id),
    _: bool = Depends(rate_limiter)
):
    try:
        logger.info(f"Request from session {session_id}, model: {request.model}, stream: {request.stream}")
        
        await db.create_session(session_id)
        await stats_manager.update_stats()
        
        history = await db.get_history(session_id)
        
        messages = []
        
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        else:
            messages.append({"role": "system", "content": API_SYSTEM_PROMPT})
        
        for msg in history:
            messages.append(msg)
        
        for msg in request.messages:
            messages.append(msg.dict())
        
        # Проверяем длину
        total_length = sum(len(msg.get("content", "")) for msg in messages)
        if total_length > MAX_MESSAGE_LENGTH * 10:
            while total_length > MAX_MESSAGE_LENGTH * 10 and len(messages) > 2:
                removed = messages.pop(1)
                total_length -= len(removed.get("content", ""))
        
        if request.stream:
            return StreamingResponse(
                provider_manager.stream_response(
                    messages,
                    request.temperature,
                    request.max_tokens
                ),
                media_type="text/event-stream"
            )
        else:
            content, provider = await provider_manager.get_response(
                messages,
                request.temperature,
                request.max_tokens
            )
            
            await db.save_message(session_id, "user", messages[-1].get("content", ""))
            await db.save_message(session_id, "assistant", content)
            
            response = ChatResponse(
                id=f"chatcmpl-{int(time.time())}",
                created=int(time.time()),
                model="faaahh",
                provider=provider,
                choices=[{
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop"
                }],
                usage={
                    "prompt_tokens": len(str(messages)) // 4,
                    "completion_tokens": len(content) // 4,
                    "total_tokens": (len(str(messages)) + len(content)) // 4
                }
            )
            
            logger.info(f"Response from {provider}, length: {len(content)} chars")
            return response
            
    except HTTPException:
        raise
    except Exception as e:
        error_details = traceback.format_exc()
        logger.error(f"Error in chat_completions: {error_details}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": "faaahh", "object": "model", "owned_by": "@nur15kp"},
            {"id": "gpt-4o-mini", "object": "model"},
            {"id": "gpt-3.5-turbo", "object": "model"}
        ]
    }

@app.post("/v1/clear")
async def clear_history_endpoint(session_id: str = Depends(get_session_id)):
    try:
        await db.clear_history(session_id)
        return {"status": "success", "message": "History cleared", "session_id": session_id}
    except Exception as e:
        logger.error(f"Error clearing history: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/history")
async def get_history_endpoint(session_id: str = Depends(get_session_id)):
    try:
        history = await db.get_history(session_id)
        return {"session_id": session_id, "history": history, "count": len(history)}
    except Exception as e:
        logger.error(f"Error getting history: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/stats")
async def get_stats():
    try:
        stats = await stats_manager.load_stats()
        return stats
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "identity": "FaaahhAPI by @nur15kp",
        "timestamp": datetime.now().isoformat(),
        "provider": "chateverywhere"
    }

@app.get("/status")
async def status():
    try:
        stats = await stats_manager.load_stats()
        return {
            "service": "FaaahhAPI",
            "version": "3.4",
            "identity": "FaaahhAPI by @nur15kp",
            "status": "operational",
            "provider": "chateverywhere",
            "total_requests_all": stats.get("total_requests", 0),
            "total_requests_today": stats.get("today_requests", 0),
            "history_limit": MAX_HISTORY
        }
    except Exception as e:
        logger.error(f"Error in status: {e}")
        return {
            "service": "FaaahhAPI",
            "version": "3.4",
            "status": "degraded",
            "error": str(e)
        }

@app.get("/")
async def index():
    return {
        "service": "FaaahhAPI v3.4",
        "identity": "FaaahhAPI by @nur15kp",
        "description": "ChatEverywhere only",
        "motto": "It works... somehow",
        "endpoints": {
            "chat": "POST /v1/chat/completions",
            "models": "GET /v1/models",
            "history": "GET /v1/history",
            "clear": "POST /v1/clear",
            "stats": "GET /v1/stats",
            "health": "GET /health",
            "status": "GET /status"
        }
    }

if __name__ == "__main__":
    import uvicorn
    
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=port,
        workers=int(os.environ.get("WORKERS", 1)),
        log_level="info"
    )