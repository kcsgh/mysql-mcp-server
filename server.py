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
  - Every HTTP request must carry `Authorization: Bearer <AUTH_TOKEN>`.
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

import pymysql
import pymysql.cursors
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

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
def list_tables() -> list[str]:
    """List every table in the connected database."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SHOW TABLES")
        rows = cur.fetchall()
    # SHOW TABLES returns one column named Tables_in_<dbname>
    return [next(iter(row.values())) for row in rows]


@mcp.tool()
def describe_table(table_name: str) -> list[dict]:
    """Show the columns, types, keys, and nullability for one table."""
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
    safe_sql = validate_select(sql)
    limit = max(1, min(row_limit, MAX_ROW_LIMIT))

    has_limit = re.search(r"\bLIMIT\s+\d+", safe_sql, re.IGNORECASE) is not None
    query_to_run = safe_sql if has_limit else f"{safe_sql} LIMIT {limit}"

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(query_to_run)
        rows = cur.fetchall()

    return {"row_count": len(rows), "rows": rows}


# ---------------------------------------------------------------------------
# Bearer-token auth middleware
# ---------------------------------------------------------------------------

class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        expected = f"Bearer {AUTH_TOKEN}"
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, expected):
            return PlainTextResponse("Unauthorized", status_code=401)
        return await call_next(request)


app = mcp.streamable_http_app()
app.add_middleware(BearerAuthMiddleware)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
