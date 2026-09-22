#!.venv/bin/python

import os
import re
import shlex
import subprocess
import httpx
import paramiko
import pymysql
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# sshtunnel 0.4.0 still references paramiko.DSSKey, which Paramiko 3+ removed.
if not hasattr(paramiko, "DSSKey"):
    class _RemovedDSSKey:
        @classmethod
        def from_private_key_file(cls, *args, **kwargs):
            raise paramiko.SSHException("DSS keys are not supported")

    paramiko.DSSKey = _RemovedDSSKey

from sshtunnel import SSHTunnelForwarder

load_dotenv()

mcp = FastMCP("totara")

TOTARA_URL = os.environ.get("TOTARA_URL", "http://localhost:8080").rstrip("/")
TOTARA_WS_TOKEN = os.environ.get("TOTARA_WS_TOKEN", "")

PROD_SSH_HOST = os.environ.get("PROD_SSH_HOST", "")
PROD_SSH_PORT = int(os.environ.get("PROD_SSH_PORT", "222"))
PROD_SSH_USER = os.environ.get("PROD_SSH_USER", "")
PROD_SSH_KEY_PATH = os.environ.get("PROD_SSH_KEY_PATH", "")  # e.g. ~/.ssh/id_ed25519; leave empty to use ssh-agent / default keys
PROD_SSH_PASSWORD = os.environ.get("PROD_SSH_PASSWORD", "")  # only if the server uses password auth

PROD_MYSQL_HOST = os.environ.get("PROD_MYSQL_HOST", "localhost")
PROD_MYSQL_PORT = int(os.environ.get("PROD_MYSQL_PORT", "3306"))
PROD_MYSQL_USER = os.environ.get("PROD_MYSQL_USER", "")
PROD_MYSQL_PASSWORD = os.environ.get("PROD_MYSQL_PASSWORD", "")

_READ_ONLY_SQL = re.compile(
    r"^\s*(SELECT|SHOW|DESCRIBE|DESC|EXPLAIN|WITH)\b",
    re.IGNORECASE | re.DOTALL,
)


async def make_mysql_query(sql_query: str, database: str) -> str:
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )

    containers = result.stdout.strip().split("\n")
    db_container = None
    for c in containers:
        if "moodle" in c.lower() or "totara" in c.lower():
            if "_db" in c.lower() or "db" in c.lower():
                db_container = c
                break

    if not db_container:
        raise Exception(
            "Could not find database container (looking for *moodle/totara*_db)"
        )

    user = os.environ.get("MYSQL_USER", "root")
    password = os.environ.get("MYSQL_PASSWORD", "root")

    cmd = ["docker", "exec", db_container, "mysql", "-u", user]
    if password:
        cmd.extend([f"-p{password}"])
    cmd.extend([database, "-e", sql_query])

    result = subprocess.run(cmd, capture_output=True, text=True, check=True)

    return result.stdout


async def execute_command_docker(cmd: str) -> str:
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )

    containers = result.stdout.strip().split("\n")
    app_container = None
    for c in containers:
        if "_docker" in c.lower():
            if "moodle" in c.lower() or "totara" in c.lower():
                app_container = c
                break

    if not app_container:
        raise Exception(
            "Could not find docker container (looking for *moodle/totara*_docker)"
        )

    result = subprocess.run(
        ["docker", "exec", "-w", "/var/www/totara", app_container, "sh", "-c", cmd],
        capture_output=True,
        text=True,
        check=True,
    )

    return result.stdout


def _flatten_ws_params(value, prefix=""):
    items = {}
    if isinstance(value, dict):
        for key, nested in value.items():
            next_prefix = f"{prefix}[{key}]" if prefix else str(key)
            items.update(_flatten_ws_params(nested, next_prefix))
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            items.update(_flatten_ws_params(nested, f"{prefix}[{index}]"))
    elif isinstance(value, bool):
        items[prefix] = 1 if value else 0
    elif value is not None:
        items[prefix] = value
    return items


def _webservice_error_message(payload: dict) -> str:
    message = payload.get("message") or payload.get("error") or "Totara web service error"
    errorcode = payload.get("errorcode")
    debuginfo = payload.get("debuginfo")
    parts = [message]
    if errorcode:
        parts.append(f"[{errorcode}]")
    if debuginfo:
        parts.append(f"({debuginfo})")
    return " ".join(parts)


async def call_webservice(wsfunction: str, params: dict | None = None):
    if not TOTARA_WS_TOKEN:
        raise ValueError("Set TOTARA_WS_TOKEN in .env before calling Totara web services")

    data = {
        "wstoken": TOTARA_WS_TOKEN,
        "wsfunction": wsfunction,
        "moodlewsrestformat": "json",
        **_flatten_ws_params(params or {}),
    }
    url = f"{TOTARA_URL}/webservice/rest/server.php"

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.post(url, data=data)

    try:
        payload = response.json()
    except ValueError as exc:
        response.raise_for_status()
        raise ValueError(f"Unexpected Totara response: {response.text[:500]}") from exc

    if isinstance(payload, dict) and (payload.get("exception") or payload.get("error")):
        raise ValueError(_webservice_error_message(payload))

    response.raise_for_status()
    return payload


