"""
Remote MCP server exposing read-only access to a MySQL database.

Designed to be registered as a Custom Connector in Claude (claude.ai
settings, or Claude for Excel/Word/PowerPoint), so Claude can query the
database directly with natural language instead of you pre-importing
data via ODBC/Power Query.

Tools exposed:
  - list_tables()                 -> names of all tables in the database
  - describe_table(table_name)    -> columns/types for one table
  - run_query(sql, row_limit)     -> run a read-only SELECT and get rows back

Security model:
  - MCP requests require the static AUTH_TOKEN or a verified OAuth access token.
  - Optional public OAuth discovery metadata enables ChatGPT account linking.
  - Only SELECT statements are allowed; anything else (INSERT, UPDATE,
    DELETE, DDL, multiple statements, etc.) is rejected before it
    reaches the database.
  - Use a dedicated MySQL user with SELECT-only grants (see README) as
    a second line of defense -- don't rely on the query filter alone.

Run locally:
    MYSQL_HOST=... MYSQL_USER=... MYSQL_PASSWORD=... MYSQL_DATABASE=... \
    AUTH_TOKEN=some-long-random-string \
    python server.py

Then deploy (see README.md) so the URL is reachable from the public
internet, and register that URL + token as a custom connector in Claude.
"""

import hmac
import os
import re
import sys
from urllib.parse import urlsplit

import jwt
import pymysql
import pymysql.cursors
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

# ---------------------------------------------------------------------------
# Configuration (all via environment variables -- see .env.example)
# ---------------------------------------------------------------------------

MYSQL_HOST = os.environ.get("MYSQL_HOST")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("MYSQL_USER")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE")
MYSQL_SSL = os.environ.get("MYSQL_SSL", "true").lower() in ("1", "true", "yes")

AUTH_TOKEN = os.environ.get("AUTH_TOKEN")


def oauth_configuration():
    """OAuth is opt-in; partial or unsafe configuration fails at startup."""
    names = ("OAUTH_ISSUER", "OAUTH_JWKS_URL", "OAUTH_RESOURCE_URL")
    values = {name: os.environ.get(name, "") for name in names}
    if not any(values.values()) and not os.environ.get("OAUTH_REQUIRED_SCOPES"):
        return None
    for name, value in values.items():
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or any(c.isspace() or c in '\\"' for c in value)):
            raise ValueError(f"{name} must be a complete HTTPS URL without credentials, query, or fragment")
    scopes = os.environ.get("OAUTH_REQUIRED_SCOPES", "mysql:read").split()
    if not scopes or any(any(ord(c) < 33 or ord(c) > 126 or c in '\\"' for c in scope) for scope in scopes):
        raise ValueError("OAUTH_REQUIRED_SCOPES must contain valid OAuth scope names")
    return {**values, "scopes": scopes}


OAUTH = oauth_configuration()
# Fetch only the operator-configured JWKS URL, never a URL supplied by a token.
# PyJWKClient caches the key set and refreshes it for key rotation.
jwks_client = jwt.PyJWKClient(OAUTH["OAUTH_JWKS_URL"], timeout=5) if OAUTH else None

DEFAULT_ROW_LIMIT = 200
MAX_ROW_LIMIT = 2000

REQUIRED_ENV = ["MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DATABASE", "AUTH_TOKEN"]
missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
if missing:
    print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)


def get_connection():
    ssl_args = {"ssl": {"ssl": True}} if MYSQL_SSL else {}
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=20,
        **ssl_args,
    )


# ---------------------------------------------------------------------------
# Query safety
# ---------------------------------------------------------------------------

_LEADING_COMMENT_RE = re.compile(r"^\s*(--[^\n]*\n|/\*.*?\*/\s*)*", re.DOTALL)
_SELECT_RE = re.compile(r"^\s*(SELECT|WITH)\s", re.IGNORECASE)
_FORBIDDEN_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|GRANT|REVOKE|"
    r"LOCK|UNLOCK|CALL|EXEC|EXECUTE|SET|LOAD_FILE|INTO\s+OUTFILE|INTO\s+DUMPFILE)\b",
    re.IGNORECASE,
)


def validate_select(sql: str) -> str:
    """Raise ValueError if `sql` is not a single, safe read-only statement."""
    stripped = _LEADING_COMMENT_RE.sub("", sql).strip()
    if not stripped:
        raise ValueError("Empty query.")
    # Reject stacked statements (a semicolon followed by more non-whitespace).
    body = stripped[:-1] if stripped.endswith(";") else stripped
    if ";" in body:
        raise ValueError("Multiple statements are not allowed -- send one SELECT at a time.")
    if not _SELECT_RE.match(body):
        raise ValueError("Only SELECT (or WITH ... SELECT) statements are allowed.")
    if _FORBIDDEN_RE.search(body):
        raise ValueError("Query contains a disallowed keyword for this read-only connector.")
    return body


