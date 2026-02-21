import aiomysql
import os
import uuid
import secrets
import string
import logging
from dotenv import load_dotenv
import hmac
from datetime import datetime, timezone

load_dotenv()

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

DB_POOL = None

try:
    DB_CONFIG = {
        'host': os.environ['DB_HOST'],
        'port': int(os.environ.get('DB_PORT', 3306)),
        'user': os.environ['DB_USER'],
        'password': os.environ['DB_PASSWORD'],
        'db': os.environ['DB_NAME'],
        'autocommit': True,
        'minsize': 1,
        'maxsize': 10
    }
except KeyError as e:
    raise RuntimeError(f"Missing required environment variable: {e}")

async def init_db():
    global DB_POOL
    try:
        if DB_POOL is None:
            DB_POOL = await aiomysql.create_pool(**DB_CONFIG)
    except Exception as e:
        logger.critical(f"Failed to connect to database: {e}")
        raise e

async def close_db():
    global DB_POOL
    if DB_POOL:
        DB_POOL.close()
        await DB_POOL.wait_closed()

async def execute_query(query, params=(), fetch_one=False, fetch_all=False):
    global DB_POOL
    if not DB_POOL:
        logger.error("Database pool is not initialized!")
        return None

    try:
        async with DB_POOL.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(query, params)
                
                if fetch_one:
                    return await cursor.fetchone()
                if fetch_all:
                    return await cursor.fetchall()
                return cursor.lastrowid
                
    except aiomysql.Error as e:
        logger.error(f"Database Query Error: {e} | Query: {query}")
        return None
    except Exception as e:
        logger.error(f"Unexpected Error: {e}")
        return None

async def log_system_resources(cpu, ram_p, ram_rem, ram_tot, disk_p, disk_rem, i_used, i_tot, conn):
    query = """
        INSERT INTO system_stats 
        (cpu, ram_percent, ram_remaining, ram_total, disk_percent, disk_remaining, inodes_used, inodes_total, connections) 
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    await execute_query(query, (cpu, ram_p, ram_rem, ram_tot, disk_p, disk_rem, i_used, i_tot, conn))

async def get_system_resources(limit=10):
    return await execute_query(
        "SELECT * FROM system_stats ORDER BY timestamp DESC LIMIT %s",
        (limit,),
        fetch_all=True
    )

async def get_recent_averages():
    query = "SELECT AVG(cpu), AVG(ram_percent) FROM system_stats WHERE timestamp >= NOW() - INTERVAL 3 MINUTE"
    return await execute_query(query, fetch_one=True)

async def get_recent_system_resources_with_averages():
    query = """
        SELECT main.*, 
               (SELECT AVG(cpu) FROM system_stats AS sub_cpu 
                WHERE sub_cpu.timestamp >= NOW() - INTERVAL 3 MINUTE) AS avg_cpu,
               (SELECT AVG(ram_percent) FROM system_stats AS sub_ram 
                WHERE sub_ram.timestamp >= NOW() - INTERVAL 3 MINUTE) AS avg_ram 
        FROM system_stats AS main
        ORDER BY main.timestamp DESC 
        LIMIT 1
    """
    return await execute_query(query, fetch_one=True)

async def log_deployment(project, status, trigger, output):
    await execute_query(
        "INSERT INTO deployment_history (project_name, status, triggered_by, output_log) VALUES (%s, %s, %s, %s)",
        (project, status, trigger, output)
    )

async def get_deployments(limit=5):
    return await execute_query(
        "SELECT * FROM deployment_history ORDER BY timestamp DESC LIMIT %s", 
        (limit,), 
        fetch_all=True
    )

async def get_webhook_project_by_uuid(uuid):
    return await execute_query(
        "SELECT * FROM webhook_projects WHERE webhook_uuid = %s", 
        (uuid,), 
        fetch_one=True
    )

async def get_all_webhook_projects():
    return await execute_query(
        "SELECT * FROM webhook_projects ORDER BY id DESC", 
        fetch_all=True
    )

async def create_new_webhook_project(name, repo_url, branch, tech_stack, subdomain, cloudflare_id, nginx_port):
    new_uuid = str(uuid.uuid4())
    new_secret = secrets.token_hex(16)
    deploy_path = f"/var/www/{new_uuid}"
    
    query = """INSERT INTO webhook_projects 
               (webhook_uuid, project_name, github_repository_url, branch, deploy_path, tech_stack, webhook_secret, subdomain, cloudflare_record_id, nginx_port) 
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
    
    params = (new_uuid, name, repo_url, branch, deploy_path, tech_stack, new_secret, subdomain, cloudflare_id, nginx_port)
    
    result = await execute_query(query, params)
    
    if result:
        return {
            "success": True,
            "webhook_uuid": new_uuid,
            "webhook_secret": new_secret,
            "deploy_path": deploy_path,
            "nginx_port": nginx_port
        }
    return {"success": False, "error": "Database insertion failed"}

