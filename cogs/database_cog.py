import asyncio
import gzip
import logging
import os
import shlex
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import random
import secrets
from cryptography.fernet import Fernet, InvalidToken
import discord
from discord import Option, SlashCommandGroup
from discord.ext import commands

from database.db import (
    execute_query,
    create_backup,
    update_backup_status,
    get_database,
    get_all_databases,
    get_database_user,
    get_all_database_users,
    get_database_privileges,
    get_active_privileges_for_database,
    get_backups_for_database,
    get_backup_by_uuid,
    create_database as db_create_database,
    delete_database as db_delete_database,
    create_database_user as db_create_database_user,
    delete_database_user as db_delete_database_user,
    grant_database_privileges as db_grant_database_privileges,
    revoke_database_privileges as db_revoke_database_privileges,
)

logger = logging.getLogger('nydus.database')

_STARCRAFT_LOCATIONS = [
    "aiur", "char", "korhal", "tarsonis", "mar_sara", "antiga", "braxis",
    "shakuras", "moria", "umoja", "castanar", "dylar", "metis", "torus",
    "meinhoff", "agria", "tyrador", "kaldir", "zerus", "ulnar", "revanscar",
    "bel_shir", "skygeirr", "deadwing", "monlyth", "valhalla", "haven",
    "avernus", "stronar", "jarban", "turaxis", "choss", "artika", "pridewater"
]

_TOUHOU_CHARACTERS = [
    "reimu", "marisa", "sakuya", "remilia", "flandre", "youmu", "yuyuko",
    "yukari", "suika", "reisen", "eirin", "kaguya", "mokou", "sanae",
    "cirno", "meiling", "patchouli", "alice", "nitori", "aya", "momiji",
    "tenshi", "iku", "komachi", "satori", "koishi", "okuu", "orin",
    "nazrin", "shou", "byakuren", "kogasa", "ichirin", "murasa", "nue",
    "tewi", "keine", "mystia", "yuuka", "rumia", "wriggle", "chen", "ran"
]


class DatabaseBackend(ABC):

    @abstractmethod
    async def create_database(self, db_name: str) -> Tuple[bool, str]: pass

    @abstractmethod
    async def drop_database(self, db_name: str) -> Tuple[bool, str]: pass

    @abstractmethod
    async def create_user(self, username: str, password: str) -> Tuple[bool, str]: pass

    @abstractmethod
    async def drop_user(self, username: str) -> Tuple[bool, str]: pass

    @abstractmethod
    async def grant_privileges(self, db_name: str, username: str, privileges: str) -> Tuple[bool, str]: pass

    @abstractmethod
    async def revoke_privileges(self, db_name: str, username: str, privileges: str) -> Tuple[bool, str]: pass

    @abstractmethod
    async def backup(self, db_name: str, backup_path: str) -> Tuple[bool, str]: pass

    @abstractmethod
    async def restore(self, db_name: str, backup_path: str) -> Tuple[bool, str]: pass


