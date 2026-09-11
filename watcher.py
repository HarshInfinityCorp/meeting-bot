#!/usr/bin/env python3
"""
Meeting Bot Watcher
Monitors the speech/ folder for new audio files.
When a new file is detected → transcribe → summarize → send to Discord.
Already-processed files are tracked in processed.json and skipped.

Usage:
    python watcher.py                  # Watch with default 10s interval
    python watcher.py --interval 5     # Check every 5 seconds
    python watcher.py --once           # Process new files once and exit
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

# Load .env from script directory
load_dotenv(Path(__file__).parent / ".env")

SPEECH_DIR = Path(__file__).parent / "speech"
PROCESSED_FILE = Path(__file__).parent / "processed.json"
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm", ".aac", ".wma"}


def load_processed() -> dict:
    """Load processed.json — auto-create if it doesn't exist."""
    if PROCESSED_FILE.exists():
        try:
            with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_processed(data: dict):
    """Save processed.json with updated tracking data."""
    with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_new_files(processed: dict) -> list:
    """Scan speech/ folder and return list of unprocessed audio files."""
    if not SPEECH_DIR.exists():
        SPEECH_DIR.mkdir(parents=True, exist_ok=True)
        return []

    new_files = []
    for file_path in sorted(SPEECH_DIR.iterdir()):
        if file_path.is_file() and file_path.suffix.lower() in AUDIO_EXTENSIONS:
            if file_path.name not in processed:
                # Check file is not still being written (size stable for 2 seconds)
                try:
                    size1 = file_path.stat().st_size
                    time.sleep(2)
                    size2 = file_path.stat().st_size
                    if size1 == size2 and size1 > 0:
                        new_files.append(file_path)
                    else:
                        print(f"   Skipping {file_path.name} (still being written...)")
                except OSError:
                    continue
    return new_files


def process_file(audio_path: Path, processed: dict) -> bool:
    """
    Process a single audio file through the transcription pipeline.
    Returns True if successful, False otherwise.
    """
    print(f"\n{'=' * 60}")
    print(f"NEW FILE DETECTED: {audio_path.name}")
    print(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'=' * 60}\n")

    # Mark as processing
    processed[audio_path.name] = {
        "status": "processing",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "file_size_mb": round(audio_path.stat().st_size / 1024 / 1024, 2),
    }
    save_processed(processed)

    try:
        # Import transcribe function
        from transcribe import transcribe

        # Run full pipeline: transcribe → summarize → discord
        transcribe(str(audio_path))

        # Mark as done
        processed[audio_path.name]["status"] = "done"
        processed[audio_path.name]["completed_at"] = datetime.now(timezone.utc).isoformat()
        save_processed(processed)

        print(f"\n{'=' * 60}")
        print(f"COMPLETED: {audio_path.name}")
        print(f"{'=' * 60}\n")
        return True

    except Exception as e:
        # Mark as failed
        processed[audio_path.name]["status"] = "failed"
        processed[audio_path.name]["error"] = str(e)
        processed[audio_path.name]["failed_at"] = datetime.now(timezone.utc).isoformat()
        save_processed(processed)

        print(f"\nERROR processing {audio_path.name}: {e}")
        return False


def watch(interval: int = 10, run_once: bool = False):
    """
    Main watcher loop.
    Scans speech/ folder every `interval` seconds for new audio files.
    """
    print(f"{'=' * 60}")
    print("MEETING BOT WATCHER")
    print(f"{'=' * 60}")
    print(f"Watching: {SPEECH_DIR.resolve()}")
    print(f"Tracking: {PROCESSED_FILE.resolve()}")
    print(f"Interval: {interval}s")
    print(f"Audio types: {', '.join(sorted(AUDIO_EXTENSIONS))}")
    print()

    # Ensure speech/ folder exists
    SPEECH_DIR.mkdir(parents=True, exist_ok=True)

    # Load tracking data
    processed = load_processed()

    already_count = len(processed)
    if already_count > 0:
        done = sum(1 for v in processed.values() if v.get("status") == "done")
        failed = sum(1 for v in processed.values() if v.get("status") == "failed")
        print(f"Previously processed: {done} done, {failed} failed, {already_count} total")

    print(f"\nWaiting for new audio files in speech/ folder...")
    print("(Press Ctrl+C to stop)\n")

    try:
        while True:
            new_files = get_new_files(processed)

            if new_files:
                print(f"Found {len(new_files)} new file(s)!")
                for audio_path in new_files:
                    process_file(audio_path, processed)

            if run_once:
                if not new_files:
                    print("No new files found.")
                break

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n\nWatcher stopped.")
        processed_data = load_processed()
        done = sum(1 for v in processed_data.values() if v.get("status") == "done")
        print(f"Total processed: {done} file(s)")


def main():
    parser = argparse.ArgumentParser(description="Watch speech/ folder for new audio files")
    parser.add_argument(
        "--interval",
        type=int,
        default=10,
        help="Check interval in seconds (default: 10)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process new files once and exit (don't keep watching)",
    )

    args = parser.parse_args()
    watch(interval=args.interval, run_once=args.once)


if __name__ == "__main__":
    main()