# ---------------------------------------------------------------------------
# MCP server + tools
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "mysql-readonly",
    instructions=(
        "Read-only access to a MySQL database. Use list_tables to see what's "
        "available, describe_table to inspect columns before writing a query, "
        "and run_query for SELECT statements. Writes and schema changes are "
        "rejected by this server."
    ),
    stateless_http=True,
    # The MCP SDK's DNS-rebinding protection only trusts "localhost" by
    # default and returns 421 for any other Host header -- which rejects
    # every real deployment (Render, Fly.io, etc.) out of the box. Our
    # bearer-token middleware below is the real access control, so it's
    # safe to disable this check rather than hardcode a specific hostname.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def list_tables() -> dict:
    """List every table in the connected database."""
    print(">>> TOOL CALLED: list_tables", flush=True)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SHOW TABLES")
        rows = cur.fetchall()
    print(f">>> RAW TABLE ROWS: {rows}", flush=True)
    # SHOW TABLES returns one column named Tables_in_<dbname>
    print(f">>> RAW ROWS: {rows}", flush=True)
    print(f">>> ROW COUNT: {len(rows)}", flush=True)
    tables = [next(iter(row.values())) for row in rows]

    print(f">>> RETURNING TABLES: {tables}", flush=True)

    return {
        "database": os.getenv("MYSQL_DATABASE"),
        "table_count": len(tables),
        "tables": tables
           }


@mcp.tool()
def describe_table(table_name: str) -> list[dict]:
    """Show the columns, types, keys, and nullability for one table."""
    print(f">>> TOOL CALLED: describe_table | table={table_name}", flush=True)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SHOW TABLES")
        valid_tables = {next(iter(row.values())) for row in cur.fetchall()}
        if table_name not in valid_tables:
            raise ValueError(f"Unknown table {table_name!r}. Call list_tables() for valid names.")
        cur.execute(f"DESCRIBE `{table_name}`")
        return cur.fetchall()


@mcp.tool()
def run_query(sql: str, row_limit: int = DEFAULT_ROW_LIMIT) -> dict:
    """
    Run a read-only SELECT query and return the resulting rows.

    Only SELECT / WITH ... SELECT statements are permitted. A LIMIT is
    added automatically if your query doesn't already have one, capped
    at MAX_ROW_LIMIT rows.
    """
    print(f">>> TOOL CALLED: run_query | SQL: {sql}", flush=True)
    safe_sql = validate_select(sql)
    limit = max(1, min(row_limit, MAX_ROW_LIMIT))

    has_limit = re.search(r"\bLIMIT\s+\d+", safe_sql, re.IGNORECASE) is not None
    query_to_run = safe_sql if has_limit else f"{safe_sql} LIMIT {limit}"

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(query_to_run)
        rows = cur.fetchall()

    return {"row_count": len(rows), "rows": rows}


# ---------------------------------------------------------------------------
# Static Bearer and OAuth resource-server authentication
# ---------------------------------------------------------------------------


def verify_oauth_token(token: str) -> dict:
    """Verify an RS256 access token issued specifically for this MCP resource."""
    header = jwt.get_unverified_header(token)
    if header.get("alg") != "RS256":
        raise jwt.InvalidAlgorithmError("Only RS256 access tokens are supported")
    key = jwks_client.get_signing_key_from_jwt(token)
    return jwt.decode(
        token, key.key, algorithms=["RS256"],
        issuer=OAUTH["OAUTH_ISSUER"], audience=OAUTH["OAUTH_RESOURCE_URL"],
        options={"require": ["iss", "aud", "exp", "sub"]},
    )


async def oauth_metadata(request: Request):
    return JSONResponse({
        "resource": OAUTH["OAUTH_RESOURCE_URL"],
        "authorization_servers": [OAUTH["OAUTH_ISSUER"]],
        "scopes_supported": OAUTH["scopes"],
        "bearer_methods_supported": ["header"],
    })


def auth_error(status_code=401, error=None):
    challenge = "Bearer"
    if OAUTH:
        resource = urlsplit(OAUTH["OAUTH_RESOURCE_URL"])
        metadata_url = f"{resource.scheme}://{resource.netloc}/.well-known/oauth-protected-resource"
        challenge += f' resource_metadata="{metadata_url}", scope="{" ".join(OAUTH["scopes"])}"'
        if error:
            challenge += f', error="{error}"'
    return PlainTextResponse(
        "Forbidden" if status_code == 403 else "Unauthorized",
        status_code=status_code, headers={"WWW-Authenticate": challenge},
    )


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Only the exact discovery endpoints are public, and only for GET/HEAD.
        if OAUTH and request.url.path in metadata_paths and request.method in ("GET", "HEAD"):
            return await call_next(request)
        headers = request.headers.getlist("authorization")
        if len(headers) != 1:
            return auth_error()
        scheme, separator, token = headers[0].partition(" ")
        if not separator or scheme.lower() != "bearer" or not token or any(c.isspace() for c in token):
            return auth_error(error="invalid_token")
        if hmac.compare_digest(token.encode(), AUTH_TOKEN.encode()):
            return await call_next(request)
        if not OAUTH:
            return auth_error()
        try:
            # JWKS fetching is synchronous; keep network I/O off the event loop.
            claims = await run_in_threadpool(verify_oauth_token, token)
        except jwt.PyJWKClientConnectionError:
            return PlainTextResponse("OAuth key service unavailable", status_code=503,
                                     headers={"Retry-After": "5"})
        except (jwt.PyJWTError, ValueError, TypeError):
            return auth_error(error="invalid_token")
        scope = claims.get("scope", "")
        if not isinstance(scope, str) or not set(OAUTH["scopes"]).issubset(scope.split()):
            return auth_error(status_code=403, error="insufficient_scope")
        return await call_next(request)


app = mcp.streamable_http_app()
metadata_paths = set()
if OAUTH:
    metadata_paths = {
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource" + urlsplit(OAUTH["OAUTH_RESOURCE_URL"]).path.rstrip("/"),
    }
    for path in sorted(metadata_paths):
        app.routes.append(Route(path, oauth_metadata, methods=["GET", "HEAD"]))
app.add_middleware(BearerAuthMiddleware)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