@mcp.tool()
async def ping():
    """Check if the MCP server is alive"""
    return {"status": "ok", "message": "Totara MCP server is running"}


@mcp.tool()
async def queryDatabase(sqlQuery: str, database: str):
    """Execute queries against the current totara database"""
    return await make_mysql_query(sqlQuery, database)


def _assert_production_read_only(sql_query: str) -> None:
    if not _READ_ONLY_SQL.match(sql_query):
        raise ValueError(
            "Production queries must be read-only (SELECT, SHOW, DESCRIBE, EXPLAIN)"
        )


def _format_mysql_rows(cursor) -> str:
    if cursor.description is None:
        return f"{cursor.rowcount} rows affected"
    columns = [col[0] for col in cursor.description]
    rows = cursor.fetchall()
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join("" if value is None else str(value) for value in row))
    return "\n".join(lines)


def _load_ssh_private_key(path: str):
    expanded = os.path.expanduser(path)
    last_error = None
    for key_class in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return key_class.from_private_key_file(expanded)
        except (paramiko.SSHException, OSError) as exc:
            last_error = exc
    raise ValueError(f"Could not load SSH private key {expanded}: {last_error}")


def _production_ssh_tunnel() -> SSHTunnelForwarder:
    if PROD_SSH_HOST in ("", "CHANGE_ME") or PROD_SSH_USER in ("", "CHANGE_ME"):
        raise ValueError("Set PROD_SSH_HOST and PROD_SSH_USER in .env before querying production")

    tunnel_kwargs = {
        "ssh_address_or_host": (PROD_SSH_HOST, PROD_SSH_PORT),
        "ssh_username": PROD_SSH_USER,
        "remote_bind_address": (PROD_MYSQL_HOST, PROD_MYSQL_PORT),
        "local_bind_address": ("127.0.0.1", 0),
        "allow_agent": True,
    }
    if PROD_SSH_KEY_PATH:
        tunnel_kwargs["ssh_pkey"] = _load_ssh_private_key(PROD_SSH_KEY_PATH)
    if PROD_SSH_PASSWORD:
        tunnel_kwargs["ssh_password"] = PROD_SSH_PASSWORD
    return SSHTunnelForwarder(**tunnel_kwargs)


async def make_production_mysql_query(sql_query: str, database: str) -> str:
    _assert_production_read_only(sql_query)
    if PROD_MYSQL_USER in ("", "CHANGE_ME"):
        raise ValueError("Set PROD_MYSQL_* in .env before querying production")

    with _production_ssh_tunnel() as tunnel:
        conn = pymysql.connect(
            host="127.0.0.1",
            port=tunnel.local_bind_port,
            user=PROD_MYSQL_USER,
            password=PROD_MYSQL_PASSWORD,
            database=database,
            connect_timeout=10,
            read_timeout=60,
            charset="utf8mb4",
        )
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql_query)
                return _format_mysql_rows(cursor)
        finally:
            conn.close()


@mcp.tool()
async def queryProduction(sqlQuery: str, database: str):
    """Execute read-only queries against the production totara database via SSH tunnel"""
    return await make_production_mysql_query(sqlQuery, database)


@mcp.tool()
async def compileGrunt():
    """Compiles css and javascript with grunt cli"""
    return await execute_command_docker("grunt --force")


@mcp.tool()
async def createCourse(
    fullname: str,
    shortname: str,
    categoryid: int = 1,
    summary: str = "",
    courseFormat: str = "",
    visible: int | None = None,
    idnumber: str = "",
    enablecompletion: int | None = None,
    startdate: int | None = None,
):
    """Create a course on the Totara site via core_course_create_courses. Uses TOTARA_URL (default http://localhost:8080) and TOTARA_WS_TOKEN from .env."""
    course = {
        "fullname": fullname,
        "shortname": shortname,
        "categoryid": categoryid,
    }
    if summary:
        course["summary"] = summary
    if courseFormat:
        course["format"] = courseFormat
    if visible is not None:
        course["visible"] = visible
    if idnumber:
        course["idnumber"] = idnumber
    if enablecompletion is not None:
        course["enablecompletion"] = enablecompletion
    if startdate is not None:
        course["startdate"] = startdate

    created = await call_webservice("core_course_create_courses", {"courses": [course]})
    return created[0] if isinstance(created, list) and created else created


def _sanitize_admin_cli_script(script: str) -> str:
    name = script.strip()
    if name.endswith(".php"):
        name = name[:-4]
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise ValueError(f"Invalid admin CLI script name: {script}")
    return name


@mcp.tool()
async def runAdminCli(script: str, cliArgs: str = ""):
    """Run a Totara admin CLI PHP script from server/admin/cli/ (e.g. cron, upgrade, purge_caches). Use cliArgs for flags such as --help."""
    script_name = _sanitize_admin_cli_script(script)
    parts = ["php", f"server/admin/cli/{script_name}.php"]
    if cliArgs.strip():
        parts.extend(shlex.split(cliArgs))
    return await execute_command_docker(" ".join(shlex.quote(p) for p in parts))


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
