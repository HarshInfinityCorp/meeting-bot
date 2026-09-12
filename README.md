# Meeting Bot

A Discord voice-channel meeting recorder that saves recordings in `speech/`, uploads the recording to the configured Discord webhook, and uses the existing Sarvam/Composer transcription pipeline to post the transcript and summary.

## Setup

1. Install dependencies. The `py-cord[voice]` extra supplies the required PyNaCl voice support:
   ```bash
   python3 -m pip install -r requirements.txt
   ```
2. Add these values to `.env` (do not commit it):
   ```dotenv
   DISCORD_BOT_TOKEN=your_discord_bot_token
   DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
   SARVAM_API_KEY=...
   NBMG_API_KEY=...
   ```
3. In the Discord Developer Portal, invite the bot with the `bot` and `applications.commands` scopes. It needs **View Channel**, **Connect**, **Speak**, **Use Voice Activity**, **Send Messages**, and **Attach Files** in the relevant channels.
4. Start it:
   ```bash
   python3 meeting_bot.py
   ```

## Process New Audio on Demand

After a recording has been added to `speech/`, run the one-time processor:

```bash
python3 watcher.py --once
```

It uses `processed.json` to skip audio that has already completed. This is a one-time command, not a background polling service.

## Discord commands

All times are ISO-8601 UTC. A timezone-free time is interpreted as UTC.

- `/meeting start_time:2026-09-15 14:30 end_time:2026-09-15 15:30`
  - Configure the meeting using the invoking user's current voice channel. Replaces that server's previous schedule and scheduled jobs.
- `/meeting-stop`
  - Stop an active recording early. It saves the captured participant WAV files, posts them through the webhook, and starts transcription.
- `/meeting-status`
  - Show the configured voice channel, schedule, and `idle` / `recording` / `stopping` state.

Schedules and active state are stored atomically in `data/meeting-state.json`. On restart, future schedules are restored; if the restart happens within an active meeting window, the bot reconnects and resumes recording. Every transition is serialized per server to avoid schedule/manual-stop races.

## Recording behavior

Pycord produces a WAV file per participant. Those files are saved as `speech/meeting-<guild>-<timestamp>-user-<id>.wav`, attached to the configured webhook, and individually passed to `transcribe.py`. This preserves speaker audio without lossy mixing and lets the existing diarized transcription pipeline post its normal summary and transcript.
