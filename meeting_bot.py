#!/usr/bin/env python3
"""Discord slash-command bot for scheduled voice-channel meeting recordings.

Required .env values:
  DISCORD_BOT_TOKEN=...
  DISCORD_WEBHOOK=...  # existing transcript/audio delivery webhook

Times accepted by /meeting are IST ISO-8601, e.g. 2026-09-15 14:30.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import dotenv_values, load_dotenv

from audio_mixer import AudioMixError, mix_wav_files
from meeting_scheduler import (
    MeetingSchedule,
    MeetingStateStore,
    ScheduleValidationError,
)
from watcher import process_audio_once

BASE_DIR = Path(__file__).parent
SPEECH_DIR = BASE_DIR / "speech"
TEMP_RECORDING_DIR = BASE_DIR / "recording-tmp"
STATE_FILE = BASE_DIR / "data" / "meeting-state.json"
ALLOWED_GUILD_ID = 1480508770499956907
ALLOWED_VOICE_CHANNEL_ID = 1548204696840314890
OUTPUT_CHANNEL_ID = 1547528564713193473
UTC = timezone.utc
LOG = logging.getLogger("meeting-bot")

ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)
REPO_ENV = dotenv_values(ENV_FILE)
# The managed host exports Frontend Warriors' token. Use the dedicated
# recorder credentials from this repository instead of inherited values.
BOT_TOKEN = REPO_ENV.get("DISCORD_BOT_TOKEN")
WEBHOOK_URL = REPO_ENV.get("DISCORD_WEBHOOK")


class MeetingBot(discord.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(intents=intents)
        self.state = MeetingStateStore(STATE_FILE)
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.locks: dict[int, asyncio.Lock] = {}

    def lock_for(self, guild_id: int) -> asyncio.Lock:
        return self.locks.setdefault(guild_id, asyncio.Lock())

    def voice_client_for(self, guild_id: int) -> discord.VoiceClient | None:
        return next((client for client in self.voice_clients if client.guild.id == guild_id), None)

    @staticmethod
    def is_allowed_schedule(schedule: MeetingSchedule) -> bool:
        return (
            schedule.guild_id == ALLOWED_GUILD_ID
            and schedule.voice_channel_id == ALLOWED_VOICE_CHANNEL_ID
            and schedule.notification_channel_id == OUTPUT_CHANNEL_ID
        )

    async def on_ready(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()
            await self.recover_schedules()
        LOG.info("Connected as %s", self.user)

    async def recover_schedules(self) -> None:
        """Restore future jobs and safely resume a meeting interrupted by restart."""
        now = datetime.now(UTC)
        for schedule in self.state.schedules():
            if not self.is_allowed_schedule(schedule) or schedule.end_at <= now:
                self.state.remove_schedule(schedule.guild_id)
                self.state.clear_active(schedule.guild_id)
                continue
            self.install_schedule(schedule)
            if schedule.start_at <= now < schedule.end_at:
                # A restarted process cannot still be recording. Rejoin and continue once.
                asyncio.create_task(self.start_recording(schedule.guild_id, recovery=True))

    def install_schedule(self, schedule: MeetingSchedule) -> None:
        """replace_existing ensures /meeting never leaves duplicate scheduled jobs."""
        self.scheduler.add_job(
            self.start_recording,
            trigger="date",
            run_date=schedule.start_at,
            args=[schedule.guild_id],
            id=f"meeting:{schedule.guild_id}:start",
            replace_existing=True,
            misfire_grace_time=60,
        )
        self.scheduler.add_job(
            self.stop_recording,
            trigger="date",
            run_date=schedule.end_at,
            args=[schedule.guild_id, "scheduled"],
            id=f"meeting:{schedule.guild_id}:stop",
            replace_existing=True,
            misfire_grace_time=300,
        )

    def remove_jobs(self, guild_id: int) -> None:
        for suffix in ("start", "stop"):
            try:
                self.scheduler.remove_job(f"meeting:{guild_id}:{suffix}")
            except JobLookupError:
                continue

    async def notify(self, schedule: MeetingSchedule, content: str) -> None:
        channel = self.get_channel(schedule.notification_channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(schedule.notification_channel_id)
            except discord.HTTPException:
                LOG.warning("Could not resolve notification channel for guild %s", schedule.guild_id)
                return
        await channel.send(content)

    async def start_recording(self, guild_id: int, recovery: bool = False) -> bool:
        async with self.lock_for(guild_id):
            schedule = self.state.get_schedule(guild_id)
            if not schedule or not self.is_allowed_schedule(schedule):
                return False
            now = datetime.now(UTC)
            if now >= schedule.end_at:
                return False
            active = self.state.get_active(guild_id)
            if active and active.get("status") in {"recording", "stopping"}:
                return False

            channel = self.get_channel(schedule.voice_channel_id)
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                await self.notify(schedule, "⚠️ Meeting recording failed: configured voice channel no longer exists.")
                return False

            try:
                voice_client = self.voice_client_for(guild_id)
                if voice_client and voice_client.recording:
                    await self.notify(schedule, "⚠️ Meeting recording was not started: the bot is already recording.")
                    return False
                if voice_client is None:
                    voice_client = await channel.connect()
                elif voice_client.channel != channel:
                    await voice_client.move_to(channel)

                self.state.set_active(
                    guild_id,
                    {
                        "status": "recording",
                        "started_at": now.isoformat(),
                        "source": "recovery" if recovery else "scheduled",
                    },
                )
                voice_client.start_recording(
                    discord.sinks.WaveSink(),
                    self.recording_finished,
                    channel,
                    sync_start=True,
                )
                suffix = " (recovered after restart)" if recovery else ""
                await self.notify(schedule, f"🔴 Meeting recording started{suffix}.")
                return True
            except Exception as exc:
                LOG.exception("Unable to start recording for guild %s", guild_id)
                self.state.clear_active(guild_id)
                await self.notify(schedule, f"⚠️ Meeting recording failed to start: `{type(exc).__name__}`")
                return False

    async def stop_recording(self, guild_id: int, source: str = "manual") -> bool:
        async with self.lock_for(guild_id):
            schedule = self.state.get_schedule(guild_id)
            active = self.state.get_active(guild_id)
            if not schedule or not active or active.get("status") != "recording":
                return False
            voice_client = self.voice_client_for(guild_id)
            if not voice_client or not voice_client.recording:
                self.state.clear_active(guild_id)
                return False

            active["status"] = "stopping"
            active["stop_source"] = source
            self.state.set_active(guild_id, active)
            try:
                voice_client.stop_recording()
                return True
            except Exception as exc:
                LOG.exception("Unable to stop recording for guild %s", guild_id)
                active["status"] = "recording"
                self.state.set_active(guild_id, active)
                await self.notify(schedule, f"⚠️ Meeting recording failed to stop: `{type(exc).__name__}`")
                return False

    async def recording_finished(self, sink: Any, channel: discord.abc.GuildChannel, *_: Any) -> None:
        """Save one mixed meeting WAV, then invoke the normal one-shot processor."""
        guild_id = channel.guild.id
        schedule = self.state.get_schedule(guild_id)
        active = self.state.get_active(guild_id) or {}
        self.state.clear_active(guild_id)
        if not schedule or not self.is_allowed_schedule(schedule):
            return

        try:
            SPEECH_DIR.mkdir(exist_ok=True)
            TEMP_RECORDING_DIR.mkdir(exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            participant_files: list[Path] = []
            for user_id, audio in sink.audio_data.items():
                target = TEMP_RECORDING_DIR / f"meeting-{guild_id}-{timestamp}-user-{user_id}.wav"
                audio.file.seek(0)
                target.write_bytes(audio.file.read())
                participant_files.append(target)

            if not participant_files:
                await self.notify(schedule, "⚠️ Meeting recording stopped, but no participant audio was captured.")
                return

            meeting_file = SPEECH_DIR / f"meeting-{guild_id}-{timestamp}.wav"
            await asyncio.to_thread(mix_wav_files, participant_files, meeting_file)
            for participant_file in participant_files:
                participant_file.unlink(missing_ok=True)

            source = active.get("stop_source", "scheduled")
            self.remove_jobs(guild_id)
            self.state.remove_schedule(guild_id)
            await self.notify(
                schedule,
                f"⏹️ Meeting recording stopped ({source}). Saved `{meeting_file.name}`; transcription has started.",
            )
            asyncio.create_task(self.process_recording(schedule, meeting_file))
        except (AudioMixError, OSError, ValueError) as exc:
            LOG.exception("Unable to finalize recording for guild %s", guild_id)
            await self.notify(schedule, f"⚠️ Meeting recording failed while saving: `{type(exc).__name__}`")
        finally:
            voice_client = self.voice_client_for(guild_id)
            if voice_client and voice_client.is_connected():
                await voice_client.disconnect(force=True)

    async def process_recording(self, schedule: MeetingSchedule, meeting_file: Path) -> None:
        """Run the same one-shot processor used by manual meeting-summary uploads."""
        try:
            processed = await asyncio.to_thread(process_audio_once, meeting_file)
            if not processed:
                await self.notify(schedule, f"⚠️ Processing skipped for `{meeting_file.name}`.")
        except Exception as exc:
            LOG.exception("Unable to process meeting recording %s", meeting_file)
            await self.notify(schedule, f"⚠️ Meeting transcription failed: `{type(exc).__name__}`")


bot = MeetingBot()


@bot.slash_command(name="meeting", description="Create or replace this server's scheduled meeting recording (IST).")
async def meeting(
    ctx: discord.ApplicationContext,
    start_time: str,
    end_time: str,
) -> None:
    if ctx.guild is None or ctx.guild.id != ALLOWED_GUILD_ID or ctx.channel_id != OUTPUT_CHANNEL_ID:
        await ctx.respond("Meeting recording can only be configured in #meeting-summary.", ephemeral=True)
        return
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.respond("Join the meeting-bot voice channel, then run `/meeting` again.", ephemeral=True)
        return
    if ctx.author.voice.channel.id != ALLOWED_VOICE_CHANNEL_ID:
        await ctx.respond("Recording is restricted to the meeting-bot voice channel.", ephemeral=True)
        return

    try:
        schedule = MeetingSchedule.create(
            guild_id=ctx.guild.id,
            voice_channel_id=ctx.author.voice.channel.id,
            notification_channel_id=ctx.channel_id,
            start_time=start_time,
            end_time=end_time,
        )
    except ScheduleValidationError as exc:
        await ctx.respond(f"Invalid meeting schedule: {exc}", ephemeral=True)
        return

    bot.state.replace_schedule(schedule)
    bot.remove_jobs(schedule.guild_id)
    bot.install_schedule(schedule)
    await ctx.respond(
        "✅ Meeting schedule saved (replaced any previous one).\n"
        f"Voice channel: {ctx.author.voice.channel.mention}\n"
        f"Start: <t:{int(schedule.start_at.timestamp())}:F>\n"
        f"End: <t:{int(schedule.end_at.timestamp())}:F>"
    )


@bot.slash_command(name="meeting_stop", description="Stop the active meeting recording now.")
async def meeting_stop(ctx: discord.ApplicationContext) -> None:
    if ctx.guild is None or ctx.guild.id != ALLOWED_GUILD_ID or ctx.channel_id != OUTPUT_CHANNEL_ID:
        await ctx.respond("Meeting recording can only be controlled in #meeting-summary.", ephemeral=True)
        return
    if await bot.stop_recording(ctx.guild.id, "manual"):
        await ctx.respond("⏹️ Stopping the active recording. Audio will be saved and transcribed shortly.")
    else:
        await ctx.respond("There is no active meeting recording to stop.", ephemeral=True)


@bot.slash_command(name="meeting_status", description="Show this server's meeting schedule and recording state.")
async def meeting_status(ctx: discord.ApplicationContext) -> None:
    if ctx.guild is None or ctx.guild.id != ALLOWED_GUILD_ID or ctx.channel_id != OUTPUT_CHANNEL_ID:
        await ctx.respond("Meeting recording status is only available in #meeting-summary.", ephemeral=True)
        return
    schedule = bot.state.get_schedule(ctx.guild.id)
    active = bot.state.get_active(ctx.guild.id)
    if not schedule:
        await ctx.respond("No meeting recording is scheduled for this server.", ephemeral=True)
        return
    state = active.get("status", "idle") if active else "idle"
    await ctx.respond(
        f"**Meeting status:** `{state}`\n"
        f"Voice channel: <#{schedule.voice_channel_id}>\n"
        f"Start: <t:{int(schedule.start_at.timestamp())}:F>\n"
        f"End: <t:{int(schedule.end_at.timestamp())}:F>"
    )


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is required in .env")
    if not WEBHOOK_URL:
        raise SystemExit("DISCORD_WEBHOOK is required in .env")
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot.run(BOT_TOKEN)
