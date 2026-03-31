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

                if cursor.lastrowid:
                    return cursor.lastrowid
                return cursor.rowcount

    except aiomysql.Error as e:
        logger.error(f"Database Query Error: {e} | Query: {query}")
        return None
    except Exception as e:
        logger.error(f"Unexpected Error: {e}")
        return None

async def log_system_resources(cpu, ram_p, ram_rem, ram_tot, disk_p, disk_rem, disk_total, i_used, i_tot, conn):
    query = """
        INSERT INTO system_stats 
        (cpu, ram_percent, ram_remaining, ram_total, disk_percent, disk_remaining, disk_total, inodes_used, inodes_total, connections) 
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    await execute_query(query, (cpu, ram_p, ram_rem, ram_tot, disk_p, disk_rem, disk_total, i_used, i_tot, conn))

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

# =====================================================
# Database Management (database_creations)
# =====================================================

async def create_database(database_name: str, allowed_hosts: str, database_type: str, created_by: str) -> dict | None:
    """
    Create a new database record in database_creations.
    Returns a dict with database_uuid on success, None on failure.
    """
    database_uuid = str(uuid.uuid4())
    query = """
        INSERT INTO database_creations
        (database_uuid, database_name, allowed_hosts, database_type, created_by)
        VALUES (%s, %s, %s, %s, %s)
    """
    params = (database_uuid, database_name, allowed_hosts, database_type, created_by)
    result = await execute_query(query, params)
    if result:
        return {
            "database_uuid": database_uuid,
            "database_name": database_name,
            "allowed_hosts": allowed_hosts,
            "database_type": database_type,
            "created_by": created_by
        }
    return None

async def get_database(database_uuid: str = None, database_name: str = None) -> dict | None:
    """
    Fetch a single database record by UUID or name.
    Only returns non-deleted records (deleted_at IS NULL).
    """
    if database_uuid:
        query = "SELECT * FROM database_creations WHERE database_uuid = %s AND deleted_at IS NULL"
        params = (database_uuid,)
    elif database_name:
        query = "SELECT * FROM database_creations WHERE database_name = %s AND deleted_at IS NULL"
        params = (database_name,)
    else:
        return None
    return await execute_query(query, params, fetch_one=True)

async def get_all_databases(include_deleted: bool = False) -> list | None:
    """
    Fetch all database records, optionally including soft-deleted ones.
    """
    if include_deleted:
        query = "SELECT * FROM database_creations ORDER BY created_at DESC"
    else:
        query = "SELECT * FROM database_creations WHERE deleted_at IS NULL ORDER BY created_at DESC"
    return await execute_query(query, fetch_all=True)

async def update_database(database_uuid: str, allowed_hosts: str = None, database_name: str = None, updated_by: str = None) -> bool:
    """
    Update allowed_hosts and/or database_name of a database.
    Returns True if at least one row was updated, False otherwise.
    """
    updates = []
    params = []
    if allowed_hosts is not None:
        updates.append("allowed_hosts = %s")
        params.append(allowed_hosts)
    if database_name is not None:
        updates.append("database_name = %s")
        params.append(database_name)
    if updated_by is not None:
        updates.append("updated_by = %s")
        params.append(updated_by)
    if not updates:
        return False
    query = f"UPDATE database_creations SET {', '.join(updates)} WHERE database_uuid = %s AND deleted_at IS NULL"
    params.append(database_uuid)
    result = await execute_query(query, tuple(params))
    # execute_query returns lastrowid for updates, which is non-zero if a row was affected.
    return bool(result)

async def delete_database(database_uuid: str, deleted_by: str) -> bool:
    """
    Soft delete a database by setting deleted_at and deleted_by.
    Returns True if successful.
    """
    query = "UPDATE database_creations SET deleted_at = CURRENT_TIMESTAMP, deleted_by = %s WHERE database_uuid = %s AND deleted_at IS NULL"
    result = await execute_query(query, (deleted_by, database_uuid))
    return bool(result)


# =====================================================
# Database Users (database_users)
# =====================================================

async def create_database_user(username: str, password_encrypted: str, created_by: str) -> dict | None:
    """
    Create a new database user record.
    Returns a dict with user_uuid on success.
    """
    user_uuid = str(uuid.uuid4())
    query = """
        INSERT INTO database_users
        (user_uuid, username, password_encrypted, created_by)
        VALUES (%s, %s, %s, %s)
    """
    params = (user_uuid, username, password_encrypted, created_by)
    result = await execute_query(query, params)
    if result:
        return {
            "user_uuid": user_uuid,
            "username": username,
            "created_by": created_by
        }
    return None

async def get_database_user(user_uuid: str = None, username: str = None) -> dict | None:
    """
    Fetch a single database user by UUID or username.
    Only returns non-deleted users.
    """
    if user_uuid:
        query = "SELECT * FROM database_users WHERE user_uuid = %s AND deleted_at IS NULL"
        params = (user_uuid,)
    elif username:
        query = "SELECT * FROM database_users WHERE username = %s AND deleted_at IS NULL"
        params = (username,)
    else:
        return None
    return await execute_query(query, params, fetch_one=True)

async def get_all_database_users(include_deleted: bool = False) -> list | None:
    """
    Fetch all database users.
    """
    if include_deleted:
        query = "SELECT * FROM database_users ORDER BY created_at DESC"
    else:
        query = "SELECT * FROM database_users WHERE deleted_at IS NULL ORDER BY created_at DESC"
    return await execute_query(query, fetch_all=True)

async def update_database_user(user_uuid: str, username: str = None, password_encrypted: str = None, updated_by: str = None) -> bool:
    """
    Update username and/or password of a user.
    """
    updates = []
    params = []
    if username is not None:
        updates.append("username = %s")
        params.append(username)
    if password_encrypted is not None:
        updates.append("password_encrypted = %s")
        params.append(password_encrypted)
    if updated_by is not None:
        updates.append("updated_by = %s")
        params.append(updated_by)
    if not updates:
        return False
    query = f"UPDATE database_users SET {', '.join(updates)} WHERE user_uuid = %s AND deleted_at IS NULL"
    params.append(user_uuid)
    result = await execute_query(query, tuple(params))
    return bool(result)

async def delete_database_user(user_uuid: str, deleted_by: str) -> bool:
    """
    Soft delete a user.
    """
    query = "UPDATE database_users SET deleted_at = CURRENT_TIMESTAMP, deleted_by = %s WHERE user_uuid = %s AND deleted_at IS NULL"
    result = await execute_query(query, (deleted_by, user_uuid))
    return bool(result)


# =====================================================
# Database User Privileges (database_user_privileges)
# =====================================================

async def grant_database_privileges(database_uuid: str, user_uuid: str, privileges: str, granted_by: str) -> bool:
    """
    Grant privileges to a user on a database.
    This will automatically revoke any currently active privileges for the same (database, user) pair.
    Returns True on success.
    """
    # First, revoke any existing active privileges
    revoke_query = """
        UPDATE database_user_privileges
        SET revoked_at = CURRENT_TIMESTAMP, revoked_by = %s
        WHERE database_uuid = %s AND user_uuid = %s AND revoked_at IS NULL
    """
    await execute_query(revoke_query, (granted_by, database_uuid, user_uuid))

    # Then insert the new grant
    insert_query = """
        INSERT INTO database_user_privileges
        (database_uuid, user_uuid, privileges, granted_by)
        VALUES (%s, %s, %s, %s)
    """
    result = await execute_query(insert_query, (database_uuid, user_uuid, privileges, granted_by))
    return bool(result)

async def revoke_database_privileges(database_uuid: str, user_uuid: str, revoked_by: str) -> bool:
    """
    Revoke all active privileges for a specific database-user pair.
    """
    query = """
        UPDATE database_user_privileges
        SET revoked_at = CURRENT_TIMESTAMP, revoked_by = %s
        WHERE database_uuid = %s AND user_uuid = %s AND revoked_at IS NULL
    """
    result = await execute_query(query, (revoked_by, database_uuid, user_uuid))
    return bool(result)

async def get_database_privileges(database_uuid: str = None, user_uuid: str = None, include_revoked: bool = False) -> list | None:
    """
    Fetch privilege records.
    If both database_uuid and user_uuid are None, returns all privileges (use with caution).
    You can filter by database, user, or both.
    By default returns only active (revoked_at IS NULL) privileges.
    """
    conditions = []
    params = []
    if database_uuid:
        conditions.append("database_uuid = %s")
        params.append(database_uuid)
    if user_uuid:
        conditions.append("user_uuid = %s")
        params.append(user_uuid)
    if not include_revoked:
        conditions.append("revoked_at IS NULL")

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
    query = f"SELECT * FROM database_user_privileges {where_clause} ORDER BY granted_at DESC"
    return await execute_query(query, tuple(params), fetch_all=True)

async def get_active_privileges_for_database(database_uuid: str) -> list | None:
    """Get all active privilege rows for a given database (including user details)."""
    query = """
        SELECT p.*, u.username
        FROM database_user_privileges p
        JOIN database_users u ON p.user_uuid = u.user_uuid
        WHERE p.database_uuid = %s AND p.revoked_at IS NULL AND u.deleted_at IS NULL
        ORDER BY p.granted_at DESC
    """
    return await execute_query(query, (database_uuid,), fetch_all=True)


# =====================================================
# Database Backups (database_backups) – with soft delete
# =====================================================

async def create_backup(
    target_database_uuid: str,
    file_name: str,
    file_path: str,
    file_size_bytes: int = None,
    checksum: str = None,
    status: str = 'pending'
) -> dict | None:
    """
    Create a new backup record.
    Returns a dict with backup_uuid on success.
    """
    backup_uuid = str(uuid.uuid4())
    query = """
        INSERT INTO database_backups
        (backup_uuid, target_database_uuid, file_name, file_path, file_size_bytes, checksum, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """
    params = (backup_uuid, target_database_uuid, file_name, file_path, file_size_bytes, checksum, status)
    result = await execute_query(query, params)
    if result:
        return {
            "backup_uuid": backup_uuid,
            "target_database_uuid": target_database_uuid,
            "file_name": file_name,
            "file_path": file_path,
            "file_size_bytes": file_size_bytes,
            "checksum": checksum,
            "status": status
        }
    return None

async def update_backup_status(backup_uuid: str, status: str, file_size_bytes: int = None, checksum: str = None) -> bool:
    """
    Update the status and optionally file_size_bytes/checksum of a backup.
    """
    updates = ["status = %s"]
    params = [status]
    if file_size_bytes is not None:
        updates.append("file_size_bytes = %s")
        params.append(file_size_bytes)
    if checksum is not None:
        updates.append("checksum = %s")
        params.append(checksum)
    params.append(backup_uuid)
    query = f"UPDATE database_backups SET {', '.join(updates)} WHERE backup_uuid = %s"
    result = await execute_query(query, tuple(params))
    return bool(result)

async def get_backup(backup_uuid: str, include_deleted: bool = False) -> dict | None:
    """
    Fetch a single backup record by UUID.
    By default excludes soft‑deleted backups unless include_deleted=True.
    """
    if include_deleted:
        query = "SELECT * FROM database_backups WHERE backup_uuid = %s"
    else:
        query = "SELECT * FROM database_backups WHERE backup_uuid = %s AND deleted_at IS NULL"
    return await execute_query(query, (backup_uuid,), fetch_one=True)

async def get_backups_for_database(database_uuid: str, limit: int = 10, include_deleted: bool = False) -> list | None:
    """
    Fetch the most recent backups for a given database.
    By default excludes soft‑deleted backups unless include_deleted=True.
    """
    if include_deleted:
        query = """
            SELECT * FROM database_backups
            WHERE target_database_uuid = %s
            ORDER BY created_at DESC
            LIMIT %s
        """
    else:
        query = """
            SELECT * FROM database_backups
            WHERE target_database_uuid = %s AND deleted_at IS NULL
            ORDER BY created_at DESC
            LIMIT %s
        """
    return await execute_query(query, (database_uuid, limit), fetch_all=True)

async def delete_backup(backup_uuid: str, deleted_by: str = None) -> bool:
    """
    Soft delete a backup by setting deleted_at (and optionally deleted_by).
    Returns True if successful.
    """
    if deleted_by:
        query = "UPDATE database_backups SET deleted_at = CURRENT_TIMESTAMP, deleted_by = %s WHERE backup_uuid = %s AND deleted_at IS NULL"
        params = (deleted_by, backup_uuid)
    else:
        query = "UPDATE database_backups SET deleted_at = CURRENT_TIMESTAMP WHERE backup_uuid = %s AND deleted_at IS NULL"
        params = (backup_uuid,)
    result = await execute_query(query, params)
    return bool(result)

async def delete_backups_for_database(database_uuid: str, deleted_by: str = None) -> bool:
    """
    Soft delete all active backups for a given database.
    Useful when the database itself is soft‑deleted.
    Returns True if at least one backup was updated.
    """
    if deleted_by:
        query = "UPDATE database_backups SET deleted_at = CURRENT_TIMESTAMP, deleted_by = %s WHERE target_database_uuid = %s AND deleted_at IS NULL"
        params = (deleted_by, database_uuid)
    else:
        query = "UPDATE database_backups SET deleted_at = CURRENT_TIMESTAMP WHERE target_database_uuid = %s AND deleted_at IS NULL"
        params = (database_uuid,)
    result = await execute_query(query, params)
    return bool(result)

async def get_backups_for_database(database_uuid: str) -> Optional[list]:
    return await execute_query(
        "SELECT * FROM database_backups WHERE target_database_uuid = %s ORDER BY created_at DESC",
        (database_uuid,),
        fetch='all'
    )

async def get_backup_by_uuid(backup_uuid: str) -> Optional[dict]:
    return await execute_query(
        "SELECT * FROM database_backups WHERE backup_uuid = %s",
        (backup_uuid,),
        fetch='one'
    )