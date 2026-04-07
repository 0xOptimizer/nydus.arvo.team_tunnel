import asyncio
import json
import logging
import os
import shutil
import uuid as uuid_lib
from datetime import datetime, timezone

import aiohttp
import discord
from discord.ext import commands

from database.db import (
    create_deployment,
    create_deployment_log,
    get_deployment_by_subdomain,
    get_deployment_by_uuid,
    get_used_deployment_ports,
    update_deployment,
    update_deployment_log,
)
from utils.deploy_checks import (
    assign_free_port,
    check_dns_propagated,
    get_used_ports_from_nginx,
    redact_pat,
)
from utils.validators import validate_env_key, validate_subdomain

_SEMAPHORE_LIMIT = int(os.getenv('DEPLOY_MAX_CONCURRENT', '2'))
_DEPLOY_TIMEOUT  = int(os.getenv('DEPLOY_TIMEOUT', '600'))
_PORT_MIN        = int(os.getenv('DEPLOYMENT_PORT_MIN', '3000'))
_PORT_MAX        = int(os.getenv('DEPLOYMENT_PORT_MAX', '3999'))
_NGINX_AVAILABLE = '/etc/nginx/sites-available'
_NGINX_ENABLED   = '/etc/nginx/sites-enabled'
_DEPLOY_BASE     = '/var/www'
_CERTBOT_EMAIL   = 'nydus@arvo.team'
_DOMAIN          = 'arvo.team'
_SERVER_IP       = os.getenv('SERVER_IP', '')
_PHP_FPM_SOCKET  = 'unix:/var/run/php/php8.2-fpm.sock'
_NODE_MEM_MB     = 512
_STREAM_TTL      = 300
_MAX_LINE        = 4096
_MAX_OUTPUT      = 2 * 1024 * 1024
_MAX_LOG_BYTES   = 500_000
_DNS_RETRIES     = 12
_DNS_DELAY       = 10.0
_DEV_ID          = int(os.getenv('DEV_ID', '0'))


class DeployError(Exception):
    pass


def _nginx_node_http(fqdn: str, port: int) -> str:
    return (
        f"server {{\n"
        f"    listen 80;\n"
        f"    server_name {fqdn};\n"
        f"\n"
        f"    location / {{\n"
        f"        proxy_pass http://localhost:{port};\n"
        f"        proxy_http_version 1.1;\n"
        f"        proxy_set_header Upgrade $http_upgrade;\n"
        f"        proxy_set_header Connection upgrade;\n"
        f"        proxy_set_header Host $host;\n"
        f"        proxy_cache_bypass $http_upgrade;\n"
        f"        proxy_set_header X-Real-IP $remote_addr;\n"
        f"        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        f"        proxy_set_header X-Forwarded-Proto $scheme;\n"
        f"        proxy_set_header X-Geo-Country $http_cf_ipcountry;\n"
        f"        proxy_set_header X-Geo-Region $http_cf_region;\n"
        f"        proxy_set_header X-Geo-City $http_cf_ipcity;\n"
        f"        proxy_set_header X-Geo-Lat $http_cf_iplatitude;\n"
        f"        proxy_set_header X-Geo-Long $http_cf_iplongitude;\n"
        f"    }}\n"
        f"}}\n"
    )


def _nginx_node_ssl(fqdn: str, port: int) -> str:
    return (
        f"server {{\n"
        f"    listen 80;\n"
        f"    server_name {fqdn};\n"
        f"    return 301 https://$host$request_uri;\n"
        f"}}\n"
        f"\n"
        f"server {{\n"
        f"    listen 443 ssl http2;\n"
        f"    server_name {fqdn};\n"
        f"\n"
        f"    ssl_certificate /etc/letsencrypt/live/{fqdn}/fullchain.pem;\n"
        f"    ssl_certificate_key /etc/letsencrypt/live/{fqdn}/privkey.pem;\n"
        f"\n"
        f"    access_log /var/log/nginx/{fqdn}.access.log db_log;\n"
        f"\n"
        f"    location / {{\n"
        f"        proxy_pass http://localhost:{port};\n"
        f"        proxy_http_version 1.1;\n"
        f"        proxy_set_header Upgrade $http_upgrade;\n"
        f"        proxy_set_header Connection upgrade;\n"
        f"        proxy_set_header Host $host;\n"
        f"        proxy_cache_bypass $http_upgrade;\n"
        f"        proxy_set_header X-Real-IP $remote_addr;\n"
        f"        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        f"        proxy_set_header X-Forwarded-Proto $scheme;\n"
        f"        proxy_set_header X-Geo-Country $http_cf_ipcountry;\n"
        f"        proxy_set_header X-Geo-Region $http_cf_region;\n"
        f"        proxy_set_header X-Geo-City $http_cf_ipcity;\n"
        f"        proxy_set_header X-Geo-Lat $http_cf_iplatitude;\n"
        f"        proxy_set_header X-Geo-Long $http_cf_iplongitude;\n"
        f"    }}\n"
        f"}}\n"
    )


def _nginx_laravel_http(fqdn: str, deploy_path: str) -> str:
    return (
        f"server {{\n"
        f"    listen 80;\n"
        f"    server_name {fqdn};\n"
        f"    root {deploy_path}/public;\n"
        f"    index index.php index.html;\n"
        f"\n"
        f"    location / {{\n"
        f"        try_files $uri $uri/ /index.php?$query_string;\n"
        f"    }}\n"
        f"\n"
        f"    location ~ \\.php$ {{\n"
        f"        fastcgi_pass {_PHP_FPM_SOCKET};\n"
        f"        fastcgi_index index.php;\n"
        f"        fastcgi_param SCRIPT_FILENAME $realpath_root$fastcgi_script_name;\n"
        f"        include fastcgi_params;\n"
        f"    }}\n"
        f"\n"
        f"    location ~ /\\.(?!well-known).* {{\n"
        f"        deny all;\n"
        f"    }}\n"
        f"}}\n"
    )


def _nginx_laravel_ssl(fqdn: str, deploy_path: str) -> str:
    return (
        f"server {{\n"
        f"    listen 80;\n"
        f"    server_name {fqdn};\n"
        f"    return 301 https://$host$request_uri;\n"
        f"}}\n"
        f"\n"
        f"server {{\n"
        f"    listen 443 ssl http2;\n"
        f"    server_name {fqdn};\n"
        f"\n"
        f"    ssl_certificate /etc/letsencrypt/live/{fqdn}/fullchain.pem;\n"
        f"    ssl_certificate_key /etc/letsencrypt/live/{fqdn}/privkey.pem;\n"
        f"\n"
        f"    root {deploy_path}/public;\n"
        f"    index index.php index.html;\n"
        f"\n"
        f"    access_log /var/log/nginx/{fqdn}.access.log db_log;\n"
        f"\n"
        f"    location / {{\n"
        f"        try_files $uri $uri/ /index.php?$query_string;\n"
        f"    }}\n"
        f"\n"
        f"    location ~ \\.php$ {{\n"
        f"        fastcgi_pass {_PHP_FPM_SOCKET};\n"
        f"        fastcgi_index index.php;\n"
        f"        fastcgi_param SCRIPT_FILENAME $realpath_root$fastcgi_script_name;\n"
        f"        include fastcgi_params;\n"
        f"    }}\n"
        f"\n"
        f"    location ~ /\\.(?!well-known).* {{\n"
        f"        deny all;\n"
        f"    }}\n"
        f"}}\n"
    )


class DeploymentCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = logging.getLogger('nydus')
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(_SEMAPHORE_LIMIT)
        self._project_locks: dict[str, asyncio.Lock] = {}
        self._active_streams: dict[str, asyncio.Queue] = {}

    async def branch_exists(self, git_url: str, branch: str, pat: str = "") -> bool:
        """Check if a branch exists in the remote repository."""
        # Build authenticated URL if PAT is provided
        if pat and git_url.startswith('https://'):
            git_url = git_url.replace('https://', f'https://{pat}@')
        
        # Use git ls-remote to check branch
        cmd = ['git', 'ls-remote', '--heads', git_url, branch]
        code, out, _ = await self.run_exec(cmd, timeout=30)
        
        if code != 0:
            return False
        # Output will be empty if branch not found
        return bool(out.strip())

    async def get_default_branch(self, git_url: str, pat: str = "") -> str | None:
        """Detect the default branch of the remote repository."""
        # Build authenticated URL if PAT is provided
        if pat and git_url.startswith('https://'):
            git_url = git_url.replace('https://', f'https://{pat}@')
        
        # Use git ls-remote to get the default branch
        # Output format: "ref: refs/heads/main\tHEAD" or "ref: refs/heads/master\tHEAD"
        cmd = ['git', 'ls-remote', '--symref', git_url, 'HEAD']
        code, out, _ = await self.run_exec(cmd, timeout=30)
        
        if code != 0 or not out.strip():
            return None
        
        # Parse the output to extract branch name
        # Format: ref: refs/heads/BRANCH_NAME\tHEAD
        for line in out.strip().split('\n'):
            if line.startswith('ref:'):
                # Extract branch name from "ref: refs/heads/branch_name"
                parts = line.split('refs/heads/')
                if len(parts) > 1:
                    branch = parts[1].split('\t')[0].strip()
                    return branch if branch else None
        return None

    async def get_local_git_remote_url(self, cwd: str) -> str | None:
        """Get the remote URL from a local git repository."""
        cmd = ['git', 'config', '--get', 'remote.origin.url']
        code, out, _ = await self.run_exec(cmd, cwd=cwd, timeout=10)
        if code == 0:
            return out.strip()
        return None

    def _get_project_lock(self, project_uuid: str) -> asyncio.Lock:
        if project_uuid not in self._project_locks:
            self._project_locks[project_uuid] = asyncio.Lock()
        return self._project_locks[project_uuid]

    async def run_exec_stream(
        self,
        args: list,
        cwd: str = None,
        env_extra: dict = None,
        timeout: int = None,
    ):
        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)
        timeout = timeout or _DEPLOY_TIMEOUT
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            
            stdout_lines = []
            stderr_lines = []
            output_queue = asyncio.Queue()
            
            async def read_stream(stream, lines_list):
                """Read from a stream and put lines in queue."""
                try:
                    while True:
                        line = await stream.readline()
                        if not line:
                            break
                        decoded = line.decode(errors='replace').rstrip()
                        lines_list.append(decoded)
                        await output_queue.put((None, decoded))
                except asyncio.CancelledError:
                    pass
            
            try:
                async with asyncio.timeout(timeout):
                    # Start concurrent readers for both stdout and stderr
                    stdout_task = asyncio.create_task(read_stream(process.stdout, stdout_lines))
                    stderr_task = asyncio.create_task(read_stream(process.stderr, stderr_lines))
                    
                    # Start a task to wait for process completion
                    process_task = asyncio.create_task(process.wait())
                    
                    # Yield all queued output lines
                    while not process_task.done() or not output_queue.empty():
                        try:
                            item = await asyncio.wait_for(output_queue.get(), timeout=0.1)
                            yield item
                        except asyncio.TimeoutError:
                            # Check if process is done
                            if process_task.done():
                                break
                            continue
                    
                    # Wait for all tasks to complete
                    await asyncio.gather(stdout_task, stderr_task, process_task)
                    
            except asyncio.TimeoutError:
                process.kill()
                stdout_task.cancel()
                stderr_task.cancel()
                try:
                    await asyncio.gather(stdout_task, stderr_task, process_task)
                except asyncio.CancelledError:
                    pass
                yield (None, f'Timed out after {timeout}s')
                return
            
            out = '\n'.join(stdout_lines)[:_MAX_OUTPUT] if stdout_lines else ''
            err = '\n'.join(stderr_lines)[:_MAX_OUTPUT] if stderr_lines else ''
            yield (process.returncode, out, err)
        except Exception as e:
            yield (-1, '', f'Exec error: {e}')

    async def run_exec(
        self,
        args: list,
        cwd: str = None,
        env_extra: dict = None,
        timeout: int = None,
    ) -> tuple[int, str, str]:
        last_code = 0
        out_parts = []
        err_parts = []
        async for result in self.run_exec_stream(args, cwd, env_extra, timeout):
            if len(result) == 2:
                code, line = result
                if code is None:
                    continue
                else:
                    last_code = code
            else:
                code, out, err = result
                return code, out, err
        return last_code, '\n'.join(out_parts), '\n'.join(err_parts)

    def get_stream(self, run_id: str):
        return self._active_streams.get(run_id)

    def queue_deploy(
        self,
        project_data: dict,
        subdomain: str,
        pat: str,
        triggered_by: str,
    ) -> str:
        run_id = str(uuid_lib.uuid4())
        self._active_streams[run_id] = asyncio.Queue()
        asyncio.create_task(
            self._run_and_cleanup(run_id, project_data, subdomain, pat, triggered_by)
        )
        return run_id

    async def _run_and_cleanup(
        self,
        run_id: str,
        project_data: dict,
        subdomain: str,
        pat: str,
        triggered_by: str,
    ):
        try:
            await self.deploy_project(run_id, project_data, subdomain, pat, triggered_by)
        except Exception as e:
            self.logger.exception(f"Unhandled deploy error [{run_id}]: {e}")
            q = self._active_streams.get(run_id)
            if q:
                await q.put(f"[FATAL] Unhandled error: {e}")
        finally:
            q = self._active_streams.get(run_id)
            if q:
                await q.put(None)
            await asyncio.sleep(_STREAM_TTL)
            self._active_streams.pop(run_id, None)

    async def _emit(
        self,
        run_id: str,
        log_lines: list,
        line: str,
        pat: str = '',
    ):
        if pat:
            line = redact_pat(line, pat)
        line = line.rstrip()[:_MAX_LINE]
        log_lines.append(line)
        self.logger.info(f"[deploy:{run_id[:8]}] {line}")
        q = self._active_streams.get(run_id)
        if q:
            await q.put(line)

    async def deploy_project(
        self,
        run_id: str,
        project_data: dict,
        subdomain: str,
        pat: str,
        triggered_by: str,
    ):
        log_lines: list[str] = []
        loop = asyncio.get_running_loop()

        async def emit(line: str):
            await self._emit(run_id, log_lines, line, pat)

        stack:           str | None = None
        deployment_uuid: str | None = None
        success:         bool       = False

        cleanup = {
            'deploy_path':   None,
            'makedirs':      False,
            'cloned':        False,
            'nginx_config':  None,
            'nginx_symlink': None,
            'cf_record_id':  None,
            'pm2_name':      None,
            'deployment_uuid': None,
        }

        project_uuid = project_data['project_uuid']
        name         = project_data['name']
        git_url      = project_data['git_url']
        branch = project_data.get('default_branch', 'main')
        await emit(f"[GIT] Checking if branch '{branch}' exists in remote...")
        branch_valid = await self.branch_exists(git_url, branch, pat)
        if not branch_valid:
            await emit(f"[GIT] Branch '{branch}' not found. Attempting to detect repository default branch...")
            detected_branch = await self.get_default_branch(git_url, pat)
            if detected_branch:
                await emit(f"[GIT] Detected default branch: '{detected_branch}'")
                branch = detected_branch
                branch_valid = True
            else:
                await emit(f"[FAIL] Could not find branch '{branch}' or detect repository default branch.")
                raise DeployError(f"Branch '{branch}' not found and could not detect default.")
        await emit("[GIT] Branch exists.")

        fqdn         = f"{subdomain}.{_DOMAIN}"
        deploy_path  = os.path.join(_DEPLOY_BASE, subdomain)

        cleanup['deploy_path']   = deploy_path
        nginx_config_path        = os.path.join(_NGINX_AVAILABLE, fqdn)
        nginx_symlink_path       = os.path.join(_NGINX_ENABLED, fqdn)

        await emit(f"[START] Deployment initiated: {name} -> {fqdn} | run={run_id}")

        async with self._semaphore:
            lock = self._get_project_lock(project_uuid)
            if lock.locked():
                await emit("[WAIT] Another deployment is active for this project. Queued.")

            async with lock:
                try:
                    valid, reason = validate_subdomain(subdomain)
                    if not valid:
                        await emit(f"[FAIL] {reason}")
                        raise DeployError(reason)

                    if not _SERVER_IP:
                        await emit("[FAIL] SERVER_IP is not configured.")
                        raise DeployError("SERVER_IP not set.")

                    existing = await get_deployment_by_subdomain(subdomain)
                    if existing:
                        await emit(f"[FAIL] Subdomain '{subdomain}' is already active.")
                        raise DeployError("Subdomain already in use.")

                    await emit("[CHECK] Pre-flight passed.")

                    await emit(f"[GIT] Preparing repository on branch '{branch}'...")

                    pat_url = (
                        git_url.replace('https://', f'https://{pat}@')
                        if pat else git_url
                    )

                    path_exists = await loop.run_in_executor(
                        None, os.path.exists, deploy_path
                    )

                    if not path_exists:
                        await loop.run_in_executor(
                            None, lambda: os.makedirs(deploy_path, exist_ok=True)
                        )
                        cleanup['makedirs'] = True
                        await emit(f"[GIT] Created directory {deploy_path}.")

                        async for result in self.run_exec_stream(
                            ['git', 'clone', '-b', branch, pat_url, '.'],
                            cwd=deploy_path,
                        ):
                            if len(result) == 2:
                                code, line = result
                                if code is None:
                                    if line.strip():
                                        await emit(f"[GIT] {line.strip()}")
                                else:
                                    await emit(f"[GIT] git clone finished with code {code}")
                            else:
                                code, out, err = result
                                for line in (out + err).splitlines():
                                    if line.strip():
                                        await emit(f"[GIT] {line.strip()}")
                                if code != 0:
                                    await emit(f"[FAIL] git clone failed (exit {code}).")
                                    raise DeployError("git clone failed.")
                        cleanup['cloned'] = True
                        await emit("[GIT] Clone successful.")
                    else:
                        await emit(f"[GIT] Directory exists. Fetching origin/{branch}...")
                        for step in [
                            ['git', 'fetch', 'origin', branch],
                            ['git', 'reset', '--hard', f'origin/{branch}'],
                        ]:
                            async for result in self.run_exec_stream(step, cwd=deploy_path):
                                if len(result) == 2:
                                    code, line = result
                                    if code is None:
                                        if line.strip():
                                            await emit(f"[GIT] {line.strip()}")
                                    else:
                                        await emit(f"[GIT] {' '.join(step)} finished with code {code}")
                                else:
                                    code, out, err = result
                                    for line in (out + err).splitlines():
                                        if line.strip():
                                            await emit(f"[GIT] {line.strip()}")
                                    if code != 0:
                                        await emit(f"[FAIL] {' '.join(step)} failed (exit {code}).")
                                        raise DeployError("Git update failed.")
                        await emit("[GIT] Repository updated.")

                    await emit("[DETECT] Detecting stack...")

                    has_package_json = await loop.run_in_executor(
                        None, os.path.exists, os.path.join(deploy_path, 'package.json')
                    )
                    has_artisan = await loop.run_in_executor(
                        None, os.path.exists, os.path.join(deploy_path, 'artisan')
                    )
                    has_composer_json = await loop.run_in_executor(
                        None, os.path.exists, os.path.join(deploy_path, 'composer.json')
                    )

                    has_vite = False
                    for vite_file in ['vite.config.js', 'vite.config.ts', 'vite.config.mjs']:
                        if await loop.run_in_executor(
                            None, os.path.exists, os.path.join(deploy_path, vite_file)
                        ):
                            has_vite = True
                            break

                    if has_artisan and has_composer_json:
                        stack = 'laravel'
                    elif has_package_json:
                        stack = 'node'
                    else:
                        await emit("[FAIL] No recognizable stack detected (no package.json or artisan).")
                        raise DeployError("Unsupported stack.")

                    await emit(f"[DETECT] Stack: {stack}.")

                    env_file_name = '.env.production' if (stack == 'node' and has_vite) else '.env'
                    await emit(f"[ENV] Target env file: {env_file_name}")

                    example_path = os.path.join(deploy_path, '.env.example')
                    env_path     = os.path.join(deploy_path, env_file_name)

                    example_exists = await loop.run_in_executor(None, os.path.exists, example_path)
                    env_exists     = await loop.run_in_executor(None, os.path.exists, env_path)

                    if example_exists and not env_exists:
                        def _copy_env():
                            shutil.copy2(example_path, env_path)

                        await loop.run_in_executor(None, _copy_env)
                        await emit(f"[ENV] Copied .env.example -> {env_file_name}.")

                        def _read_example():
                            with open(example_path, 'r', errors='replace') as f:
                                return f.readlines()

                        raw_lines = await loop.run_in_executor(None, _read_example)
                        for raw in raw_lines:
                            stripped = raw.strip()
                            if stripped and not stripped.startswith('#') and '=' in stripped:
                                key = stripped.split('=', 1)[0].strip()
                                await emit(f"[ENV] Variable: {key}")

                    elif env_exists:
                        await emit(f"[ENV] {env_file_name} already exists. Skipping copy.")
                    else:
                        await emit("[ENV] No .env.example found. Continuing without env copy.")

                    if stack == 'node':
                        await emit("[INSTALL] Running npm install...")
                        async for result in self.run_exec_stream(
                            ['npm', 'install'], cwd=deploy_path
                        ):
                            if len(result) == 2:
                                code, line = result
                                if code is None:
                                    if line.strip():
                                        await emit(f"[INSTALL] {line.strip()}")
                                else:
                                    await emit(f"[INSTALL] npm install finished with code {code}")
                            else:
                                code, out, err = result
                                for line in (out + err).splitlines():
                                    if line.strip():
                                        await emit(f"[INSTALL] {line.strip()}")
                                if code != 0:
                                    await emit(f"[FAIL] npm install failed (exit {code}).")
                                    raise DeployError("npm install failed.")
                        await emit("[INSTALL] npm install complete.")
                    elif stack == 'laravel':
                        await emit("[INSTALL] Running composer install...")
                        async for result in self.run_exec_stream(
                            ['composer', 'install', '--no-dev', '--optimize-autoloader'],
                            cwd=deploy_path,
                        ):
                            if len(result) == 2:
                                code, line = result
                                if code is None:
                                    if line.strip():
                                        await emit(f"[INSTALL] {line.strip()}")
                                else:
                                    await emit(f"[INSTALL] composer install finished with code {code}")
                            else:
                                code, out, err = result
                                for line in (out + err).splitlines():
                                    if line.strip():
                                        await emit(f"[INSTALL] {line.strip()}")
                                if code != 0:
                                    await emit(f"[FAIL] composer install failed (exit {code}).")
                                    raise DeployError("composer install failed.")
                        await emit("[INSTALL] composer install complete.")

                    if stack == 'node':
                        await emit("[BUILD] Running npm run build...")
                        async for result in self.run_exec_stream(
                            ['npm', 'run', 'build'],
                            cwd=deploy_path,
                            env_extra={'NODE_OPTIONS': f'--max-old-space-size={_NODE_MEM_MB}'},
                        ):
                            if len(result) == 2:
                                code, line = result
                                if code is None:
                                    if line.strip():
                                        await emit(f"[BUILD] {line.strip()}")
                                else:
                                    await emit(f"[BUILD] npm run build finished with code {code}")
                            else:
                                code, out, err = result
                                for line in (out + err).splitlines():
                                    if line.strip():
                                        await emit(f"[BUILD] {line.strip()}")
                                if code != 0:
                                    await emit(f"[FAIL] npm run build failed (exit {code}).")
                                    raise DeployError("npm build failed.")
                        await emit("[BUILD] Build complete.")
                    elif stack == 'laravel':
                        await emit("[BUILD] Running Laravel artisan setup...")
                        for artisan_cmd in [
                            ['php', 'artisan', 'migrate', '--force'],
                            ['php', 'artisan', 'config:cache'],
                            ['php', 'artisan', 'route:cache'],
                            ['php', 'artisan', 'view:cache'],
                        ]:
                            async for result in self.run_exec_stream(artisan_cmd, cwd=deploy_path):
                                if len(result) == 2:
                                    code, line = result
                                    if code is None:
                                        if line.strip():
                                            await emit(f"[BUILD] {line.strip()}")
                                    else:
                                        await emit(f"[BUILD] {' '.join(artisan_cmd)} finished with code {code}")
                                else:
                                    code, out, err = result
                                    for line in (out + err).splitlines():
                                        if line.strip():
                                            await emit(f"[BUILD] {line.strip()}")
                                    if code != 0:
                                        await emit(f"[FAIL] {' '.join(artisan_cmd)} failed (exit {code}).")
                                        raise DeployError("Artisan command failed.")
                        await emit("[BUILD] Laravel setup complete.")

                    assigned_port: int | None = None
                    if stack == 'node':
                        await emit("[PORT] Scanning for an available port...")
                        
                        # Manually check nginx config files one by one (except default)
                        def _scan_nginx_ports():
                            ports = set()
                            try:
                                for config_file in os.listdir(_NGINX_AVAILABLE):
                                    if config_file == 'default':
                                        continue
                                    config_path = os.path.join(_NGINX_AVAILABLE, config_file)
                                    if not os.path.isfile(config_path):
                                        continue
                                    with open(config_path, 'r', errors='replace') as f:
                                        content = f.read()
                                        for line in content.splitlines():
                                            line = line.strip()
                                            if 'proxy_pass http://localhost:' in line:
                                                try:
                                                    port_str = line.split('localhost:')[1].split(';')[0].split('/')[0].strip()
                                                    port = int(port_str)
                                                    ports.add(port)
                                                except (ValueError, IndexError):
                                                    pass
                            except Exception:
                                pass
                            return ports
                        
                        nginx_ports = await loop.run_in_executor(None, _scan_nginx_ports)
                        db_ports      = await get_used_deployment_ports()
                        used_ports    = nginx_ports | db_ports
                        assigned_port = assign_free_port(used_ports, _PORT_MIN, _PORT_MAX)
                        if assigned_port is None:
                            await emit(f"[FAIL] No ports available in range {_PORT_MIN}-{_PORT_MAX}.")
                            raise DeployError("Port exhaustion.")
                        await emit(f"[PORT] Assigned port {assigned_port}.")

                    await emit("[DB] Saving deployment record...")
                    deployment_uuid = await create_deployment(
                        project_uuid=project_uuid,
                        subdomain=subdomain,
                        tech_stack=stack,
                        assigned_port=assigned_port,
                        deploy_path=deploy_path,
                        env_file_name=env_file_name,
                        deployed_by=triggered_by,
                        branch=branch,
                    )
                    cleanup['deployment_uuid'] = deployment_uuid

                    await create_deployment_log(
                        run_uuid=run_id,
                        deployment_uuid=deployment_uuid,
                        project_uuid=project_uuid,
                        triggered_by=triggered_by,
                    )
                    await emit(f"[DB] Deployment UUID: {deployment_uuid}.")

                    await emit("[NGINX] Writing initial HTTP nginx config...")

                    http_config = (
                        _nginx_node_http(fqdn, assigned_port)
                        if stack == 'node'
                        else _nginx_laravel_http(fqdn, deploy_path)
                    )

                    def _write_http():
                        with open(nginx_config_path, 'w') as f:
                            f.write(http_config)

                    await loop.run_in_executor(None, _write_http)
                    cleanup['nginx_config'] = nginx_config_path
                    await emit(f"[NGINX] Config written: {nginx_config_path}.")

                    def _create_symlink():
                        if os.path.islink(nginx_symlink_path):
                            os.remove(nginx_symlink_path)
                        os.symlink(nginx_config_path, nginx_symlink_path)

                    await loop.run_in_executor(None, _create_symlink)
                    cleanup['nginx_symlink'] = nginx_symlink_path
                    await emit("[NGINX] Symlink created in sites-enabled.")

                    code, out, err = await self.run_exec(['sudo', 'nginx', '-t'], timeout=30)
                    for line in (out + err).splitlines():
                        if line.strip():
                            await emit(f"[NGINX] {line.strip()}")
                    if code != 0:
                        await emit("[FAIL] nginx config test failed (HTTP).")
                        raise DeployError("nginx -t failed (HTTP).")

                    code, _, err = await self.run_exec(
                        ['sudo', 'systemctl', 'reload', 'nginx'], timeout=30
                    )
                    if code != 0:
                        await emit(f"[FAIL] nginx reload failed: {err.strip()}")
                        raise DeployError("nginx reload failed (HTTP).")
                    await emit("[NGINX] nginx reloaded with HTTP config.")

                    await emit(f"[DNS] Creating DNS A record for {fqdn} (unproxied)...")

                    cf_cog = self.bot.get_cog('CloudflareCog')
                    if not cf_cog:
                        await emit("[FAIL] CloudflareCog is not loaded.")
                        raise DeployError("CloudflareCog unavailable.")

                    cf_record, cf_error = await cf_cog.create_dns_record(
                        type='A',
                        name=subdomain,
                        content=_SERVER_IP,
                        ttl=60,
                        proxied=False,
                        comment=f"nydus | run={run_id}",
                    )
                    if cf_error:
                        await emit(f"[FAIL] Cloudflare DNS error: {cf_error}")
                        raise DeployError(f"Cloudflare DNS failed: {cf_error}")

                    cleanup['cf_record_id'] = cf_record['id']
                    await update_deployment(deployment_uuid, cf_record_id=cf_record['id'])
                    await emit(f"[DNS] Record created (unproxied). ID: {cf_record['id']}")

                    await emit(
                        f"[DNS] Waiting for propagation "
                        f"(up to {int(_DNS_RETRIES * _DNS_DELAY)}s)..."
                    )
                    
                    # Create a task for DNS propagation check
                    propagation_task = asyncio.create_task(
                        check_dns_propagated(fqdn, _SERVER_IP, _DNS_RETRIES, _DNS_DELAY)
                    )
                    
                    # Keep-alive pings while waiting (without logging)
                    q = self._active_streams.get(run_id)
                    while not propagation_task.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(propagation_task), timeout=15)
                        except asyncio.TimeoutError:
                            if q:
                                await q.put("[DNS] Still waiting for propagation...")
                    
                    propagated = propagation_task.result()
                    if propagated:
                        await emit("[DNS] DNS propagated successfully.")
                    else:
                        await emit(
                            "[DNS] Propagation check timed out. "
                            "Proceeding anyway; certbot may fail."
                        )

                    await emit(f"[SSL] Obtaining Let's Encrypt certificate for {fqdn}...")
                    code, out, err = await self.run_exec(
                        [
                            'sudo', 'certbot', 'certonly', '--nginx',
                            '-d', fqdn,
                            '--non-interactive',
                            '--agree-tos',
                            '-m', _CERTBOT_EMAIL,
                        ],
                        timeout=180,
                    )
                    for line in (out + err).splitlines():
                        if line.strip():
                            await emit(f"[SSL] {line.strip()}")
                    if code != 0:
                        await emit(f"[FAIL] certbot failed (exit {code}).")
                        raise DeployError("certbot SSL provisioning failed.")
                    await emit(f"[SSL] Certificate obtained for {fqdn}.")

                    await emit("[NGINX] Writing full SSL nginx config...")

                    ssl_config = (
                        _nginx_node_ssl(fqdn, assigned_port)
                        if stack == 'node'
                        else _nginx_laravel_ssl(fqdn, deploy_path)
                    )

                    def _write_ssl():
                        with open(nginx_config_path, 'w') as f:
                            f.write(ssl_config)

                    await loop.run_in_executor(None, _write_ssl)
                    await emit("[NGINX] SSL config written.")

                    code, out, err = await self.run_exec(['sudo', 'nginx', '-t'], timeout=30)
                    for line in (out + err).splitlines():
                        if line.strip():
                            await emit(f"[NGINX] {line.strip()}")
                    if code != 0:
                        await emit("[FAIL] nginx config test failed (SSL).")
                        raise DeployError("nginx -t failed (SSL).")

                    code, _, err = await self.run_exec(
                        ['sudo', 'systemctl', 'reload', 'nginx'], timeout=30
                    )
                    if code != 0:
                        await emit(f"[FAIL] nginx reload failed (SSL): {err.strip()}")
                        raise DeployError("nginx reload failed (SSL).")
                    await emit("[NGINX] nginx reloaded with SSL config.")

                    await emit("[DNS] Enabling Cloudflare proxy on DNS record...")
                    _, cf_upd_err = await cf_cog.update_dns_record(
                        record_id=cf_record['id'],
                        type='A',
                        name=subdomain,
                        content=_SERVER_IP,
                        ttl=1,
                        proxied=True,
                        comment=f"nydus | run={run_id}",
                    )
                    if cf_upd_err:
                        await emit(
                            f"[WARN] Could not enable Cloudflare proxy: {cf_upd_err}. "
                            "Site is live but not proxied yet."
                        )
                    else:
                        await emit("[DNS] Cloudflare proxy enabled.")

                    pm2_name = deployment_uuid[:12]
                    if stack == 'node':
                        await emit(f"[PM2] Starting process '{pm2_name}' on port {assigned_port}...")

                        code_desc, _, _ = await self.run_exec(['pm2', 'describe', pm2_name])
                        if code_desc == 0:
                            async for result in self.run_exec_stream(
                                ['pm2', 'reload', pm2_name], cwd=deploy_path
                            ):
                                if len(result) == 2:
                                    code, line = result
                                    if code is None:
                                        if line.strip():
                                            await emit(f"[PM2] {line.strip()}")
                                else:
                                    code, out, err = result
                                    for line in (out + err).splitlines():
                                        if line.strip():
                                            await emit(f"[PM2] {line.strip()}")
                                    if code != 0:
                                        await emit(f"[FAIL] pm2 reload failed (exit {code}).")
                                        raise DeployError("pm2 reload failed.")
                        else:
                            async for result in self.run_exec_stream(
                                ['pm2', 'start', 'npm', '--name', pm2_name, '--', 'start'],
                                cwd=deploy_path,
                                env_extra={'PORT': str(assigned_port)},
                            ):
                                if len(result) == 2:
                                    code, line = result
                                    if code is None:
                                        if line.strip():
                                            await emit(f"[PM2] {line.strip()}")
                                else:
                                    code, out, err = result
                                    for line in (out + err).splitlines():
                                        if line.strip():
                                            await emit(f"[PM2] {line.strip()}")
                                    if code != 0:
                                        await emit(f"[FAIL] pm2 start failed (exit {code}).")
                                        raise DeployError("pm2 start failed.")

                        cleanup['pm2_name'] = pm2_name
                        await emit(f"[PM2] Process '{pm2_name}' running on port {assigned_port}.")

                    await emit("[CHECK] Running final verification...")

                    cert_path = f"/etc/letsencrypt/live/{fqdn}/fullchain.pem"
                    cert_ok = await loop.run_in_executor(None, os.path.exists, cert_path)
                    if cert_ok:
                        await emit("[CHECK] SSL certificate confirmed on disk.")
                    else:
                        await emit(f"[WARN] SSL certificate not found at {cert_path}.")

                    code, out, err = await self.run_exec(['sudo', 'nginx', '-t'], timeout=30)
                    if code == 0:
                        await emit("[CHECK] nginx config is valid.")
                    else:
                        await emit("[WARN] nginx -t returned non-zero on final check.")

                    if stack == 'node':
                        code, _, _ = await self.run_exec(['pm2', 'describe', pm2_name])
                        if code == 0:
                            await emit(f"[CHECK] pm2 process '{pm2_name}' confirmed running.")
                        else:
                            await emit(f"[WARN] pm2 process '{pm2_name}' not confirmed.")

                    await emit("[HEALTH] Performing HTTP health check...")
                    health_ok = False
                    for attempt in range(5):
                        try:
                            async with aiohttp.ClientSession() as session:
                                async with session.get(f"https://{fqdn}", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                    if resp.status == 200:
                                        health_ok = True
                                        await emit(f"[HEALTH] Site responded with HTTP {resp.status} (attempt {attempt+1}/5).")
                                        break
                                    else:
                                        await emit(f"[HEALTH] HTTP {resp.status} (attempt {attempt+1}/5).")
                        except Exception as e:
                            await emit(f"[HEALTH] Error: {e} (attempt {attempt+1}/5).")
                        if attempt < 4:
                            await asyncio.sleep(3)
                    if health_ok:
                        await emit("[HEALTH] Health check passed.")
                    else:
                        await emit("[HEALTH] Health check failed. Site may be unreachable.")
                        await update_deployment(deployment_uuid, status='unhealthy')

                    await update_deployment(
                        deployment_uuid,
                        status='active',
                        deployed_at=datetime.now(timezone.utc),
                    )
                    success = True
                    await emit(f"[DONE] Deployment complete. Live at: https://{fqdn}")

                except DeployError as e:
                    await emit(f"[FAIL] {e}")
                    await self._cleanup_failed_deploy(cleanup, run_id, log_lines, pat, stack)

                except Exception as e:
                    self.logger.exception(f"Unexpected deploy error [{run_id}]: {e}")
                    await emit(f"[FATAL] Unexpected error: {e}")
                    await self._cleanup_failed_deploy(cleanup, run_id, log_lines, pat, stack)

                finally:
                    full_log = '\n'.join(log_lines)
                    if len(full_log) > _MAX_LOG_BYTES:
                        full_log = full_log[:_MAX_LOG_BYTES] + '\n[LOG TRUNCATED]'

                    if cleanup['deployment_uuid']:
                        if not success:
                            await update_deployment(cleanup['deployment_uuid'], status='failed')
                        await update_deployment_log(
                            run_id,
                            'success' if success else 'failed',
                            full_log,
                        )
                    else:
                        self.logger.error(
                            f"Deploy run {run_id} ended without a deployment_uuid. "
                            f"Partial log: {full_log[:500]}"
                        )

    async def _cleanup_failed_deploy(
        self,
        cleanup: dict,
        run_id: str,
        log_lines: list,
        pat: str,
        stack: str | None,
    ):
        loop = asyncio.get_running_loop()

        async def emit(line: str):
            await self._emit(run_id, log_lines, line, pat)

        await emit("[CLEANUP] Rolling back deployment...")

        if cleanup.get('pm2_name'):
            await emit(f"[CLEANUP] Stopping pm2 process '{cleanup['pm2_name']}'...")
            code, _, _ = await self.run_exec(['pm2', 'delete', cleanup['pm2_name']])
            if code == 0:
                await emit("[CLEANUP] pm2 process deleted.")
            else:
                await emit(
                    f"[CLEANUP] Warning: could not delete pm2 process '{cleanup['pm2_name']}'. "
                    "Manual cleanup may be required."
                )

        if cleanup.get('nginx_symlink'):
            def _rm_symlink():
                try:
                    os.remove(cleanup['nginx_symlink'])
                except OSError:
                    pass

            await loop.run_in_executor(None, _rm_symlink)
            await emit("[CLEANUP] nginx symlink removed.")

        if cleanup.get('nginx_config'):
            def _rm_config():
                try:
                    os.remove(cleanup['nginx_config'])
                except OSError:
                    pass

            await loop.run_in_executor(None, _rm_config)
            await emit("[CLEANUP] nginx config removed.")

        if cleanup.get('nginx_config') or cleanup.get('nginx_symlink'):
            code, out, err = await self.run_exec(['sudo', 'nginx', '-t'], timeout=30)
            if code == 0:
                await self.run_exec(['sudo', 'systemctl', 'reload', 'nginx'], timeout=30)
                await emit("[CLEANUP] nginx reloaded.")
            else:
                await emit(
                    "[CLEANUP] Warning: nginx -t failed after config removal. "
                    "Manual inspection of nginx is required."
                )

        if cleanup.get('cf_record_id'):
            await emit(f"[CLEANUP] Deleting Cloudflare DNS record {cleanup['cf_record_id']}...")
            cf_cog = self.bot.get_cog('CloudflareCog')
            if cf_cog:
                ok, err = await cf_cog.delete_dns_record(cleanup['cf_record_id'])
                if ok:
                    await emit("[CLEANUP] DNS record deleted.")
                else:
                    await emit(
                        f"[CLEANUP] Warning: could not delete DNS record: {err}. "
                        "Manual cleanup in Cloudflare may be required."
                    )
            else:
                await emit(
                    "[CLEANUP] Warning: CloudflareCog unavailable. "
                    f"DNS record {cleanup['cf_record_id']} was NOT deleted."
                )

        if cleanup.get('makedirs') and cleanup.get('deploy_path'):
            def _rm_dir():
                try:
                    shutil.rmtree(cleanup['deploy_path'])
                except Exception:
                    pass

            await loop.run_in_executor(None, _rm_dir)
            await emit(f"[CLEANUP] Removed deploy directory {cleanup['deploy_path']}.")

        await emit("[CLEANUP] Rollback complete.")

    async def delete_deployment(self, deployment_uuid: str) -> tuple[bool, str]:
        deployment = await get_deployment_by_uuid(deployment_uuid)
        if not deployment:
            return False, "Deployment not found."

        fqdn = f"{deployment['subdomain']}.{_DOMAIN}"
        loop = asyncio.get_running_loop()

        if deployment['tech_stack'] == 'node' and deployment['assigned_port']:
            pm2_name = deployment_uuid[:12]
            code, _, _ = await self.run_exec(['pm2', 'delete', pm2_name])
            if code != 0:
                self.logger.warning(f"Could not delete pm2 process {pm2_name} for {deployment_uuid}")

        nginx_config_path = os.path.join(_NGINX_AVAILABLE, fqdn)
        nginx_symlink_path = os.path.join(_NGINX_ENABLED, fqdn)

        def _remove_nginx():
            try:
                if os.path.islink(nginx_symlink_path):
                    os.remove(nginx_symlink_path)
                if os.path.exists(nginx_config_path):
                    os.remove(nginx_config_path)
            except Exception as e:
                self.logger.warning(f"Nginx cleanup error: {e}")

        await loop.run_in_executor(None, _remove_nginx)

        await self.run_exec(['sudo', 'systemctl', 'reload', 'nginx'], timeout=30)

        cf_cog = self.bot.get_cog('CloudflareCog')
        if cf_cog and deployment.get('cf_record_id'):
            await cf_cog.delete_dns_record(deployment['cf_record_id'])

        await self.run_exec(
            ['sudo', 'certbot', 'delete', '--cert-name', fqdn, '--non-interactive'],
            timeout=60
        )

        if deployment.get('deploy_path'):
            def _rm_deploy():
                try:
                    shutil.rmtree(deployment['deploy_path'])
                except Exception:
                    pass
            await loop.run_in_executor(None, _rm_deploy)

        await update_deployment(deployment_uuid, status='deleted')
        return True, "Deployment deleted successfully."

    async def get_env_lines(self, deployment_uuid: str) -> tuple[list, str]:
        deployment = await get_deployment_by_uuid(deployment_uuid)
        if not deployment:
            return [], "Deployment not found."

        env_path = os.path.join(deployment['deploy_path'], deployment['env_file_name'])
        loop = asyncio.get_running_loop()
        exists = await loop.run_in_executor(None, os.path.exists, env_path)
        if not exists:
            return [], f"{deployment['env_file_name']} not found."

        def _read():
            result = []
            with open(env_path, 'r', errors='replace') as f:
                for raw in f:
                    stripped = raw.strip()
                    if not stripped or stripped.startswith('#'):
                        continue
                    if '=' not in stripped:
                        continue
                    key, _, value = stripped.partition('=')
                    result.append({'key': key.strip(), 'value': value})
            return result

        try:
            return await loop.run_in_executor(None, _read), ""
        except Exception as e:
            return [], str(e)

    async def update_env_line(
        self, deployment_uuid: str, key: str, value: str
    ) -> tuple[bool, str]:
        valid, reason = validate_env_key(key)
        if not valid:
            return False, reason

        deployment = await get_deployment_by_uuid(deployment_uuid)
        if not deployment:
            return False, "Deployment not found."

        env_path = os.path.join(deployment['deploy_path'], deployment['env_file_name'])
        loop = asyncio.get_running_loop()

        def _update():
            if not os.path.exists(env_path):
                return False, f"{deployment['env_file_name']} not found."
            with open(env_path, 'r', errors='replace') as f:
                lines = f.readlines()
            new_lines = []
            found = False
            for line in lines:
                if line.strip().startswith(f'{key}='):
                    new_lines.append(f'{key}={value}\n')
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                return False, f"Key '{key}' not found."
            with open(env_path, 'w') as f:
                f.writelines(new_lines)
            return True, ""

        try:
            return await loop.run_in_executor(None, _update)
        except Exception as e:
            return False, str(e)

    async def add_env_line(
        self, deployment_uuid: str, key: str, value: str
    ) -> tuple[bool, str]:
        valid, reason = validate_env_key(key)
        if not valid:
            return False, reason

        deployment = await get_deployment_by_uuid(deployment_uuid)
        if not deployment:
            return False, "Deployment not found."

        env_path = os.path.join(deployment['deploy_path'], deployment['env_file_name'])
        loop = asyncio.get_running_loop()

        def _add():
            if not os.path.exists(env_path):
                return False, f"{deployment['env_file_name']} not found."
            with open(env_path, 'r', errors='replace') as f:
                content = f.read()
            if f'\n{key}=' in content or content.startswith(f'{key}='):
                return False, f"Key '{key}' already exists."
            with open(env_path, 'a') as f:
                if content and not content.endswith('\n'):
                    f.write('\n')
                f.write(f'{key}={value}\n')
            return True, ""

        try:
            return await loop.run_in_executor(None, _add)
        except Exception as e:
            return False, str(e)

    async def delete_env_line(
        self, deployment_uuid: str, key: str
    ) -> tuple[bool, str]:
        valid, reason = validate_env_key(key)
        if not valid:
            return False, reason

        deployment = await get_deployment_by_uuid(deployment_uuid)
        if not deployment:
            return False, "Deployment not found."

        env_path = os.path.join(deployment['deploy_path'], deployment['env_file_name'])
        loop = asyncio.get_running_loop()

        def _delete():
            if not os.path.exists(env_path):
                return False, f"{deployment['env_file_name']} not found."
            with open(env_path, 'r', errors='replace') as f:
                lines = f.readlines()
            new_lines = [l for l in lines if not l.strip().startswith(f'{key}=')]
            if len(new_lines) == len(lines):
                return False, f"Key '{key}' not found."
            with open(env_path, 'w') as f:
                f.writelines(new_lines)
            return True, ""

        try:
            return await loop.run_in_executor(None, _delete)
        except Exception as e:
            return False, str(e)

    async def get_packages(self, deployment_uuid: str) -> tuple[dict, str]:
        deployment = await get_deployment_by_uuid(deployment_uuid)
        if not deployment:
            return {}, "Deployment not found."

        deploy_path = deployment['deploy_path']
        stack       = deployment['tech_stack']
        loop        = asyncio.get_running_loop()

        if stack == 'node':
            pkg_path = os.path.join(deploy_path, 'package.json')

            def _read_node():
                if not os.path.exists(pkg_path):
                    return {}, "package.json not found."
                with open(pkg_path, 'r') as f:
                    data = json.load(f)
                return {
                    'dependencies':    data.get('dependencies', {}),
                    'devDependencies': data.get('devDependencies', {}),
                }, ""

            try:
                return await loop.run_in_executor(None, _read_node)
            except Exception as e:
                return {}, str(e)

        elif stack == 'laravel':
            comp_path = os.path.join(deploy_path, 'composer.json')

            def _read_laravel():
                if not os.path.exists(comp_path):
                    return {}, "composer.json not found."
                with open(comp_path, 'r') as f:
                    data = json.load(f)
                return {
                    'require':     data.get('require', {}),
                    'require-dev': data.get('require-dev', {}),
                }, ""

            try:
                return await loop.run_in_executor(None, _read_laravel)
            except Exception as e:
                return {}, str(e)

        return {}, f"Unsupported stack: {stack}"

    async def update_package_version(
        self,
        deployment_uuid: str,
        package: str,
        version: str,
        section: str,
    ) -> tuple[bool, str]:
        deployment = await get_deployment_by_uuid(deployment_uuid)
        if not deployment:
            return False, "Deployment not found."

        deploy_path = deployment['deploy_path']
        stack       = deployment['tech_stack']
        loop        = asyncio.get_running_loop()

        if stack == 'node':
            if section not in ('dependencies', 'devDependencies'):
                return False, "Section must be 'dependencies' or 'devDependencies'."
            pkg_path = os.path.join(deploy_path, 'package.json')

            def _update_node():
                if not os.path.exists(pkg_path):
                    return False, "package.json not found."
                with open(pkg_path, 'r') as f:
                    data = json.load(f)
                if package not in data.get(section, {}):
                    return False, f"Package '{package}' not found in {section}."
                data[section][package] = version
                with open(pkg_path, 'w') as f:
                    json.dump(data, f, indent=2)
                return True, ""

            try:
                return await loop.run_in_executor(None, _update_node)
            except Exception as e:
                return False, str(e)

        elif stack == 'laravel':
            if section not in ('require', 'require-dev'):
                return False, "Section must be 'require' or 'require-dev'."
            comp_path = os.path.join(deploy_path, 'composer.json')

            def _update_laravel():
                if not os.path.exists(comp_path):
                    return False, "composer.json not found."
                with open(comp_path, 'r') as f:
                    data = json.load(f)
                if package not in data.get(section, {}):
                    return False, f"Package '{package}' not found in {section}."
                data[section][package] = version
                with open(comp_path, 'w') as f:
                    json.dump(data, f, indent=2)
                return True, ""

            try:
                return await loop.run_in_executor(None, _update_laravel)
            except Exception as e:
                return False, str(e)

        return False, f"Unsupported stack: {stack}"

    def queue_rebuild(self, deployment_uuid: str, triggered_by: str) -> str:
        run_id = str(uuid_lib.uuid4())
        self._active_streams[run_id] = asyncio.Queue()
        asyncio.create_task(self._run_rebuild(run_id, deployment_uuid, triggered_by))
        return run_id

    async def _run_rebuild(
        self, run_id: str, deployment_uuid: str, triggered_by: str
    ):
        log_lines: list[str] = []
        success    = False
        log_created = False

        async def emit(line: str):
            await self._emit(run_id, log_lines, line)

        try:
            deployment = await get_deployment_by_uuid(deployment_uuid)
            if not deployment:
                await emit("[FAIL] Deployment not found.")
                return
            if deployment['status'] != 'active':
                await emit(f"[FAIL] Cannot rebuild: deployment status is '{deployment['status']}'.")
                return

            await create_deployment_log(
                run_uuid=run_id,
                deployment_uuid=deployment_uuid,
                project_uuid=deployment['project_uuid'],
                triggered_by=triggered_by,
            )
            log_created = True

            deploy_path   = deployment['deploy_path']
            stack         = deployment['tech_stack']
            assigned_port = deployment['assigned_port']
            pm2_name      = deployment_uuid[:12]
            branch        = deployment.get('branch', 'main')

            async with self._semaphore:
                lock = self._get_project_lock(deployment['project_uuid'])
                async with lock:
                    await emit(f"[REBUILD] Starting rebuild for {deployment_uuid[:8]}...")

                    # Try to detect the correct branch if the stored one doesn't exist
                    await emit(f"[REBUILD] Verifying branch '{branch}' exists...")
                    git_url = await self.get_local_git_remote_url(deploy_path)
                    if git_url:
                        branch_valid = await self.branch_exists(git_url, branch)
                        if not branch_valid:
                            await emit(f"[REBUILD] Branch '{branch}' not found. Detecting default branch...")
                            detected_branch = await self.get_default_branch(git_url)
                            if detected_branch:
                                await emit(f"[REBUILD] Using detected default branch: '{detected_branch}'")
                                branch = detected_branch
                            else:
                                await emit(f"[REBUILD] Could not detect default branch, will attempt with '{branch}' anyway.")
                        else:
                            await emit(f"[REBUILD] Branch '{branch}' verified.")
                    else:
                        await emit(f"[REBUILD] Could not get remote URL, proceeding with branch '{branch}'")

                    await emit("[REBUILD] Fetching latest code from git...")
                    for step in [
                        ['git', 'fetch', 'origin', branch],
                        ['git', 'reset', '--hard', f'origin/{branch}'],
                    ]:
                        async for result in self.run_exec_stream(step, cwd=deploy_path):
                            if len(result) == 2:
                                code, line = result
                                if code is None:
                                    if line.strip():
                                        await emit(f"[GIT] {line.strip()}")
                            else:
                                code, out, err = result
                                for line in (out + err).splitlines():
                                    if line.strip():
                                        await emit(f"[GIT] {line.strip()}")
                                if code != 0:
                                    await emit(f"[FAIL] {' '.join(step)} failed (exit {code}).")
                                    raise DeployError("Git update failed.")
                    await emit("[REBUILD] Git update complete.")

                    if stack == 'node':
                        await emit("[REBUILD] npm install...")
                        async for result in self.run_exec_stream(
                            ['npm', 'install'], cwd=deploy_path
                        ):
                            if len(result) == 2:
                                code, line = result
                                if code is None:
                                    if line.strip():
                                        await emit(f"[INSTALL] {line.strip()}")
                            else:
                                code, out, err = result
                                for line in (out + err).splitlines():
                                    if line.strip():
                                        await emit(f"[INSTALL] {line.strip()}")
                                if code != 0:
                                    await emit(f"[FAIL] npm install failed (exit {code}).")
                                    raise DeployError("npm install failed.")

                        await emit("[REBUILD] npm run build...")
                        async for result in self.run_exec_stream(
                            ['npm', 'run', 'build'],
                            cwd=deploy_path,
                            env_extra={'NODE_OPTIONS': f'--max-old-space-size={_NODE_MEM_MB}'},
                        ):
                            if len(result) == 2:
                                code, line = result
                                if code is None:
                                    if line.strip():
                                        await emit(f"[BUILD] {line.strip()}")
                            else:
                                code, out, err = result
                                for line in (out + err).splitlines():
                                    if line.strip():
                                        await emit(f"[BUILD] {line.strip()}")
                                if code != 0:
                                    await emit(f"[FAIL] Build failed (exit {code}).")
                                    raise DeployError("Build failed.")

                        await emit(f"[REBUILD] Restarting pm2 process '{pm2_name}'...")
                        code, out, err = await self.run_exec(
                            ['pm2', 'reload', pm2_name], cwd=deploy_path
                        )
                        if code != 0:
                            await emit("[REBUILD] pm2 reload failed, trying pm2 start...")
                            code, out, err = await self.run_exec(
                                ['pm2', 'start', 'npm', '--name', pm2_name, '--', 'start'],
                                cwd=deploy_path,
                                env_extra={'PORT': str(assigned_port)},
                            )
                        for line in (out + err).splitlines():
                            if line.strip():
                                await emit(f"[PM2] {line.strip()}")
                        if code != 0:
                            await emit(f"[FAIL] pm2 failed (exit {code}).")
                            raise DeployError("pm2 failed.")

                    elif stack == 'laravel':
                        await emit("[REBUILD] composer install...")
                        async for result in self.run_exec_stream(
                            ['composer', 'install', '--no-dev', '--optimize-autoloader'],
                            cwd=deploy_path,
                        ):
                            if len(result) == 2:
                                code, line = result
                                if code is None:
                                    if line.strip():
                                        await emit(f"[INSTALL] {line.strip()}")
                            else:
                                code, out, err = result
                                for line in (out + err).splitlines():
                                    if line.strip():
                                        await emit(f"[INSTALL] {line.strip()}")
                                if code != 0:
                                    await emit(f"[FAIL] composer install failed (exit {code}).")
                                    raise DeployError("composer install failed.")

                        for artisan_cmd in [
                            ['php', 'artisan', 'config:cache'],
                            ['php', 'artisan', 'route:cache'],
                            ['php', 'artisan', 'view:cache'],
                        ]:
                            async for result in self.run_exec_stream(artisan_cmd, cwd=deploy_path):
                                if len(result) == 2:
                                    code, line = result
                                    if code is None:
                                        if line.strip():
                                            await emit(f"[BUILD] {line.strip()}")
                                else:
                                    code, out, err = result
                                    for line in (out + err).splitlines():
                                        if line.strip():
                                            await emit(f"[BUILD] {line.strip()}")
                                    if code != 0:
                                        await emit(f"[FAIL] {' '.join(artisan_cmd)} failed (exit {code}).")
                                        raise DeployError("Artisan command failed.")

                    await emit("[REBUILD] Performing health check...")
                    fqdn = f"{deployment['subdomain']}.{_DOMAIN}"
                    health_ok = False
                    for attempt in range(5):
                        try:
                            async with aiohttp.ClientSession() as session:
                                async with session.get(f"https://{fqdn}", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                    if resp.status == 200:
                                        health_ok = True
                                        await emit(f"[HEALTH] Site responded with HTTP {resp.status} (attempt {attempt+1}/5).")
                                        break
                                    else:
                                        await emit(f"[HEALTH] HTTP {resp.status} (attempt {attempt+1}/5).")
                        except Exception as e:
                            await emit(f"[HEALTH] Error: {e} (attempt {attempt+1}/5).")
                        if attempt < 4:
                            await asyncio.sleep(3)
                    if health_ok:
                        await emit("[HEALTH] Health check passed.")
                    else:
                        await emit("[HEALTH] Health check failed after rebuild.")

                    success = True
                    await emit("[REBUILD] Rebuild complete.")

        except DeployError:
            pass
        except Exception as e:
            self.logger.exception(f"Unexpected rebuild error [{run_id}]: {e}")
            await emit(f"[FATAL] Unexpected error: {e}")
        finally:
            full_log = '\n'.join(log_lines)
            if len(full_log) > _MAX_LOG_BYTES:
                full_log = full_log[:_MAX_LOG_BYTES] + '\n[LOG TRUNCATED]'
            if log_created:
                await update_deployment_log(
                    run_id, 'success' if success else 'failed', full_log
                )
            q = self._active_streams.get(run_id)
            if q:
                await q.put(None)
            await asyncio.sleep(_STREAM_TTL)
            self._active_streams.pop(run_id, None)

    async def _check_dev(self, ctx_or_interaction) -> bool:
        user_id = ctx_or_interaction.author.id if hasattr(ctx_or_interaction, 'author') else ctx_or_interaction.user.id
        if user_id != _DEV_ID:
            if hasattr(ctx_or_interaction, 'send'):
                await ctx_or_interaction.send("You are not authorized to use this command.", ephemeral=True)
            else:
                await ctx_or_interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
            return False
        return True

    @commands.slash_command(name="deploy", description="Deploy a new project")
    async def slash_deploy(self, ctx: discord.ApplicationContext, project_uuid: str, subdomain: str, github_pat: str):
        if not await self._check_dev(ctx):
            return
        await ctx.respond("Processing deployment...", ephemeral=True)
        project_data = {"project_uuid": project_uuid, "name": project_uuid, "git_url": "https://github.com/example/repo.git", "default_branch": "main"}
        run_id = self.queue_deploy(project_data, subdomain, github_pat, str(ctx.author.id))
        embed = discord.Embed(title="Deployment Started", description=f"Run ID: `{run_id}`\nSubdomain: `{subdomain}`", color=discord.Color.blue())
        await ctx.send_followup(embed=embed)

    @commands.slash_command(name="logs", description="Stream logs for a deployment run")
    async def slash_logs(self, ctx: discord.ApplicationContext, run_uuid: str):
        if not await self._check_dev(ctx):
            return
        q = self.get_stream(run_uuid)
        if not q:
            await ctx.respond("No active stream for that run ID.", ephemeral=True)
            return
        await ctx.respond(f"Streaming logs for `{run_uuid}`...", ephemeral=True)
        while True:
            try:
                line = await asyncio.wait_for(q.get(), timeout=30.0)
                if line is None:
                    await ctx.send_followup("Log stream ended.", ephemeral=True)
                    break
                await ctx.send_followup(f"```\n{line}\n```", ephemeral=True)
            except asyncio.TimeoutError:
                await ctx.send_followup("Log stream timed out.", ephemeral=True)
                break

    @commands.slash_command(name="delete", description="Delete a deployment")
    async def slash_delete(self, ctx: discord.ApplicationContext, deployment_uuid: str):
        if not await self._check_dev(ctx):
            return
        await ctx.respond(f"Deleting deployment `{deployment_uuid}`...", ephemeral=True)
        ok, msg = await self.delete_deployment(deployment_uuid)
        if ok:
            await ctx.send_followup(f"{msg}", ephemeral=True)
        else:
            await ctx.send_followup(f"{msg}", ephemeral=True)

    @commands.slash_command(name="rebuild", description="Rebuild an existing deployment")
    async def slash_rebuild(self, ctx: discord.ApplicationContext, deployment_uuid: str):
        if not await self._check_dev(ctx):
            return
        await ctx.respond(f"Rebuilding deployment `{deployment_uuid}`...", ephemeral=True)
        run_id = self.queue_rebuild(deployment_uuid, str(ctx.author.id))
        await ctx.send_followup(f"Rebuild started. Run ID: `{run_id}`\nUse `/logs {run_id}` to watch.", ephemeral=True)


def setup(bot):
    bot.add_cog(DeploymentCog(bot))