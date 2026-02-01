import aiosqlite
import os
import uuid
import secrets

DB_PATH = os.getenv('DB_PATH', './nydus.db')

async def init_db():
    if not os.path.exists(DB_PATH):
        open(DB_PATH, 'a').close()
        
    async with aiosqlite.connect(DB_PATH) as db:
        with open('database/schema.sql', 'r') as f:
            await db.executescript(f.read())
        await db.commit()

async def log_usage(cpu, ram, disk, connections):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO usage_logs (cpu_percent, ram_percent, disk_percent, active_connections) VALUES (?, ?, ?, ?)",
            (cpu, ram, disk, connections)
        )
        await db.commit()

async def log_deployment(project, status, trigger, output):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO deployment_history (project_name, status, triggered_by, output_log) VALUES (?, ?, ?, ?)",
            (project, status, trigger, output)
        )
        await db.commit()

async def get_recent_usage(limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM usage_logs ORDER BY timestamp DESC LIMIT ?", (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def get_deployments(limit=5):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM deployment_history ORDER BY timestamp DESC LIMIT ?", (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def get_project_by_uuid(uuid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM webhook_projects WHERE webhook_uuid = ?", (uuid,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def get_all_projects():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM webhook_projects ORDER BY id DESC") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def create_new_project(name, repo_url, branch, tech_stack, subdomain, cloudflare_id):
    new_uuid = str(uuid.uuid4())
    new_secret = secrets.token_hex(16)
    deploy_path = f"/var/www/{new_uuid}"
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO webhook_projects 
               (webhook_uuid, project_name, github_repository_url, branch, deploy_path, tech_stack, webhook_secret, subdomain, cloudflare_record_id) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (new_uuid, name, repo_url, branch, deploy_path, tech_stack, new_secret, subdomain, cloudflare_id)
        )
        await db.commit()
        return {
            "success": True,
            "webhook_uuid": new_uuid,
            "webhook_secret": new_secret,
            "deploy_path": deploy_path
        }

async def delete_project(uuid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM webhook_projects WHERE webhook_uuid = ?", (uuid,))
        await db.commit()