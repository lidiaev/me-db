import asyncio, base64, hashlib, html, json, logging, os, secrets, subprocess, sys, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

# ── Logging ───────────────────────────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                           "level": record.levelname, "msg": record.getMessage()})

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logging.root.handlers = [handler]
logging.root.setLevel(logging.INFO)
logger = logging.getLogger("mcp")

# ── Config ────────────────────────────────────────────────────────────────────

ME_DB_PATH      = Path(os.environ.get("ME_DB_PATH", "/mnt/me_db"))
BASE_URL        = os.environ.get("MCP_BASE_URL", "").rstrip("/")
BROWSER_URL     = os.environ.get("MCP_BROWSER_URL", BASE_URL).rstrip("/")
_MCP_STATE_PATH = Path(os.environ.get("MCP_STATE_PATH", "/mnt/mcp_state"))
MAX_DCR_CLIENTS = int(os.environ.get("MAX_DCR_CLIENTS", "10"))
MCP_OAUTH_SECRET = os.environ.get("MCP_OAUTH_SECRET", "")

# ── OAuth stores ─────────────────────────────────────────────────────────────

_CLIENTS_FILE = _MCP_STATE_PATH / "oauth_clients.json"

def _load_clients() -> dict:
    try:
        return json.loads(_CLIENTS_FILE.read_text()) if _CLIENTS_FILE.exists() else {}
    except Exception:
        return {}

def _save_clients(clients: dict):
    try:
        _CLIENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CLIENTS_FILE.write_text(json.dumps(clients))
    except Exception as e:
        logger.warning(f"Could not persist clients: {e}")

_clients        = _load_clients()
_codes          = {}
_access_tokens  = {}
_refresh_tokens = {}

# ── Helpers ───────────────────────────────────────────────────────────────────

mcp = FastMCP("me-db")

def full_path(path: str) -> Path:
    root = ME_DB_PATH.resolve()
    p = (root / path.lstrip("/")).resolve()
    if not p.is_relative_to(root):
        raise ToolError(f"Path traversal denied: {path}")
    return p

def valid_token(token: str | None) -> bool:
    if not token:
        return False
    exp = _access_tokens.get(token)
    if exp and time.time() < exp:
        return True
    if exp:
        del _access_tokens[token]
    return False

def www_auth() -> str:
    return f'Bearer resource_metadata="{BASE_URL}/.well-known/oauth-protected-resource"'

# ── Public endpoints ──────────────────────────────────────────────────────────

async def health(request: Request):
    return JSONResponse({"status": "ok"})

async def oauth_protected_resource(request: Request):
    return JSONResponse({
        "resource": f"{BASE_URL}/mcp",
        "authorization_servers": [BASE_URL],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp"],
    })

async def oauth_metadata(request: Request):
    return JSONResponse({
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BROWSER_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "registration_endpoint": f"{BASE_URL}/register",
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "response_types_supported": ["code"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["mcp"],
    })

async def oauth_register(request: Request):
    """DCR — Dynamic Client Registration (RFC 7591)."""
    while len(_clients) >= MAX_DCR_CLIENTS:
        _clients.pop(next(iter(_clients)), None)   # evict oldest registration
    try:
        body = await request.json()
    except Exception:
        body = {}
    client_id     = secrets.token_urlsafe(16)
    client_secret = secrets.token_urlsafe(32)
    _clients[client_id] = {"client_secret": client_secret, "redirect_uris": body.get("redirect_uris", [])}
    _save_clients(_clients)
    logger.info(f"DCR: registered client {client_id[:8]}...")
    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": ["authorization_code"],
        "token_endpoint_auth_method": "client_secret_post",
    }, status_code=201)

