#!/usr/bin/env python3
"""
Meeting Bot Watcher
Monitors the speech/ folder for new audio files.
When a new file is detected → transcribe → summarize → send to Discord.
Already-processed files are tracked in processed.json and skipped.

Usage:
    python watcher.py          # Process each currently unprocessed audio file once
    python watcher.py --once   # Same one-time processing command (used by meeting-summary)
"""

import argparse
import json
import time
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
        except (OSError, json.JSONDecodeError):
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
        if (
            file_path.is_file()
            and file_path.suffix.lower() in AUDIO_EXTENSIONS
            and file_path.name not in processed
        ):
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

    except Exception as e:  # noqa: BLE001 - persist and report all pipeline failures.
        # Mark as failed
        processed[audio_path.name]["status"] = "failed"
        processed[audio_path.name]["error"] = str(e)
        processed[audio_path.name]["failed_at"] = datetime.now(timezone.utc).isoformat()
        save_processed(processed)

        print(f"\nERROR processing {audio_path.name}: {e}")
        return False


def process_audio_once(audio_path: Path) -> bool:
    """Process exactly one meeting recording, using the normal processed-file guard."""
    resolved_audio = audio_path.resolve()
    if resolved_audio.parent != SPEECH_DIR.resolve():
        raise ValueError("Audio file must be inside the speech/ directory.")
    if not resolved_audio.is_file():
        raise FileNotFoundError(resolved_audio)

    processed = load_processed()
    current = processed.get(resolved_audio.name, {})
    if current.get("status") in {"processing", "done"}:
        print(f"Skipping {resolved_audio.name}: already {current['status']}.")
        return False
    return process_file(resolved_audio, processed)


def watch():
    """Process every currently unprocessed audio file in speech/ once, then exit."""
    print(f"{'=' * 60}")
    print("MEETING BOT: PROCESS NEW AUDIO")
    print(f"{'=' * 60}")
    print(f"Speech folder: {SPEECH_DIR.resolve()}")
    print(f"Tracking: {PROCESSED_FILE.resolve()}")
    print()

    SPEECH_DIR.mkdir(parents=True, exist_ok=True)
    processed = load_processed()
    new_files = get_new_files(processed)

    if not new_files:
        print("No unprocessed audio files found.")
        return

    print(f"Found {len(new_files)} new file(s)!")
    for audio_path in new_files:
        process_file(audio_path, processed)


def main():
    parser = argparse.ArgumentParser(
        description="Process currently unprocessed audio files from speech/ once"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="One-time scan (default behavior; retained for meeting-summary)",
    )
    parser.add_argument(
        "--file",
        help="Process one audio file from speech/ exactly once.",
    )
    args = parser.parse_args()
    if args.file:
        process_audio_once(Path(args.file))
    else:
        watch()


if __name__ == "__main__":
    main()
