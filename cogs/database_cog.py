import asyncio
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, Tuple, Dict, List

from cryptography.fernet import Fernet, InvalidToken
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
    create_database as db_create_database,
    delete_database as db_delete_database,
    create_database_user as db_create_database_user,
    delete_database_user as db_delete_database_user,
    grant_database_privileges as db_grant_database_privileges,
    revoke_database_privileges as db_revoke_database_privileges,
)

logger = logging.getLogger('nydus.database')


# ------------------------------------------------------------------
# Abstract Base Class for Database Backends
# ------------------------------------------------------------------
class DatabaseBackend(ABC):

    @abstractmethod
    async def create_database(self, db_name: str) -> Tuple[bool, str]:
        pass

    @abstractmethod
    async def drop_database(self, db_name: str) -> Tuple[bool, str]:
        pass

    @abstractmethod
    async def create_user(self, username: str, password: str) -> Tuple[bool, str]:
        pass

    @abstractmethod
    async def drop_user(self, username: str) -> Tuple[bool, str]:
        pass

    @abstractmethod
    async def grant_privileges(self, db_name: str, username: str, privileges: str) -> Tuple[bool, str]:
        pass

    @abstractmethod
    async def revoke_privileges(self, db_name: str, username: str, privileges: str) -> Tuple[bool, str]:
        pass

    @abstractmethod
    async def backup(self, db_name: str, backup_path: str) -> Tuple[bool, str]:
        pass

    @abstractmethod
    async def restore(self, db_name: str, backup_path: str) -> Tuple[bool, str]:
        pass


# ------------------------------------------------------------------
# MySQL Backend
# ------------------------------------------------------------------
class MySQLBackend(DatabaseBackend):
    def __init__(self, host: str, port: int, user: str, password: str,
                 backup_dir: str, allowed_hosts: Optional[List[str]] = None):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.backup_dir = backup_dir

        raw_hosts = allowed_hosts if allowed_hosts else ['localhost']
        if '*' in raw_hosts:
            self._resolved_hosts = ['%']
        else:
            self._resolved_hosts = raw_hosts

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
        if errors:
            return False, "; ".join(errors)
        return True, ""

    async def drop_user(self, username: str) -> Tuple[bool, str]:
        errors = []
        for host in self._resolved_hosts:
            result = await execute_query(f"DROP USER IF EXISTS '{username}'@'{host}'")
            if result is None:
                errors.append(f"{host}: query failed")
        if errors:
            return False, "; ".join(errors)
        return True, ""

    async def grant_privileges(self, db_name: str, username: str, privileges: str) -> Tuple[bool, str]:
        errors = []
        for host in self._resolved_hosts:
            result = await execute_query(
                f"GRANT {privileges} ON `{db_name}`.* TO '{username}'@'{host}'"
            )
            if result is None:
                errors.append(f"{host}: query failed")
        if errors:
            return False, "; ".join(errors)
        return True, ""

    async def revoke_privileges(self, db_name: str, username: str, privileges: str) -> Tuple[bool, str]:
        errors = []
        for host in self._resolved_hosts:
            result = await execute_query(
                f"REVOKE {privileges} ON `{db_name}`.* FROM '{username}'@'{host}'"
            )
            if result is None:
                errors.append(f"{host}: query failed")
        if errors:
            return False, "; ".join(errors)
        return True, ""

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
            with open(backup_path, 'wb') as f:
                while True:
                    chunk = await process.stdout.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
            _, stderr = await process.communicate()
            if process.returncode != 0:
                error = stderr.decode() if stderr else "mysqldump failed"
                return False, error
            return True, ""
        except Exception as e:
            return False, str(e)

    async def restore(self, db_name: str, backup_path: str) -> Tuple[bool, str]:
        cmd = [
            'mysql',
            '-h', self.host,
            '-P', str(self.port),
            '-u', self.user,
            f'-p{self.password}',
            db_name
        ]
        try:
            with open(backup_path, 'rb') as f:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=f,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
            _, stderr = await process.communicate()
            if process.returncode != 0:
                error = stderr.decode() if stderr else "mysql restore failed"
                return False, error
            return True, ""
        except Exception as e:
            return False, str(e)


# ------------------------------------------------------------------
# Main Cog
# ------------------------------------------------------------------
class DatabaseCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot
        self.backends: Dict[str, DatabaseBackend] = {}

        encryption_key = os.getenv('DB_ENCRYPTION_KEY')
        if not encryption_key:
            raise RuntimeError("DB_ENCRYPTION_KEY environment variable is not set.")
        try:
            self._fernet = Fernet(encryption_key.encode())
        except (ValueError, Exception) as e:
            raise RuntimeError(f"Invalid DB_ENCRYPTION_KEY: {e}")

        self.backup_base_dir = os.getenv('BACKUP_DIR', '/var/backups/nydus')
        os.makedirs(self.backup_base_dir, exist_ok=True)

        db_host = os.getenv('DB_HOST')
        db_port = int(os.getenv('DB_PORT', 3306))
        db_user = os.getenv('DB_USER')
        db_password = os.getenv('DB_PASSWORD')

        if db_host and db_user and db_password is not None:
            mysql_backup_dir = os.path.join(self.backup_base_dir, 'mysql')
            os.makedirs(mysql_backup_dir, exist_ok=True)

            raw_allowed = os.getenv('MYSQL_ALLOWED_HOSTS', 'localhost')
            allowed_hosts = [h.strip() for h in raw_allowed.split(',') if h.strip()]

            self.backends['mysql'] = MySQLBackend(
                host=db_host,
                port=db_port,
                user=db_user,
                password=db_password,
                backup_dir=mysql_backup_dir,
                allowed_hosts=allowed_hosts,
            )
            logger.info(f"MySQL backend enabled. Allowed hosts: {allowed_hosts}")
        else:
            logger.warning("MySQL backend disabled: missing DB environment variables.")

        if not self.backends:
            raise RuntimeError("No database backends configured. Please set DB credentials in your environment.")

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

    # ------------------------------------------------------------------
    # Metadata reads (delegated entirely to db.py)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Engine operations (coordinated with db.py recording)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Backup operations
    # ------------------------------------------------------------------
    async def perform_backup(self, database_uuid: str, database_type: str,
                             database_name: str) -> Tuple[bool, Optional[str]]:
        backend = self._get_backend(database_type)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = database_name.replace('`', '').replace("'", '').replace('"', '')
        filename = f"{safe_name}_{timestamp}.dump"
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


def setup(bot):
    bot.add_cog(DatabaseCog(bot))