async def delete_webhook_project(uuid):
    await execute_query(
        "DELETE FROM webhook_projects WHERE webhook_uuid = %s", 
        (uuid,)
    )

async def add_github_project(name, owner, owner_discord_id, owner_type, description, url_path, git_url, ssh_url, visibility, branch):
    new_uuid = str(uuid.uuid4())
    query = """INSERT INTO projects 
               (project_uuid, name, owner_login, owner_discord_id, owner_type, description, url_path, git_url, ssh_url, visibility, default_branch)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"""
    params = (new_uuid, name, owner, owner_discord_id, owner_type, description, url_path, git_url, ssh_url, visibility, branch)
    result = await execute_query(query, params)
    return new_uuid if result else None

async def get_github_project(project_uuid):
    return await execute_query(
        "SELECT * FROM projects WHERE project_uuid = %s",
        (project_uuid,),
        fetch_one=True
    )

async def get_all_github_projects():
    return await execute_query(
        "SELECT * FROM projects ORDER BY created_at DESC",
        fetch_all=True
    )

async def remove_github_project(project_uuid):
    return await execute_query(
        "DELETE FROM projects WHERE project_uuid = %s",
        (project_uuid,)
    )

async def get_all_attached_projects(owner_discord_id):
    return await execute_query(
        "SELECT * FROM projects WHERE owner_discord_id = %s",
        (owner_discord_id,),
        fetch_all=True
    )

async def add_user(discord_id, username=None):
    new_uuid = str(uuid.uuid4())
    return await execute_query(
        "INSERT INTO users (user_uuid, discord_id, username) VALUES (%s, %s, %s)",
        (new_uuid, discord_id, username)
    )

async def remove_user(discord_id):
    return await execute_query(
        "DELETE FROM users WHERE discord_id = %s",
        (discord_id,)
    )

async def get_user(discord_id):
    return await execute_query(
        "SELECT * FROM users WHERE discord_id = %s",
        (discord_id,),
        fetch_one=True
    )

async def add_auth_key(discord_id, app_name, expires_on=None):
    alphabet = string.ascii_letters + string.digits
    random_part = ''.join(secrets.choice(alphabet) for _ in range(58))
    new_secret = f"nydus_{random_part}"

    query = """INSERT INTO auth_keys 
               (auth_key_secret, discord_id, app_name, expires_on) 
               VALUES (%s, %s, %s, %s)"""
    params = (new_secret, discord_id, app_name, expires_on)
    result = await execute_query(query, params)

    if result:
        return {
            "success": True,
            "secret": new_secret,
            "app_name": app_name
        }
    return {"success": False}

async def get_auth_key(auth_key_secret):
    return await execute_query(
        "SELECT * FROM auth_keys WHERE auth_key_secret = %s AND deleted_at IS NULL",
        (auth_key_secret,),
        fetch_one=True
    )

async def get_user_auth_keys(discord_id):
    return await execute_query(
        "SELECT * FROM auth_keys WHERE discord_id = %s AND deleted_at IS NULL",
        (discord_id,),
        fetch_all=True
    )

async def update_auth_key_expiry(auth_key_secret, new_expiry):
    return await execute_query(
        "UPDATE auth_keys SET expires_on = %s WHERE auth_key_secret = %s",
        (new_expiry, auth_key_secret)
    )

async def soft_remove_auth_key(auth_key_secret):
    return await execute_query(
        "UPDATE auth_keys SET deleted_at = CURRENT_TIMESTAMP WHERE auth_key_secret = %s",
        (auth_key_secret,)
    )

async def validate_auth_key(auth_key_secret):
    try:
        key_data = await execute_query(
            "SELECT * FROM auth_keys WHERE auth_key_secret = %s AND deleted_at IS NULL",
            (auth_key_secret,),
            fetch_one=True
        )

        if not key_data:
            return {"valid": False, "reason": "Key not found or deleted", "data": None}

        if not hmac.compare_digest(key_data['auth_key_secret'], auth_key_secret):
            return {"valid": False, "reason": "Invalid key", "data": None}

        expires_on = key_data.get('expires_on')
        if expires_on:
            now_utc = datetime.now(timezone.utc)
            if expires_on.tzinfo is None:
                expires_on = expires_on.replace(tzinfo=timezone.utc)
            if now_utc > expires_on:
                return {"valid": False, "reason": "Key expired", "data": None}

        return {"valid": True, "reason": None, "data": key_data}

    except Exception:
        return {"valid": False, "reason": "Internal validation error", "data": None}

async def log_slash_command(
    discord_id: str,
    command_name: str,
    owner: str,
    repo: str,
    used_pat: bool,
    is_success: bool,
    error_message: str | None = None
):
    """
    Logs execution of a slash command into slash_command_logs.
    Never store sensitive values such as raw PAT.
    """
    return await execute_query(
        """
        INSERT INTO slash_command_logs
        (discord_id, command_name, owner, repo, used_pat, is_success, error_message)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(discord_id),
            command_name,
            owner,
            repo,
            int(used_pat),
            int(is_success),
            error_message
        )
    )