import aiomysql
import os
import uuid
import secrets
import logging
from dotenv import load_dotenv

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

async def log_usage(cpu, ram, disk, connections):
    await execute_query(
        "INSERT INTO usage_logs (cpu_percent, ram_percent, disk_percent, active_connections) VALUES (%s, %s, %s, %s)",
        (cpu, ram, disk, connections)
    )

async def log_deployment(project, status, trigger, output):
    await execute_query(
        "INSERT INTO deployment_history (project_name, status, triggered_by, output_log) VALUES (%s, %s, %s, %s)",
        (project, status, trigger, output)
    )

async def get_recent_usage(limit=10):
    return await execute_query(
        "SELECT * FROM usage_logs ORDER BY timestamp DESC LIMIT %s", 
        (limit,), 
        fetch_all=True
    )

async def get_deployments(limit=5):
    return await execute_query(
        "SELECT * FROM deployment_history ORDER BY timestamp DESC LIMIT %s", 
        (limit,), 
        fetch_all=True
    )

async def get_project_by_uuid(uuid):
    return await execute_query(
        "SELECT * FROM webhook_projects WHERE webhook_uuid = %s", 
        (uuid,), 
        fetch_one=True
    )

async def get_all_projects():
    return await execute_query(
        "SELECT * FROM webhook_projects ORDER BY id DESC", 
        fetch_all=True
    )

async def create_new_project(name, repo_url, branch, tech_stack, subdomain, cloudflare_id, nginx_port):
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

async def delete_project(uuid):
    await execute_query(
        "DELETE FROM webhook_projects WHERE webhook_uuid = %s", 
        (uuid,)
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