def _authorize_form(client_id, redirect_uri, challenge, state, error="") -> str:
    e = f'<p style="color:#c00">{html.escape(error)}</p>' if error else ""
    def h(v): return html.escape(v or "")
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:sans-serif;max-width:360px;margin:60px auto;padding:0 16px">
<h3>Authorize me-db</h3>{e}
<form method="post" action="/oauth/authorize">
<input type="hidden" name="client_id" value="{h(client_id)}">
<input type="hidden" name="redirect_uri" value="{h(redirect_uri)}">
<input type="hidden" name="code_challenge" value="{h(challenge)}">
<input type="hidden" name="state" value="{h(state)}">
<p>Setup secret:<br><input type="password" name="secret" autofocus style="width:100%;padding:8px"></p>
<button type="submit" style="padding:8px 16px">Authorize</button>
</form></body></html>"""

async def oauth_authorize(request: Request):
    """Authorization endpoint — gated by a pre-shared secret (no open auto-approve)."""
    if request.method == "GET":
        p = dict(request.query_params)
        return HTMLResponse(_authorize_form(p.get("client_id", ""), p.get("redirect_uri", ""),
                                            p.get("code_challenge", ""), p.get("state", "")))
    form         = await request.form()
    client_id    = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    challenge    = form.get("code_challenge", "")
    state        = form.get("state", "")
    if not MCP_OAUTH_SECRET or form.get("secret", "") != MCP_OAUTH_SECRET:
        return HTMLResponse(_authorize_form(client_id, redirect_uri, challenge, state,
                                            error="Wrong secret"), status_code=403)
    if client_id not in _clients:
        return JSONResponse({"error": "invalid_client"}, status_code=400)
    registered = _clients[client_id].get("redirect_uris", [])
    if redirect_uri not in registered:
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    code = secrets.token_urlsafe(32)
    _codes[code] = {"client_id": client_id, "code_challenge": challenge,
                    "redirect_uri": redirect_uri, "exp": time.time() + 300}
    return RedirectResponse(f"{redirect_uri}?code={code}&state={state}", status_code=302)

async def oauth_token(request: Request):
    """Token endpoint — authorization_code + PKCE, and refresh_token."""
    form       = await request.form()
    grant_type = form.get("grant_type", "")

    if grant_type == "authorization_code":
        code          = form.get("code", "")
        code_verifier = form.get("code_verifier", "")
        client_id     = form.get("client_id", "")
        entry = _codes.pop(code, None)
        if not entry or entry["exp"] < time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if entry["client_id"] != client_id:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        digest    = hashlib.sha256(code_verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if challenge != entry["code_challenge"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        token   = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        _access_tokens[token]    = time.time() + 3600
        _refresh_tokens[refresh] = {"exp": time.time() + 86400 * 30, "client_id": client_id}
        logger.info(f"token issued via auth_code for {client_id[:8]}...")
        return JSONResponse({"access_token": token, "token_type": "Bearer",
                             "expires_in": 3600, "refresh_token": refresh})

    if grant_type == "refresh_token":
        old_refresh    = form.get("refresh_token", "")
        client_id_form = form.get("client_id", "")
        entry = _refresh_tokens.pop(old_refresh, None)
        if not entry or entry["exp"] < time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if entry["client_id"] != client_id_form:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        token   = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        _access_tokens[token]    = time.time() + 3600
        _refresh_tokens[refresh] = {"exp": time.time() + 86400 * 30, "client_id": client_id_form}
        logger.info(f"token refreshed for {client_id_form[:8]}...")
        return JSONResponse({"access_token": token, "token_type": "Bearer",
                             "expires_in": 3600, "refresh_token": refresh})

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

# ── Auth middleware ───────────────────────────────────────────────────────────

PUBLIC_PATHS = {"/health", "/register", "/oauth/token", "/oauth/authorize"}

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS":
            return await call_next(request)
        if path in PUBLIC_PATHS or path.startswith("/.well-known/"):
            return await call_next(request)
        auth  = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else None
        if not valid_token(token):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"},
                                headers={"WWW-Authenticate": www_auth()})
        return await call_next(request)

# ── MCP tools ─────────────────────────────────────────────────────────────────

@mcp.tool
async def read_file(path: str) -> dict:
    """Read a file from the knowledge base."""
    fp = full_path(path)
    if not fp.exists() or fp.is_dir():
        raise ToolError(f"File not found: {path}")
    return {"content": fp.read_text(encoding="utf-8"), "path": path}

@mcp.tool
async def write_file(path: str, content: str) -> dict:
    """Write (overwrite) a file in the knowledge base."""
    fp = full_path(path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")
    logger.info(f"tool=write_file path={path}")
    return {"status": "ok", "path": path}

@mcp.tool
async def append_file(path: str, content: str) -> dict:
    """Append content to a file in the knowledge base."""
    fp = full_path(path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    existing = fp.read_text(encoding="utf-8") if fp.exists() else ""
    with open(fp, "a", encoding="utf-8") as f:
        f.write(("\n" if existing and not existing.endswith("\n") else "") + content)
    logger.info(f"tool=append_file path={path}")
    return {"status": "ok", "path": path}

@mcp.tool
async def delete_file(path: str) -> dict:
    """Delete a file from the knowledge base."""
    fp = full_path(path)
    if not fp.exists() or fp.is_dir():
        raise ToolError(f"File not found: {path}")
    fp.unlink()
    logger.info(f"tool=delete_file path={path}")
    return {"status": "ok", "path": path}

@mcp.tool
async def move_file(src: str, dst: str) -> dict:
    """Move or rename a file in the knowledge base."""
    fp_src = full_path(src)
    fp_dst = full_path(dst)
    if not fp_src.exists():
        raise ToolError(f"Source not found: {src}")
    fp_dst.parent.mkdir(parents=True, exist_ok=True)
    fp_src.rename(fp_dst)
    logger.info(f"tool=move_file src={src} dst={dst}")
    return {"status": "ok", "src": src, "dst": dst}

@mcp.tool
async def list_files(path: str = "") -> dict:
    """List files and directories in the knowledge base."""
    fp = full_path(path)
    if not fp.is_dir():
        raise ToolError(f"Not a directory: {path}")
    entries = []
    for entry in sorted(fp.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        if entry.name.startswith("."):
            continue
        try:
            entries.append({"name": entry.name, "type": "directory" if entry.is_dir() else "file",
                             "size": entry.stat().st_size})
        except OSError:
            continue
    return {"items": entries, "path": path or "/"}

@mcp.tool
async def search(query: str, path: str = "") -> dict:
    """Search for text in the knowledge base using ripgrep."""
    root = full_path(path)
    try:
        result = subprocess.run(["rg", "--json", "-i", "--max-count", "50", query, str(root)],
                                capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        raise ToolError("ripgrep not found")
    except subprocess.TimeoutExpired:
        raise ToolError("Search timed out")
    matches = []
    for line in result.stdout.splitlines():
        try:
            obj = json.loads(line)
            if obj.get("type") == "match":
                d = obj["data"]
                matches.append({"file": str(Path(d["path"]["text"]).relative_to(ME_DB_PATH)),
                                 "line": d["line_number"], "text": d["lines"]["text"].rstrip()})
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return {"matches": matches, "query": query}

# ── App assembly ──────────────────────────────────────────────────────────────

mcp_app = mcp.http_app()

async def _token_cleanup():
    while True:
        await asyncio.sleep(600)
        now = time.time()
        for key in [k for k, v in list(_codes.items())          if v.get("exp", 0) < now]:
            _codes.pop(key, None)
        for key in [k for k, v in list(_access_tokens.items())  if v < now]:
            _access_tokens.pop(key, None)
        for key in [k for k, v in list(_refresh_tokens.items()) if v["exp"] < now]:
            _refresh_tokens.pop(key, None)

@asynccontextmanager
async def lifespan(app):
    async with mcp_app.router.lifespan_context(app):
        task = asyncio.create_task(_token_cleanup())
        try:
            yield
        finally:
            task.cancel()

_starlette = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/health", health),
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
        Route("/.well-known/oauth-protected-resource/{path:path}", oauth_protected_resource),
        Route("/.well-known/oauth-authorization-server", oauth_metadata),
        Route("/register", oauth_register, methods=["POST"]),
        Route("/oauth/authorize", oauth_authorize, methods=["GET", "POST"]),
        Route("/oauth/token", oauth_token, methods=["POST"]),
        Mount("/", app=mcp_app),
    ],
)

app = CORSMiddleware(
    AuthMiddleware(_starlette),
    allow_origins=["https://claude.ai", "https://api.claude.ai", f"{BROWSER_URL}"],
    allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Mcp-Session-Id", "MCP-Protocol-Version"],
    allow_credentials=True,
    expose_headers=["Mcp-Session-Id", "WWW-Authenticate"],
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