class MySQLBackend(DatabaseBackend):

    def __init__(self, host: str, port: int, user: str, password: str,
                 backup_dir: str, allowed_hosts: Optional[List[str]] = None):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.backup_dir = backup_dir

        raw_hosts = allowed_hosts if allowed_hosts else ['localhost']
        self._resolved_hosts = ['%'] if '*' in raw_hosts else raw_hosts

    async def create_database(self, db_name: str) -> Tuple[bool, str]:
        result = await execute_query(
            f"CREATE DATABASE `{db_name}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        if result is None:
            return False, f"Failed to create database `{db_name}`"
        return True, ""

    async def drop_database(self, db_name: str) -> Tuple[bool, str]:
        result = await execute_query(f"DROP DATABASE IF EXISTS `{db_name}`")
        if result is None:
            return False, f"Failed to drop database `{db_name}`"
        return True, ""

    async def create_user(self, username: str, password: str) -> Tuple[bool, str]:
        errors = []
        for host in self._resolved_hosts:
            result = await execute_query(
                f"CREATE USER '{username}'@'{host}' IDENTIFIED BY %s",
                (password,)
            )
            if result is None:
                errors.append(f"{host}: query failed")
        return (False, "; ".join(errors)) if errors else (True, "")

    async def drop_user(self, username: str) -> Tuple[bool, str]:
        errors = []
        for host in self._resolved_hosts:
            result = await execute_query(f"DROP USER IF EXISTS '{username}'@'{host}'")
            if result is None:
                errors.append(f"{host}: query failed")
        return (False, "; ".join(errors)) if errors else (True, "")

    async def grant_privileges(self, db_name: str, username: str, privileges: str) -> Tuple[bool, str]:
        errors = []
        for host in self._resolved_hosts:
            result = await execute_query(
                f"GRANT {privileges} ON `{db_name}`.* TO '{username}'@'{host}'"
            )
            if result is None:
                errors.append(f"{host}: query failed")
        return (False, "; ".join(errors)) if errors else (True, "")

    async def revoke_privileges(self, db_name: str, username: str, privileges: str) -> Tuple[bool, str]:
        errors = []
        for host in self._resolved_hosts:
            result = await execute_query(
                f"REVOKE {privileges} ON `{db_name}`.* FROM '{username}'@'{host}'"
            )
            if result is None:
                errors.append(f"{host}: query failed")
        return (False, "; ".join(errors)) if errors else (True, "")

    async def backup(self, db_name: str, backup_path: str) -> Tuple[bool, str]:
        cmd = [
            'mysqldump',
            '-h', self.host,
            '-P', str(self.port),
            '-u', self.user,
            f'-p{self.password}',
            '--single-transaction',
            '--routines',
            '--triggers',
            db_name
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stderr_chunks: List[bytes] = []

            async def _read_stderr():
                while True:
                    chunk = await process.stderr.read(8192)
                    if not chunk:
                        break
                    stderr_chunks.append(chunk)

            async def _write_stdout():
                with gzip.open(backup_path, 'wb') as f:
                    while True:
                        chunk = await process.stdout.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)

            await asyncio.gather(_write_stdout(), _read_stderr())
            await process.wait()

            if process.returncode != 0:
                error = b''.join(stderr_chunks).decode(errors='replace') if stderr_chunks else "mysqldump failed"
                return False, error
            return True, ""
        except Exception as e:
            return False, str(e)

    async def restore(self, db_name: str, backup_path: str) -> Tuple[bool, str]:
        try:
            if backup_path.endswith('.gz'):
                shell_cmd = (
                    f"zcat {shlex.quote(backup_path)} | "
                    f"mysql -h {shlex.quote(self.host)} -P {self.port} "
                    f"-u {shlex.quote(self.user)} "
                    f"-p{shlex.quote(self.password)} "
                    f"{shlex.quote(db_name)}"
                )
                process = await asyncio.create_subprocess_shell(
                    shell_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                _, stderr = await process.communicate()
            else:
                cmd = [
                    'mysql',
                    '-h', self.host,
                    '-P', str(self.port),
                    '-u', self.user,
                    f'-p{self.password}',
                    db_name
                ]
                with open(backup_path, 'rb') as f:
                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdin=f,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                _, stderr = await process.communicate()

            if process.returncode != 0:
                return False, stderr.decode(errors='replace') if stderr else "restore failed"
            return True, ""
        except Exception as e:
            return False, str(e)


class DatabaseCog(commands.Cog):

    db = SlashCommandGroup("db", "Database management commands")
    db_user = db.create_subgroup("user", "Database user management")

    def __init__(self, bot):
        self.bot = bot
        self.backends: Dict[str, DatabaseBackend] = {}

        encryption_key = os.getenv('DB_ENCRYPTION_KEY')
        if not encryption_key:
            raise RuntimeError("DB_ENCRYPTION_KEY environment variable is not set.")
        try:
            self._fernet = Fernet(encryption_key.encode())
        except Exception as e:
            raise RuntimeError(f"Invalid DB_ENCRYPTION_KEY: {e}")

        self.backup_base_dir = os.getenv('BACKUP_DIR', '/var/backups/nydus')
        os.makedirs(self.backup_base_dir, exist_ok=True)

        db_host = os.getenv('DB_HOST')
        db_port = int(os.getenv('DB_PORT', 3306))
        db_user_env = os.getenv('DB_USER')
        db_password = os.getenv('DB_PASSWORD')

        if db_host and db_user_env and db_password is not None:
            mysql_backup_dir = os.path.join(self.backup_base_dir, 'mysql')
            os.makedirs(mysql_backup_dir, exist_ok=True)

            raw_allowed = os.getenv('MYSQL_ALLOWED_HOSTS', 'localhost')
            allowed_hosts = [h.strip() for h in raw_allowed.split(',') if h.strip()]

            self.backends['mysql'] = MySQLBackend(
                host=db_host,
                port=db_port,
                user=db_user_env,
                password=db_password,
                backup_dir=mysql_backup_dir,
                allowed_hosts=allowed_hosts,
            )
            logger.info(f"MySQL backend enabled. Allowed hosts: {allowed_hosts}")
        else:
            logger.warning("MySQL backend disabled: missing DB environment variables.")

        if not self.backends:
            raise RuntimeError("No database backends configured. Please set DB credentials in your environment.")

    def _check_dev(self, ctx: discord.ApplicationContext) -> bool:
        dev_id = os.getenv('DEV_ID')
        return dev_id is not None and str(ctx.author.id) == dev_id

    def _get_backend(self, database_type: str) -> DatabaseBackend:
        backend = self.backends.get(database_type)
        if not backend:
            raise ValueError(f"Unsupported or disabled database type: {database_type}")
        return backend

    def _encrypt_password(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def _decrypt_password(self, encrypted: str) -> Optional[str]:
        try:
            return self._fernet.decrypt(encrypted.encode()).decode()
        except InvalidToken:
            logger.error("Failed to decrypt password: invalid token.")
            return None

    async def fetch_all_databases(self, include_deleted: bool = False) -> Optional[list]:
        return await get_all_databases(include_deleted=include_deleted)

    async def fetch_database(self, database_uuid: str = None, database_name: str = None) -> Optional[dict]:
        return await get_database(database_uuid=database_uuid, database_name=database_name)

    async def fetch_all_database_users(self, include_deleted: bool = False) -> Optional[list]:
        return await get_all_database_users(include_deleted=include_deleted)

    async def fetch_privileges_for_database(self, database_uuid: str) -> Optional[list]:
        return await get_active_privileges_for_database(database_uuid)

    async def fetch_all_privileges(self) -> Optional[list]:
        return await get_database_privileges()

    async def get_user_credentials(self, user_uuid: str) -> Optional[dict]:
        user = await get_database_user(user_uuid=user_uuid)
        if not user:
            return None
        decrypted = self._decrypt_password(user['password_encrypted'])
        if decrypted is None:
            return None
        return {'username': user['username'], 'password': decrypted}

    async def fetch_backups_for_database(self, database_uuid: str) -> Optional[list]:
        return await get_backups_for_database(database_uuid)

    async def fetch_backup(self, backup_uuid: str) -> Optional[dict]:
        return await get_backup_by_uuid(backup_uuid)

    async def create_actual_database(self, database_type: str, database_name: str,
                                     allowed_hosts: str, created_by: str) -> Tuple[bool, str]:
        success, error = await self._get_backend(database_type).create_database(database_name)
        if not success:
            return False, error
        record = await db_create_database(database_name, allowed_hosts, database_type, created_by)
        if not record:
            logger.error(f"Database `{database_name}` created on server but metadata record failed.")
            return False, "Database created but failed to record metadata."
        return True, record['database_uuid']

    async def drop_actual_database(self, database_type: str, database_name: str,
                                   database_uuid: str, deleted_by: str) -> Tuple[bool, str]:
        success, error = await self._get_backend(database_type).drop_database(database_name)
        if not success:
            return False, error
        await db_delete_database(database_uuid, deleted_by)
        return True, ""

    async def create_actual_user(self, database_type: str, username: str, password: str,
                                 created_by: str) -> Tuple[bool, str]:
        success, error = await self._get_backend(database_type).create_user(username, password)
        if not success:
            return False, error
        encrypted = self._encrypt_password(password)
        record = await db_create_database_user(username, encrypted, created_by)
        if not record:
            logger.error(f"User `{username}` created on server but metadata record failed.")
            return False, "User created but failed to record metadata."
        return True, record['user_uuid']

    async def drop_actual_user(self, database_type: str, username: str,
                               user_uuid: str, deleted_by: str) -> Tuple[bool, str]:
        success, error = await self._get_backend(database_type).drop_user(username)
        if not success:
            return False, error
        await db_delete_database_user(user_uuid, deleted_by)
        return True, ""

    async def grant_actual_privileges(self, database_type: str, database_name: str,
                                      database_uuid: str, username: str, user_uuid: str,
                                      privileges: str, granted_by: str) -> Tuple[bool, str]:
        success, error = await self._get_backend(database_type).grant_privileges(database_name, username, privileges)
        if not success:
            return False, error
        await db_grant_database_privileges(database_uuid, user_uuid, privileges, granted_by)
        return True, ""

    async def revoke_actual_privileges(self, database_type: str, database_name: str,
                                       database_uuid: str, username: str, user_uuid: str,
                                       privileges: str, revoked_by: str) -> Tuple[bool, str]:
        success, error = await self._get_backend(database_type).revoke_privileges(database_name, username, privileges)
        if not success:
            return False, error
        await db_revoke_database_privileges(database_uuid, user_uuid, revoked_by)
        return True, ""

    async def perform_backup(self, database_uuid: str, database_type: str,
                             database_name: str) -> Tuple[bool, Optional[str]]:
        backend = self._get_backend(database_type)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = database_name.replace('`', '').replace("'", '').replace('"', '')
        filename = f"{safe_name}_{timestamp}.sql.gz"
        filepath = os.path.join(backend.backup_dir, filename)

        backup_info = await create_backup(
            target_database_uuid=database_uuid,
            file_name=filename,
            file_path=filepath,
            status='running'
        )
        if not backup_info:
            logger.error("Failed to create backup record in database.")
            return False, "Failed to create backup record"

        backup_uuid = backup_info['backup_uuid']

        success, error = await backend.backup(database_name, filepath)
        if success:
            file_size = os.path.getsize(filepath)
            await update_backup_status(backup_uuid, 'completed', file_size_bytes=file_size)
            logger.info(f"Backup completed: {filepath}")
            return True, backup_uuid
        else:
            await update_backup_status(backup_uuid, 'failed')
            if os.path.exists(filepath):
                os.unlink(filepath)
            logger.error(f"Backup failed for {database_name}: {error}")
            return False, error

    async def restore_backup(self, database_type: str, database_name: str,
                             backup_file_path: str) -> Tuple[bool, str]:
        backend = self._get_backend(database_type)
        if not os.path.exists(backup_file_path):
            return False, "Backup file not found"
        return await backend.restore(database_name, backup_file_path)

    async def _generate_quickgen_names(self) -> tuple[str, str]:
        existing = await get_all_databases(include_deleted=False) or []
        existing_names = {db['database_name'] for db in existing}

        for _ in range(20):
            location = random.choice(_STARCRAFT_LOCATIONS)
            hex_suffix = secrets.token_hex(4)
            db_name = f"{location}_{hex_suffix}"
            if db_name not in existing_names:
                touhou = random.choice(_TOUHOU_CHARACTERS)
                username = f"{touhou}_{db_name}"
                return db_name, username

        raise RuntimeError("Failed to generate a unique database name after 20 attempts.")

    async def quickgen_provision(self, database_type: str, created_by: str) -> tuple[bool, str, dict]:
        db_name, username = await self._generate_quickgen_names()
        password = secrets.token_urlsafe(24)

        ok, result = await self.create_actual_database(database_type, db_name, '*', created_by)
        if not ok:
            return False, result, {}

        database_uuid = result

        ok, result = await self.create_actual_user(database_type, username, password, created_by)
        if not ok:
            await self.drop_actual_database(database_type, db_name, database_uuid, created_by)
            return False, result, {}

        user_uuid = result

        ok, error = await self.grant_actual_privileges(
            database_type, db_name, database_uuid,
            username, user_uuid, 'ALL PRIVILEGES', created_by
        )
        if not ok:
            await self.drop_actual_user(database_type, username, user_uuid, created_by)
            await self.drop_actual_database(database_type, db_name, database_uuid, created_by)
            return False, error, {}

        return True, "", {
            "database_uuid": database_uuid,
            "database_name": db_name,
            "user_uuid": user_uuid,
            "username": username,
            "password": password,
        }

    @db.command(name="list", description="List all databases")
    async def cmd_db_list(
        self,
        ctx: discord.ApplicationContext,
        include_deleted: Option(bool, "Include soft-deleted databases", default=False)
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        databases = await self.fetch_all_databases(include_deleted=include_deleted)
        if not databases:
            await ctx.respond("No databases found.", ephemeral=True)
            return

        lines = []
        for record in databases:
            status = " (deleted)" if record.get('deleted_at') else ""
            lines.append(f"`{record['database_name']}` — {record['database_type']}{status}")

        embed = discord.Embed(
            title=f"Databases ({len(databases)})",
            description="\n".join(lines),
            color=discord.Color.blurple()
        )
        await ctx.respond(embed=embed, ephemeral=True)

    @db.command(name="info", description="Get details on a database by name or UUID")
    async def cmd_db_info(
        self,
        ctx: discord.ApplicationContext,
        identifier: Option(str, "Database name or UUID")
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        if '-' in identifier:
            record = await self.fetch_database(database_uuid=identifier)
        else:
            record = await self.fetch_database(database_name=identifier)

        if not record:
            await ctx.respond("Database not found.", ephemeral=True)
            return

        color = discord.Color.red() if record.get('deleted_at') else discord.Color.blurple()
        embed = discord.Embed(title=record['database_name'], color=color)
        embed.add_field(name="UUID", value=record['database_uuid'], inline=False)
        embed.add_field(name="Type", value=record['database_type'], inline=True)
        embed.add_field(name="Allowed Hosts", value=record.get('allowed_hosts', 'N/A'), inline=True)
        embed.add_field(name="Created By", value=record.get('created_by', 'N/A'), inline=True)
        embed.add_field(name="Created At", value=str(record.get('created_at', 'N/A')), inline=True)
        if record.get('deleted_at'):
            embed.add_field(name="Deleted At", value=str(record['deleted_at']), inline=True)
        await ctx.respond(embed=embed, ephemeral=True)

    @db.command(name="create", description="Create a new database")
    async def cmd_db_create(
        self,
        ctx: discord.ApplicationContext,
        database_type: Option(str, "Database type", choices=["mysql"]),
        database_name: Option(str, "Name for the database"),
        allowed_hosts: Option(str, "Comma-separated allowed hosts", default="localhost")
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        success, result = await self.create_actual_database(
            database_type, database_name, allowed_hosts, str(ctx.author.id)
        )
        if not success:
            await ctx.followup.send(f"Failed: {result}", ephemeral=True)
            return

        embed = discord.Embed(title="Database Created", color=discord.Color.green())
        embed.add_field(name="Name", value=database_name, inline=True)
        embed.add_field(name="UUID", value=result, inline=False)
        await ctx.followup.send(embed=embed, ephemeral=True)

    @db.command(name="drop", description="Drop a database by UUID")
    async def cmd_db_drop(
        self,
        ctx: discord.ApplicationContext,
        database_uuid: Option(str, "Database UUID"),
        database_name: Option(str, "Database name"),
        database_type: Option(str, "Database type", choices=["mysql"])
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        success, error = await self.drop_actual_database(
            database_type, database_name, database_uuid, str(ctx.author.id)
        )
        if not success:
            await ctx.followup.send(f"Failed: {error}", ephemeral=True)
            return

        await ctx.followup.send(f"Database `{database_name}` dropped.", ephemeral=True)

    @db.command(name="quickgen", description="Auto-provision a database with a generated name and user")
    async def cmd_db_quickgen(
        self,
        ctx: discord.ApplicationContext,
        database_type: Option(str, "Database type", choices=["mysql"])
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        success, error, result = await self.quickgen_provision(database_type, str(ctx.author.id))
        if not success:
            await ctx.followup.send(f"Failed: {error}", ephemeral=True)
            return

        embed = discord.Embed(title="Quickgen Provisioned", color=discord.Color.green())
        embed.add_field(name="Database", value=result['database_name'], inline=False)
        embed.add_field(name="DB UUID", value=result['database_uuid'], inline=False)
        embed.add_field(name="Username", value=result['username'], inline=True)
        embed.add_field(name="Password", value=result['password'], inline=True)
        embed.add_field(name="User UUID", value=result['user_uuid'], inline=False)
        embed.set_footer(text="Ephemeral. Save these credentials before dismissing.")
        await ctx.followup.send(embed=embed, ephemeral=True)

    @db.command(name="backup", description="Trigger a manual backup for a database")
    async def cmd_db_backup(
        self,
        ctx: discord.ApplicationContext,
        database_uuid: Option(str, "Database UUID"),
        database_type: Option(str, "Database type", choices=["mysql"]),
        database_name: Option(str, "Database name")
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        success, result = await self.perform_backup(database_uuid, database_type, database_name)
        if not success:
            await ctx.followup.send(f"Backup failed: {result}", ephemeral=True)
            return

        embed = discord.Embed(title="Backup Complete", color=discord.Color.green())
        embed.add_field(name="Backup UUID", value=result, inline=False)
        embed.add_field(name="Database", value=database_name, inline=True)
        await ctx.followup.send(embed=embed, ephemeral=True)

    @db.command(name="backups", description="List backups for a database")
    async def cmd_db_backups(
        self,
        ctx: discord.ApplicationContext,
        database_uuid: Option(str, "Database UUID")
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        backups = await self.fetch_backups_for_database(database_uuid)
        if not backups:
            await ctx.respond("No backups found for this database.", ephemeral=True)
            return

        lines = []
        for b in backups[:15]:
            size = b.get('file_size_bytes')
            size_str = f"{size / 1024 / 1024:.2f} MB" if size else "N/A"
            short_uuid = b['backup_uuid'][:8]
            lines.append(f"`{short_uuid}...` — {b['status']} — {size_str} — {b.get('created_at', '')}")

        total = len(backups)
        shown = min(total, 15)
        embed = discord.Embed(
            title=f"Backups ({total} total, showing {shown})",
            description="\n".join(lines),
            color=discord.Color.blurple()
        )
        embed.set_footer(text="Use /db snapshot <uuid> for details. Download via API.")
        await ctx.respond(embed=embed, ephemeral=True)

    @db.command(name="snapshot", description="Show details for a specific backup snapshot")
    async def cmd_db_snapshot(
        self,
        ctx: discord.ApplicationContext,
        backup_uuid: Option(str, "Backup UUID")
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        backup = await self.fetch_backup(backup_uuid)
        if not backup:
            await ctx.respond("Snapshot not found.", ephemeral=True)
            return

        size = backup.get('file_size_bytes')
        size_str = f"{size / 1024 / 1024:.2f} MB" if size else "N/A"
        on_disk = os.path.exists(backup.get('file_path', ''))

        embed = discord.Embed(title="Snapshot Info", color=discord.Color.blurple())
        embed.add_field(name="UUID", value=backup['backup_uuid'], inline=False)
        embed.add_field(name="File", value=backup.get('file_name', 'N/A'), inline=True)
        embed.add_field(name="Size", value=size_str, inline=True)
        embed.add_field(name="Status", value=backup.get('status', 'N/A'), inline=True)
        embed.add_field(name="On Disk", value="Yes" if on_disk else "No", inline=True)
        embed.add_field(name="Created", value=str(backup.get('created_at', 'N/A')), inline=True)
        embed.set_footer(text=f"Download: GET /api/databases/backups/{backup['backup_uuid']}/download")
        await ctx.respond(embed=embed, ephemeral=True)

    @db.command(name="privileges", description="List active privileges for a database")
    async def cmd_db_privileges(
        self,
        ctx: discord.ApplicationContext,
        database_uuid: Option(str, "Database UUID")
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        privs = await self.fetch_privileges_for_database(database_uuid)
        if not privs:
            await ctx.respond("No active privileges found.", ephemeral=True)
            return

        lines = [f"`{p['user_uuid'][:8]}...` — {p.get('privileges', 'N/A')}" for p in privs]
        embed = discord.Embed(
            title=f"Privileges ({len(privs)})",
            description="\n".join(lines),
            color=discord.Color.blurple()
        )
        await ctx.respond(embed=embed, ephemeral=True)

    @db.command(name="grant", description="Grant privileges to a user on a database")
    async def cmd_db_grant(
        self,
        ctx: discord.ApplicationContext,
        database_uuid: Option(str, "Database UUID"),
        database_name: Option(str, "Database name"),
        database_type: Option(str, "Database type", choices=["mysql"]),
        user_uuid: Option(str, "User UUID"),
        username: Option(str, "Username"),
        privileges: Option(str, "Privileges string", default="ALL PRIVILEGES")
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        success, error = await self.grant_actual_privileges(
            database_type, database_name, database_uuid,
            username, user_uuid, privileges, str(ctx.author.id)
        )
        if not success:
            await ctx.followup.send(f"Failed: {error}", ephemeral=True)
            return

        await ctx.followup.send(
            f"Granted `{privileges}` on `{database_name}` to `{username}`.", ephemeral=True
        )

    @db.command(name="revoke", description="Revoke privileges from a user on a database")
    async def cmd_db_revoke(
        self,
        ctx: discord.ApplicationContext,
        database_uuid: Option(str, "Database UUID"),
        database_name: Option(str, "Database name"),
        database_type: Option(str, "Database type", choices=["mysql"]),
        user_uuid: Option(str, "User UUID"),
        username: Option(str, "Username"),
        privileges: Option(str, "Privileges string", default="ALL PRIVILEGES")
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        success, error = await self.revoke_actual_privileges(
            database_type, database_name, database_uuid,
            username, user_uuid, privileges, str(ctx.author.id)
        )
        if not success:
            await ctx.followup.send(f"Failed: {error}", ephemeral=True)
            return

        await ctx.followup.send(
            f"Revoked `{privileges}` on `{database_name}` from `{username}`.", ephemeral=True
        )

    @db_user.command(name="list", description="List database users")
    async def cmd_user_list(
        self,
        ctx: discord.ApplicationContext,
        include_deleted: Option(bool, "Include soft-deleted users", default=False)
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        users = await self.fetch_all_database_users(include_deleted=include_deleted)
        if not users:
            await ctx.respond("No users found.", ephemeral=True)
            return

        lines = []
        for u in users:
            status = " (deleted)" if u.get('deleted_at') else ""
            lines.append(f"`{u['username']}` — {u['user_uuid'][:8]}...{status}")

        embed = discord.Embed(
            title=f"Database Users ({len(users)})",
            description="\n".join(lines),
            color=discord.Color.blurple()
        )
        await ctx.respond(embed=embed, ephemeral=True)

    @db_user.command(name="create", description="Create a database user")
    async def cmd_user_create(
        self,
        ctx: discord.ApplicationContext,
        database_type: Option(str, "Database type", choices=["mysql"]),
        username: Option(str, "Username"),
        password: Option(str, "Password (leave blank to auto-generate)", default=None)
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        final_password = password if password else secrets.token_urlsafe(20)

        success, result = await self.create_actual_user(
            database_type, username, final_password, str(ctx.author.id)
        )
        if not success:
            await ctx.followup.send(f"Failed: {result}", ephemeral=True)
            return

        embed = discord.Embed(title="User Created", color=discord.Color.green())
        embed.add_field(name="Username", value=username, inline=True)
        embed.add_field(name="Password", value=final_password, inline=True)
        embed.add_field(name="User UUID", value=result, inline=False)
        embed.set_footer(text="Ephemeral. Save these credentials before dismissing.")
        await ctx.followup.send(embed=embed, ephemeral=True)

    @db_user.command(name="drop", description="Drop a database user by UUID")
    async def cmd_user_drop(
        self,
        ctx: discord.ApplicationContext,
        database_type: Option(str, "Database type", choices=["mysql"]),
        user_uuid: Option(str, "User UUID"),
        username: Option(str, "Username")
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        success, error = await self.drop_actual_user(
            database_type, username, user_uuid, str(ctx.author.id)
        )
        if not success:
            await ctx.followup.send(f"Failed: {error}", ephemeral=True)
            return

        await ctx.followup.send(f"User `{username}` dropped.", ephemeral=True)


def setup(bot):
    bot.add_cog(DatabaseCog(bot))