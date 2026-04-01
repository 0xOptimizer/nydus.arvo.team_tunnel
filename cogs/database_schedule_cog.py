import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import Option, SlashCommandGroup
from discord.ext import commands

from database.db import (
    create_database_schedule_records,
    get_due_schedules,
    get_schedules_for_database,
    get_schedule_by_uuid,
    set_schedule_next_run,
    set_schedule_enabled,
    set_schedule_interval,
    get_schedule_for_database_phase,
    get_enabled_backup_schedules_with_db_age,
    transition_schedule_phase,
    create_schedule_log,
    upsert_schedule_stats,
    get_schedule_stats,
    check_database_has_data,
    get_database_size_bytes,
    get_databases_without_schedules
)

logger = logging.getLogger('nydus.database_schedule')

_PHASE_ORDER = ['week1', 'week1_plus', 'month1_plus', 'month3_plus']

_PHASE_THRESHOLDS_DAYS = {
    'week1': 7,
    'week1_plus': 30,
    'month1_plus': 90,
}

_PHASE_INTERVALS = {
    'week1_plus': 86400,
    'month1_plus': 259200,
    'month3_plus': 604800,
}

_SIZE_THRESHOLD_BYTES = 50 * 1024 * 1024
_DISPATCHER_INTERVAL = 60
_SCHEDULE_BATCH_SIZE = 50

class _ProvisionConfirmView(discord.ui.View):
    def __init__(self, cog: 'DatabaseScheduleCog', databases: list):
        super().__init__(timeout=60)
        self._cog = cog
        self._databases = databases
        self._triggered = False

    @discord.ui.button(label="Provision All", style=discord.ButtonStyle.success)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        if self._triggered:
            await interaction.response.send_message("Already running.", ephemeral=True)
            return
        self._triggered = True
        self.disable_all_items()
        await interaction.response.edit_message(
            content=f"Provisioning schedules for {len(self._databases)} database(s)...",
            embed=None,
            view=self
        )

        succeeded = []
        failed = []
        for db in self._databases:
            try:
                await self._cog.initialise_schedule_records(
                    db['database_uuid'],
                    db['database_name'],
                    db['database_type']
                )
                succeeded.append(db['database_name'])
            except Exception as e:
                failed.append((db['database_name'], str(e)))
                logger.error(f"Failed to provision schedule for {db['database_name']}: {e}")

        lines = []
        for name in succeeded:
            lines.append(f"✓ `{name}`")
        for name, err in failed:
            lines.append(f"✗ `{name}` — {err}")

        embed = discord.Embed(
            title=f"Provisioning Complete ({len(succeeded)} succeeded, {len(failed)} failed)",
            description="\n".join(lines) or "Nothing to report.",
            color=discord.Color.green() if not failed else discord.Color.orange()
        )
        await interaction.edit_original_response(content=None, embed=embed, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.disable_all_items()
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)

    async def on_timeout(self):
        self.disable_all_items()

