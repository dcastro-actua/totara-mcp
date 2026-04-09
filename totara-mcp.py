#!.venv/bin/python

from typing import Any
import os
import subprocess
import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("totara")


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


@mcp.tool()
async def ping():
    """Check if the MCP server is alive"""
    return {"status": "ok", "message": "Totara MCP server is running"}


@mcp.tool()
async def queryDatabase(sqlQuery: str, database: str):
    """Execute queries against the current totara database"""
    return await make_mysql_query(sqlQuery, database)

@mcp.tool()
async def compileGrunt():
    """Compiles css and javascript with grunt cli"""
    return await execute_command_docker("grunt --force")

def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
