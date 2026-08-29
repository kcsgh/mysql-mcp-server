# MySQL MCP Server

A remote MCP (Model Context Protocol) server that gives Claude read-only,
natural-language query access to a MySQL database — so Claude for Excel
(or claude.ai) can query the live database directly, instead of you
importing tables into a workbook via ODBC/Power Query first.

It exposes three tools:

- `list_tables()` — names of all tables
- `describe_table(table_name)` — columns/types for one table
- `run_query(sql, row_limit=200)` — run a `SELECT` and get rows back

Only `SELECT` (and `WITH ... SELECT`) statements are allowed; the server
rejects everything else (`INSERT`, `UPDATE`, `DELETE`, DDL, multiple
statements) before it ever reaches the database. Combine this with a
read-only database user (below) for defense in depth.

## Why this needs to be hosted somewhere public

Claude connects to custom connectors **from Anthropic's cloud
infrastructure**, not from your laptop. That means this server has to be
reachable on the public internet (or at least from Anthropic's IP
ranges) — running it on `localhost` and pointing Claude at it will not
work. The instructions below deploy it to Render's free tier, which
gives you a stable `https://...onrender.com` URL. (Fly.io or Railway
work the same way if you'd rather use one of those.)

Because it's public, the auth token and the read-only database user
described below aren't optional extras — they're what keeps this safe
to run.

---

## 1. Create a read-only MySQL user

Don't point this server at an account with write access. On the MySQL
server (run this once, from any MySQL client with admin rights):

```sql
CREATE USER 'readonly_user'@'%' IDENTIFIED BY 'a-strong-password-here';
GRANT SELECT ON car_rental_original.* TO 'readonly_user'@'%';
FLUSH PRIVILEGES;
```

If the database is an AWS RDS instance, you'll also need its security
group to allow inbound connections on port 3306 from wherever you
deploy the server (see step 3) — Render/Fly.io/Railway don't publish
fixed IP ranges on their free tiers, so the simplest option for a
teaching/personal setup is to allow the RDS instance's public access on
3306 and rely on the strong password + SSL + the fact that only
`SELECT` is possible for this user. If that's too permissive for your
comfort, host the MCP server on AWS instead (e.g. a small EC2/Fargate
service in the same VPC as the RDS instance), so the database itself
never needs to be internet-facing.

## 2. Generate an auth token

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Save this value — you'll set it as `AUTH_TOKEN` on the host and paste
it into Claude's connector settings later.

## 3. Run it locally first (Mac or Windows, via Docker)

This step works identically on both platforms since Docker abstracts
the OS. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/)
if you don't have it, then from this folder:

```bash
cp .env.example .env
# edit .env with your real MYSQL_HOST / USER / PASSWORD / DATABASE / AUTH_TOKEN

docker build -t mysql-mcp-server .
docker run --env-file .env -p 8000:8000 mysql-mcp-server
```

Quick sanity check in another terminal (should return `401 Unauthorized`
without a token, and a normal MCP response with one):

```bash
curl -i http://localhost:8000/mcp
curl -i http://localhost:8000/mcp \
  -H "Authorization: Bearer <your AUTH_TOKEN>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

This only proves the server runs — Claude still can't reach it here,
since `localhost` isn't public. Once it works locally, move to step 4.

## 4. Deploy to Render (free, public URL)

1. Push this folder to a GitHub repo (Render deploys from a repo, on
   either platform — this step is the same whether you did step 3 on
   Mac or Windows).
2. Go to [render.com](https://render.com), sign up/in, click **New +**
   → **Web Service**, and connect that repo. Render will detect the
   `Dockerfile` automatically.
3. Under **Environment**, add: `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`,
   `MYSQL_PASSWORD`, `MYSQL_DATABASE`, `MYSQL_SSL`, `AUTH_TOKEN` — same
   values as your local `.env`.
4. Deploy. Render gives you a URL like
   `https://mysql-mcp-server-xxxx.onrender.com`. Your MCP endpoint is
   that URL plus `/mcp`, e.g.
   `https://mysql-mcp-server-xxxx.onrender.com/mcp`.

(A `render.yaml` is included if you prefer Render's "Blueprint" deploy
flow instead of the manual steps above — either works.)

Note: Render's free tier spins the service down after periods of
inactivity and takes a few seconds to wake back up on the next request.
Fine for a teaching/personal setup; upgrade to a paid instance if you
want it always warm.

## 5. Register it as a custom connector in Claude

1. In Claude (claude.ai or the Excel add-in's settings), go to
   **Customize → Connectors**.
2. Click **+** → **Add custom connector**.
3. Paste the URL from step 4 (the one ending in `/mcp`).
4. Open **Advanced settings** and set the bearer token to your
   `AUTH_TOKEN` value.
5. Click **Add**.

Because connectors are tied to your Claude account rather than a
device, this one registration makes the connector available in Claude
for Excel on **both your Mac and Windows machines** — no need to repeat
this step per computer, just sign in with the same account.

## 6. Try it

In Claude for Excel, open the **+** menu below the chat input →
**Connectors**, make sure this connector is enabled for the
conversation, and ask something like:

> "Using the MySQL connector, list the tables in the database."
>
> "Which rental agency has the most cars in the `car` table?"

Claude will call `list_tables` / `describe_table` / `run_query` on its
own to answer, pulling only what it needs from the live database.

---

## Troubleshooting

- **`421 Invalid Host header`** — the MCP SDK's DNS-rebinding protection
  rejected the request's Host header. This shouldn't happen against a
  normal Render/Fly.io/Railway URL; if you put a custom reverse proxy in
  front of it, make sure it forwards the original Host header, or relax
  `transport_security` in `server.py` (`FastMCP(..., transport_security=
  TransportSecuritySettings(allowed_hosts=["*"]))`).
- **`401 Unauthorized`** — the bearer token in Claude's connector
  settings doesn't match `AUTH_TOKEN` on the host. Re-check both.
- **Connection to MySQL times out** — check the database's security
  group / firewall allows inbound connections from your host's IPs, and
  that `MYSQL_SSL` matches what the database requires.