class DatabaseScheduleCog(commands.Cog):

    schedule_group = SlashCommandGroup("schedule", "Database schedule management")

    def __init__(self, bot):
        self.bot = bot
        self._dispatcher_task: Optional[asyncio.Task] = None

        max_backups = int(os.getenv('SCHEDULE_MAX_CONCURRENT_BACKUPS', 1))
        max_validity = int(os.getenv('SCHEDULE_MAX_CONCURRENT_VALIDITY', 5))
        self._backup_semaphore = asyncio.Semaphore(max_backups)
        self._validity_semaphore = asyncio.Semaphore(max_validity)

        self._dispatcher_task = asyncio.create_task(self._dispatcher_loop())

    def cog_unload(self):
        if self._dispatcher_task and not self._dispatcher_task.done():
            self._dispatcher_task.cancel()

    def _check_dev(self, ctx: discord.ApplicationContext) -> bool:
        dev_id = os.getenv('DEV_ID')
        return dev_id is not None and str(ctx.author.id) == dev_id

    async def initialise_schedule_records(self, database_uuid: str, database_name: str, database_type: str) -> None:
        success = await create_database_schedule_records(database_uuid, database_name, database_type)
        if success:
            await create_schedule_log(
                schedule_uuid=None,
                database_uuid=database_uuid,
                event_type='schedule_created',
                message=f"Initialised schedule records for {database_name}"
            )
        else:
            logger.error(f"Failed to initialise schedule records for {database_name} ({database_uuid})")

    async def _dispatcher_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            try:
                await self._process_due_schedules()
                await self._check_phase_transitions()
            except Exception as e:
                logger.error(f"Dispatcher loop error: {e}")
            await asyncio.sleep(_DISPATCHER_INTERVAL)

    async def _process_due_schedules(self) -> None:
        while True:
            due = await get_due_schedules(limit=_SCHEDULE_BATCH_SIZE)
            if not due:
                break

            for schedule in due:
                interval = schedule['interval_seconds']
                next_run = datetime.utcnow() + timedelta(seconds=interval)
                await set_schedule_next_run(schedule['schedule_uuid'], next_run)

                task_type = schedule['task_type']
                if task_type == 'db_validity_check':
                    asyncio.create_task(self._guarded_validity_check(schedule))
                elif task_type == 'db_backup':
                    asyncio.create_task(self._guarded_backup(schedule))
                else:
                    logger.warning(f"Unknown task_type '{task_type}' for schedule {schedule['schedule_uuid']}")

            if len(due) < _SCHEDULE_BATCH_SIZE:
                break

    async def _check_phase_transitions(self) -> None:
        schedules = await get_enabled_backup_schedules_with_db_age()
        if not schedules:
            return

        for s in schedules:
            phase = s.get('phase')
            if phase not in _PHASE_THRESHOLDS_DAYS:
                continue

            db_created_at = s.get('db_created_at')
            if not db_created_at:
                continue

            db_age_days = (datetime.utcnow() - db_created_at.replace(tzinfo=None)).days
            threshold = _PHASE_THRESHOLDS_DAYS[phase]
            if db_age_days < threshold:
                continue

            try:
                phase_idx = _PHASE_ORDER.index(phase)
            except ValueError:
                continue

            if phase_idx + 1 >= len(_PHASE_ORDER):
                continue

            next_phase = _PHASE_ORDER[phase_idx + 1]
            next_interval = _PHASE_INTERVALS.get(next_phase)
            if not next_interval:
                continue

            next_schedule = await get_schedule_for_database_phase(s['database_uuid'], next_phase)
            if not next_schedule:
                continue

            next_run = datetime.utcnow() + timedelta(seconds=next_interval)
            await transition_schedule_phase(
                old_uuid=s['schedule_uuid'],
                new_uuid=next_schedule['schedule_uuid'],
                interval_seconds=next_interval,
                next_run_at=next_run
            )
            await create_schedule_log(
                schedule_uuid=s['schedule_uuid'],
                database_uuid=s['database_uuid'],
                event_type='phase_transition',
                old_interval=s['interval_seconds'],
                new_interval=next_interval,
                message=f"Transitioned from {phase} to {next_phase} after {db_age_days} days"
            )
            logger.info(f"Phase transition: {s['database_uuid']} {phase} -> {next_phase}")

    async def _guarded_validity_check(self, schedule: dict) -> None:
        async with self._validity_semaphore:
            await self._handle_validity_check(schedule)

    async def _guarded_backup(self, schedule: dict) -> None:
        async with self._backup_semaphore:
            await self._handle_backup(schedule)

    async def _handle_validity_check(self, schedule: dict) -> None:
        schedule_uuid = schedule['schedule_uuid']
        try:
            config = json.loads(schedule['task_config'])
            database_uuid = config['database_uuid']
            database_name = config['database_name']

            data_row = await check_database_has_data(database_name)
            table_count = int(data_row['table_count']) if data_row else 0
            total_rows = int(data_row['total_rows']) if data_row else 0
            is_valid = table_count > 0 and total_rows > 0

            db_cog = self.bot.get_cog('DatabaseCog')
            if not db_cog:
                logger.error("DatabaseCog unavailable during validity check.")
                return

            db_record = await db_cog.fetch_database(database_uuid=database_uuid)
            if not db_record:
                logger.error(f"Database record not found for {database_uuid} during validity check.")
                return

            created_at = db_record['created_at'].replace(tzinfo=None)
            db_age_days = (datetime.utcnow() - created_at).days

            if is_valid:
                total_bytes = await get_database_size_bytes(database_name)
                interval = 21600 if total_bytes < _SIZE_THRESHOLD_BYTES else 86400
                next_run = datetime.utcnow() + timedelta(seconds=interval)

                week1 = await get_schedule_for_database_phase(database_uuid, 'week1')
                if week1:
                    await set_schedule_interval(week1['schedule_uuid'], interval)
                    await set_schedule_next_run(week1['schedule_uuid'], next_run)
                    await set_schedule_enabled(week1['schedule_uuid'], 1)

                await set_schedule_enabled(schedule_uuid, 0)
                await create_schedule_log(
                    schedule_uuid=schedule_uuid,
                    database_uuid=database_uuid,
                    event_type='validity_check_pass',
                    new_interval=interval,
                    message=f"Validated on day {db_age_days}. Size: {total_bytes} bytes. Week1 interval: {interval}s."
                )
                logger.info(f"Validity passed for {database_name}. Week1 enabled at {interval}s interval.")

            elif db_age_days >= 7:
                await set_schedule_enabled(schedule_uuid, 0)
                await create_schedule_log(
                    schedule_uuid=schedule_uuid,
                    database_uuid=database_uuid,
                    event_type='validity_expired',
                    message=f"No tables or rows found after {db_age_days} days. Backup schedule will not be activated."
                )
                logger.info(f"Validity expired for {database_name} after {db_age_days} days.")

            else:
                await create_schedule_log(
                    schedule_uuid=schedule_uuid,
                    database_uuid=database_uuid,
                    event_type='validity_check_fail',
                    message=f"No tables or rows found. Day {db_age_days} of 7."
                )

        except Exception as e:
            logger.error(f"Validity check error for schedule {schedule_uuid}: {e}")

    async def _handle_backup(self, schedule: dict) -> None:
        schedule_uuid = schedule['schedule_uuid']
        try:
            config = json.loads(schedule['task_config'])
            database_uuid = config['database_uuid']
            database_name = config['database_name']
            database_type = config['database_type']

            db_cog = self.bot.get_cog('DatabaseCog')
            if not db_cog:
                logger.error(f"DatabaseCog unavailable for scheduled backup of {database_name}.")
                return

            started_at = datetime.utcnow()
            success, result = await db_cog.perform_backup(database_uuid, database_type, database_name)
            duration_ms = int((datetime.utcnow() - started_at).total_seconds() * 1000)

            file_size = 0
            if success:
                backup = await db_cog.fetch_backup(result)
                file_size = int(backup.get('file_size_bytes', 0)) if backup else 0

            await upsert_schedule_stats(database_uuid, success, file_size, duration_ms)
            await create_schedule_log(
                schedule_uuid=schedule_uuid,
                database_uuid=database_uuid,
                event_type='backup_completed' if success else 'backup_failed',
                message=f"Backup {result} completed in {duration_ms}ms" if success else str(result)
            )

        except Exception as e:
            logger.error(f"Backup error for schedule {schedule_uuid}: {e}")

    @schedule_group.command(name="list", description="List schedules for a database")
    async def cmd_schedule_list(
        self,
        ctx: discord.ApplicationContext,
        database_uuid: Option(str, "Database UUID")
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        schedules = await get_schedules_for_database(database_uuid)
        if not schedules:
            await ctx.respond("No schedules found for this database.", ephemeral=True)
            return

        lines = []
        for s in schedules:
            status = "on" if s['enabled'] else "off"
            next_run = str(s['next_run_at']) if s['next_run_at'] else "N/A"
            lines.append(f"`{s['phase']}` [{status}] — every {s['interval_seconds']}s — next: {next_run}")

        embed = discord.Embed(
            title=f"Schedules for {database_uuid[:8]}... ({len(schedules)})",
            description="\n".join(lines),
            color=discord.Color.blurple()
        )
        await ctx.respond(embed=embed, ephemeral=True)

    @schedule_group.command(name="info", description="Show details and stats for a schedule")
    async def cmd_schedule_info(
        self,
        ctx: discord.ApplicationContext,
        schedule_uuid: Option(str, "Schedule UUID")
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        s = await get_schedule_by_uuid(schedule_uuid)
        if not s:
            await ctx.respond("Schedule not found.", ephemeral=True)
            return

        stats = await get_schedule_stats(s['database_uuid'])

        embed = discord.Embed(title="Schedule Info", color=discord.Color.blurple())
        embed.add_field(name="UUID", value=s['schedule_uuid'], inline=False)
        embed.add_field(name="Phase", value=s['phase'], inline=True)
        embed.add_field(name="Type", value=s['task_type'], inline=True)
        embed.add_field(name="Enabled", value="Yes" if s['enabled'] else "No", inline=True)
        embed.add_field(name="Interval", value=f"{s['interval_seconds']}s", inline=True)
        embed.add_field(name="Next Run", value=str(s['next_run_at'] or "N/A"), inline=True)

        if stats:
            total = stats.get('total_backups', 0)
            success = stats.get('successful_backups', 0)
            failed = stats.get('failed_backups', 0)
            avg_size = stats.get('average_backup_size_bytes', 0)
            avg_dur = stats.get('average_duration_ms', 0)
            last_ok = stats.get('last_successful_backup_at', 'N/A')
            embed.add_field(name="Total Backups", value=str(total), inline=True)
            embed.add_field(name="Success / Failed", value=f"{success} / {failed}", inline=True)
            embed.add_field(name="Avg Size", value=f"{avg_size / 1024 / 1024:.2f} MB" if avg_size else "N/A", inline=True)
            embed.add_field(name="Avg Duration", value=f"{avg_dur}ms" if avg_dur else "N/A", inline=True)
            embed.add_field(name="Last Success", value=str(last_ok), inline=True)

        await ctx.respond(embed=embed, ephemeral=True)

    @schedule_group.command(name="toggle", description="Enable or disable a schedule by UUID")
    async def cmd_schedule_toggle(
        self,
        ctx: discord.ApplicationContext,
        schedule_uuid: Option(str, "Schedule UUID")
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        s = await get_schedule_by_uuid(schedule_uuid)
        if not s:
            await ctx.respond("Schedule not found.", ephemeral=True)
            return

        new_state = 0 if s['enabled'] else 1
        await set_schedule_enabled(schedule_uuid, new_state)

        if new_state == 1 and not s['next_run_at']:
            next_run = datetime.utcnow() + timedelta(seconds=s['interval_seconds'])
            await set_schedule_next_run(schedule_uuid, next_run)

        state_str = "enabled" if new_state else "disabled"
        await create_schedule_log(
            schedule_uuid=schedule_uuid,
            database_uuid=s['database_uuid'],
            event_type='manual_toggle',
            message=f"Schedule manually {state_str} by developer"
        )
        await ctx.respond(f"Schedule `{schedule_uuid[:8]}...` {state_str}.", ephemeral=True)

    @schedule_group.command(name="run", description="Force-run a schedule immediately")
    async def cmd_schedule_run(
        self,
        ctx: discord.ApplicationContext,
        schedule_uuid: Option(str, "Schedule UUID")
    ):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        s = await get_schedule_by_uuid(schedule_uuid)
        if not s:
            await ctx.respond("Schedule not found.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)

        task_type = s['task_type']
        if task_type == 'db_validity_check':
            asyncio.create_task(self._guarded_validity_check(s))
        elif task_type == 'db_backup':
            asyncio.create_task(self._guarded_backup(s))
        else:
            await ctx.followup.send(f"Unknown task type '{task_type}', cannot force-run.", ephemeral=True)
            return

        await ctx.followup.send(
            f"Schedule `{schedule_uuid[:8]}...` queued for immediate run. It will respect the semaphore.",
            ephemeral=True
        )

    @schedule_group.command(name="audit", description="Find databases missing schedule records and optionally provision them")
    async def cmd_schedule_audit(self, ctx: discord.ApplicationContext):
        if not self._check_dev(ctx):
            await ctx.respond("Unauthorized.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        databases = await get_databases_without_schedules()

        if not databases:
            await ctx.followup.send("All databases have schedule records. Nothing to provision.", ephemeral=True)
            return

        lines = []
        for db in databases[:25]:
            lines.append(f"`{db['database_name']}` — {db['database_type']} — `{db['database_uuid'][:8]}...`")

        overflow = len(databases) - 25
        description = "\n".join(lines)
        if overflow > 0:
            description += f"\n... and {overflow} more"

        embed = discord.Embed(
            title=f"{len(databases)} database(s) missing schedules",
            description=description,
            color=discord.Color.orange()
        )
        embed.set_footer(text="Provisioning will create 5 schedule records per database. Validity checks will be enabled immediately.")

        view = _ProvisionConfirmView(self, databases)
        await ctx.followup.send(embed=embed, view=view, ephemeral=True)


def setup(bot):
    bot.add_cog(DatabaseScheduleCog(bot))