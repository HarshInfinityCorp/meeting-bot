#!/usr/bin/env python3
"""Discord slash-command bot for scheduled voice-channel meeting recordings.

Required .env values:
  DISCORD_BOT_TOKEN=...
  DISCORD_WEBHOOK=...  # existing transcript/audio delivery webhook

Times accepted by /meeting are UTC ISO-8601, e.g. 2026-09-15 14:30.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
import httpx
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from meeting_scheduler import (
    MeetingSchedule,
    MeetingStateStore,
    ScheduleValidationError,
)
from transcribe import transcribe

BASE_DIR = Path(__file__).parent
SPEECH_DIR = BASE_DIR / "speech"
STATE_FILE = BASE_DIR / "data" / "meeting-state.json"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
UTC = timezone.utc
LOG = logging.getLogger("meeting-bot")

load_dotenv(BASE_DIR / ".env")
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")


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

    async def on_ready(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()
            await self.recover_schedules()
        LOG.info("Connected as %s", self.user)

    async def recover_schedules(self) -> None:
        """Restore future jobs and safely resume a meeting interrupted by restart."""
        now = datetime.now(UTC)
        for schedule in self.state.schedules():
            if schedule.end_at <= now:
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
            if not schedule:
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
                voice_client.start_recording(discord.sinks.WaveSink(), self.recording_finished, channel)
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
        """Persist Pycord's per-speaker WAV streams, deliver audio, then transcribe."""
        guild_id = channel.guild.id
        schedule = self.state.get_schedule(guild_id)
        active = self.state.get_active(guild_id) or {}
        self.state.clear_active(guild_id)
        if not schedule:
            return

        try:
            SPEECH_DIR.mkdir(exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            recordings: list[Path] = []
            for user_id, audio in sink.audio_data.items():
                # Pycord exposes a BytesIO WAV file for every participant.
                target = SPEECH_DIR / f"meeting-{guild_id}-{timestamp}-user-{user_id}.wav"
                audio.file.seek(0)
                target.write_bytes(audio.file.read())
                recordings.append(target)

            if not recordings:
                await self.notify(schedule, "⚠️ Meeting recording stopped, but no participant audio was captured.")
                return

            source = active.get("stop_source", "scheduled")
            self.remove_jobs(guild_id)
            self.state.remove_schedule(guild_id)
            await self.notify(
                schedule,
                f"⏹️ Meeting recording stopped ({source}). Saved {len(recordings)} audio file(s); transcription has started.",
            )
            await self.send_audio_webhook(recordings, guild_id)
            for recording in recordings:
                asyncio.create_task(asyncio.to_thread(transcribe, str(recording)))
        except Exception as exc:
            LOG.exception("Unable to finalize recording for guild %s", guild_id)
            await self.notify(schedule, f"⚠️ Meeting recording failed while saving: `{type(exc).__name__}`")
        finally:
            voice_client = self.voice_client_for(guild_id)
            if voice_client and voice_client.is_connected():
                await voice_client.disconnect(force=True)

    async def send_audio_webhook(self, recordings: list[Path], guild_id: int) -> None:
        """Attach the saved audio to the existing Discord webhook before transcription."""
        if not WEBHOOK_URL:
            raise RuntimeError("DISCORD_WEBHOOK is not configured")

        # Discord permits up to 10 attachments in one webhook request.
        for offset in range(0, len(recordings), 10):
            batch = recordings[offset : offset + 10]
            files = [
                (f"files[{index}]", (recording.name, recording.read_bytes(), "audio/wav"))
                for index, recording in enumerate(batch)
            ]
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    WEBHOOK_URL,
                    data={"content": f"🎙️ Meeting recording saved (guild {guild_id})."},
                    files=files,
                )
                response.raise_for_status()


bot = MeetingBot()


@bot.slash_command(name="meeting", description="Create or replace this server's scheduled meeting recording (UTC).")
async def meeting(
    ctx: discord.ApplicationContext,
    start_time: str,
    end_time: str,
) -> None:
    if ctx.guild is None or ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.respond("Join the voice channel to record, then run `/meeting` again.", ephemeral=True)
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
    if ctx.guild is None:
        await ctx.respond("This command can only be used in a server.", ephemeral=True)
        return
    if await bot.stop_recording(ctx.guild.id, "manual"):
        await ctx.respond("⏹️ Stopping the active recording. Audio will be saved and transcribed shortly.")
    else:
        await ctx.respond("There is no active meeting recording to stop.", ephemeral=True)


@bot.slash_command(name="meeting_status", description="Show this server's meeting schedule and recording state.")
async def meeting_status(ctx: discord.ApplicationContext) -> None:
    if ctx.guild is None:
        await ctx.respond("This command can only be used in a server.", ephemeral=True)
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
