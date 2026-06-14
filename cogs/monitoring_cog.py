import discord
from discord.ext import commands, tasks
import psutil
import os
import logging
import aiohttp
from datetime import datetime, timezone
from database.db import (
    log_system_resources, execute_query,
    get_all_managed_services, get_active_deployments,
)
from utils.domains import fqdn_of

_DOMAIN = os.getenv('DEPLOY_DOMAIN', 'arvo.team')
_DEV_ID = int(os.getenv('DEV_ID', '0'))

class MonitoringCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Edge-triggered resource alert thresholds (percent). Alerts fire when a metric
        # crosses the threshold and clear once it drops a margin below it (hysteresis),
        # so a value flapping around the line doesn't spam the channel.
        self._cpu_threshold  = float(os.getenv('ALERT_CPU_PCT', '90'))
        self._ram_threshold  = float(os.getenv('ALERT_RAM_PCT', '90'))
        self._disk_threshold = float(os.getenv('ALERT_DISK_PCT', '85'))
        self._alert_state = {'cpu': False, 'ram': False, 'disk': False}
        # Health watchdog over managed services + active deployments. Alert-only by default;
        # set SELF_HEAL_ENABLED=true to allow auto-restart / cert renewal (cooldown-guarded).
        self._watch_state = {}
        self._watch_fail = {}
        self._heal_attempts = {}
        self._heal_enabled = os.getenv('SELF_HEAL_ENABLED', 'false').lower() in ('1', 'true', 'yes')
        # Consecutive failed ticks before a target is declared down (debounce against blips).
        self._watch_fail_threshold = int(os.getenv('WATCHDOG_FAIL_THRESHOLD', '2'))
        # Watchdog ALERTING is OFF by default: a reboot leaves every service momentarily down,
        # and we don't want one "Service down" alert per service all at once. Turn it on once
        # the fleet is stable (POST /api/watchdog or /watchdog). WATCHDOG_ALERTS_ENABLED sets
        # the initial state; it's in-memory, so a bot/server restart returns to that default.
        self._alerts_enabled = os.getenv('WATCHDOG_ALERTS_ENABLED', 'false').lower() in ('1', 'true', 'yes')
        # Even when enabled, suppress alerts for the first GRACE seconds after the watchdog
        # starts, so a reboot's transient downtime never storms before things finish booting.
        self._watch_grace = float(os.getenv('WATCHDOG_GRACE_SECONDS', '300'))
        self._watch_started_at = None
        self.monitor_system.start()
        self.cleanup_old_logs.start()
        self.watchdog.start()

    async def _check_threshold(self, key, value, threshold, label):
        output = self.bot.get_cog('OutputCog')
        breached = value >= threshold
        was = self._alert_state.get(key, False)
        if breached and not was:
            self._alert_state[key] = True
            if output:
                try:
                    await output.alert(
                        'warning', f"High {label}",
                        f"{label} at {value:.0f}% (threshold {threshold:.0f}%).",
                        source='monitor', target=label, critical=True,
                    )
                except Exception:
                    pass
        elif was and value < (threshold - 5):
            self._alert_state[key] = False
            if output:
                try:
                    await output.alert(
                        'success', f"{label} recovered", f"{label} back to {value:.0f}%.",
                        source='monitor', target=label, critical=False,
                    )
                except Exception:
                    pass

    def cog_unload(self):
        self.monitor_system.cancel()
        self.cleanup_old_logs.cancel()
        self.watchdog.cancel()

    @tasks.loop(seconds=10)
    async def monitor_system(self):
        try:
            cpu = psutil.cpu_percent(interval=None)
            
            mem = psutil.virtual_memory()
            ram_percent = mem.percent
            ram_remaining = mem.available
            ram_total = mem.total

            disk_info = psutil.disk_usage('/')
            disk_percent = disk_info.percent
            disk_remaining = disk_info.free
            disk_total = disk_info.total

            st = os.statvfs('/')
            inodes_total = st.f_files
            inodes_free = st.f_ffree
            inodes_used = inodes_total - inodes_free

            connections = len(psutil.net_connections())

            await log_system_resources(
                cpu,
                ram_percent,
                ram_remaining,
                ram_total,
                disk_percent,
                disk_remaining,
                disk_total,
                inodes_used,
                inodes_total,
                connections
            )

            # Edge-triggered alerts on sustained resource pressure.
            await self._check_threshold('cpu', cpu, self._cpu_threshold, 'CPU')
            await self._check_threshold('ram', ram_percent, self._ram_threshold, 'RAM')
            await self._check_threshold('disk', disk_percent, self._disk_threshold, 'disk')
        except Exception as e:
            logging.error(f"Monitoring error: {e}")

    @tasks.loop(hours=24)
    async def cleanup_old_logs(self):
        try:
            await execute_query(
                "DELETE FROM system_stats WHERE timestamp < NOW() - INTERVAL 30 DAY"
            )
            await execute_query(
                "DELETE FROM alerts WHERE acknowledged_at IS NOT NULL "
                "AND created_at < NOW() - INTERVAL 30 DAY"
            )
            logging.info("Cleaned up old system resources logs.")
        except Exception as e:
            logging.error(f"Cleanup error: {e}")

    # ------------------------------
    # Health watchdog (managed services + active deployments)
    # ------------------------------
    def _alerts_active(self) -> bool:
        """Whether the watchdog may EMIT alerts now: toggled on AND past the startup grace."""
        if not self._alerts_enabled:
            return False
        if self._watch_started_at is not None:
            age = (datetime.now(timezone.utc) - self._watch_started_at).total_seconds()
            if age < self._watch_grace:
                return False
        return True

    def watchdog_status(self) -> dict:
        remaining = 0.0
        if self._alerts_enabled and self._watch_started_at is not None:
            remaining = max(0.0, self._watch_grace
                            - (datetime.now(timezone.utc) - self._watch_started_at).total_seconds())
        return {
            'alerts_enabled': self._alerts_enabled,
            'alerting_now': self._alerts_active(),       # false while disabled OR in startup grace
            'grace_seconds': self._watch_grace,
            'grace_remaining_seconds': int(remaining),
            'self_heal_enabled': self._heal_enabled,
            'fail_threshold': self._watch_fail_threshold,
        }

    def set_watchdog(self, alerts_enabled=None, self_heal_enabled=None) -> dict:
        """Runtime toggle for watchdog alerting (and, optionally, self-heal)."""
        if alerts_enabled is not None:
            self._alerts_enabled = bool(alerts_enabled)
        if self_heal_enabled is not None:
            self._heal_enabled = bool(self_heal_enabled)
        return self.watchdog_status()

    @tasks.loop(seconds=60)
    async def watchdog(self):
        try:
            await self._run_watchdog()
        except Exception as e:
            logging.error(f"Watchdog error: {e}")

    async def _http_ok(self, url):
        # For a watchdog, "responding" matters more than "exactly 200": 3xx/4xx mean the
        # server is up (redirects, auth gates). Only 5xx or no response counts as down.
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                    return r.status < 500, r.status
        except Exception:
            return False, None

    async def _collect_targets(self):
        targets = []
        for s in await get_all_managed_services(enabled_only=True):
            fqdn = s.get('fqdn')
            url = s.get('health_url') or (f"https://{fqdn}" if fqdn else None)
            targets.append({
                'key': f"svc:{s['service_uuid']}", 'label': s['name'], 'url': url, 'fqdn': fqdn,
                'pm2_name': s.get('pm2_name') if s.get('service_type') == 'pm2' else None,
                'deploy_path': s.get('deploy_path'), 'port': s.get('port'),
            })
        for d in await get_active_deployments():
            fqdn = fqdn_of(d)  # canonical hostname (custom domains have subdomain=None)
            targets.append({
                'key': f"dep:{d['deployment_uuid']}", 'label': fqdn,
                'url': f"https://{fqdn}", 'fqdn': fqdn,
                'pm2_name': (d.get('pm2_name') or d['deployment_uuid'][:12])
                            if d.get('tech_stack') == 'node' else None,
                'deploy_path': d.get('deploy_path'), 'port': d.get('assigned_port'),
            })
        return targets

    async def _run_watchdog(self):
        dep = self.bot.get_cog('DeploymentCog')
        output = self.bot.get_cog('OutputCog')
        if not dep:
            return
        # Stamp the start of the watchdog's life so the startup grace can be measured.
        if self._watch_started_at is None:
            self._watch_started_at = datetime.now(timezone.utc)

        targets = await self._collect_targets()
        if not targets:
            return

        # One pm2 jlist + one certbot snapshot per tick, shared across all targets.
        pm2_map = await dep._pm2_jlist_map()
        cert_map = await dep._all_certs_map()

        # Detection/debounce always runs; only EMITTING alerts is gated. So when alerting is
        # turned on later, anything genuinely still down alerts on the next tick (it was never
        # marked "alerted" while suppressed).
        alerts_on = self._alerts_active()

        for t in targets:
            problems = []
            if t['pm2_name']:
                proc = pm2_map.get(t['pm2_name'])
                status = (proc.get('pm2_env', {}) or {}).get('status') if proc else None
                if status != 'online':
                    problems.append(f"process {status or 'not found'}")
            if t['url']:
                ok, code = await self._http_ok(t['url'])
                if not ok:
                    problems.append(f"HTTP {code}")
            if t['fqdn']:
                days = cert_map.get(t['fqdn'])
                if days is not None and days < 14:
                    problems.append(f"cert expires in {days}d")

            key = t['key']
            now_down = bool(problems)
            # Debounce: require N consecutive failing ticks before declaring down.
            fails = self._watch_fail.get(key, 0) + 1 if now_down else 0
            self._watch_fail[key] = fails
            confirmed_down = fails >= self._watch_fail_threshold
            was_alerted = self._watch_state.get(key, False)

            if confirmed_down and not was_alerted:
                # Suppress while alerting is off or in startup grace: don't alert, don't heal,
                # and DON'T mark as alerted — so it surfaces once alerting is enabled.
                if alerts_on:
                    self._watch_state[key] = True
                    if output:
                        try:
                            await output.alert('error', f"Service down: {t['label']}", "; ".join(problems),
                                               source='watchdog', target=t['label'], critical=True)
                        except Exception:
                            pass
                    if self._heal_enabled:
                        await self._attempt_heal(t, problems, dep, output)
            elif not now_down and was_alerted:
                self._watch_state[key] = False
                self._heal_attempts.pop(key, None)
                if output and alerts_on:
                    try:
                        await output.alert('success', f"Service recovered: {t['label']}", "Back to healthy.",
                                           source='watchdog', target=t['label'], critical=False)
                    except Exception:
                        pass

    async def _attempt_heal(self, t, problems, dep, output):
        """Safe auto-remediation (off by default): restart a down process, renew an expiring cert."""
        key = t['key']
        attempts = self._heal_attempts.get(key, 0)
        if attempts >= 3:
            if output:
                try:
                    await output.alert(
                        'warning', f"Auto-heal gave up: {t['label']}",
                        f"Still unhealthy after {attempts} attempts; manual intervention needed.",
                        source='watchdog', target=t['label'], critical=True,
                    )
                except Exception:
                    pass
            return
        self._heal_attempts[key] = attempts + 1
        if t['pm2_name'] and any('process' in p for p in problems):
            # Pass path + port so a process pm2 lost is recreated on its correct port.
            await dep.control_process(
                t['pm2_name'], 'restart',
                deploy_path=t.get('deploy_path'), port=t.get('port'),
            )
        if t['fqdn'] and any('cert expires' in p for p in problems):
            await dep.renew_ssl(t['fqdn'])

    @monitor_system.before_loop
    @cleanup_old_logs.before_loop
    @watchdog.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()

    @commands.slash_command(name="watchdog", description="Toggle watchdog down-alerts on/off")
    async def slash_watchdog(self, ctx: discord.ApplicationContext, enabled: bool):
        if ctx.author.id != _DEV_ID:
            await ctx.respond("You are not authorized to use this command.", ephemeral=True)
            return
        status = self.set_watchdog(alerts_enabled=enabled)
        note = ""
        if status['alerts_enabled'] and status['grace_remaining_seconds'] > 0:
            note = f" (startup grace active — alerts begin in ~{status['grace_remaining_seconds']}s)"
        await ctx.respond(
            f"Watchdog alerting is now **{'ON' if status['alerts_enabled'] else 'OFF'}**{note}.",
            ephemeral=True,
        )

def setup(bot):
    bot.add_cog(MonitoringCog(bot))