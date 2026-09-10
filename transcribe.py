#!/usr/bin/env python3
"""
Sarvam Batch Speech-to-Text — Meeting Bot Transcriber
Usage:
    python transcribe.py <audio_file> [--mode transcribe|translate|verbatim|codemix] [--lang en-IN] [--speakers 2]

Examples:
    python transcribe.py meeting.mp3
    python transcribe.py meeting.wav --mode translate --speakers 4
    python transcribe.py call.mp3 --lang hi-IN --mode codemix
"""

import os
import sys
import json
import argparse
import time
from pathlib import Path
from dotenv import load_dotenv
from sarvamai import SarvamAI

# Load .env from script directory
load_dotenv(Path(__file__).parent / ".env")

SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")
OUTPUT_DIR = Path(__file__).parent / "output"


def transcribe(audio_path: str, mode: str = "transcribe", language: str = "en-IN", num_speakers: int = 2) -> dict:
    """
    Transcribe an audio file using Sarvam Batch API with speaker diarization.
    
    Returns dict with transcript, diarized entries, and metadata.
    """
    if not SARVAM_API_KEY:
        print("❌ SARVAM_API_KEY not found. Set it in .env file.")
        sys.exit(1)

    audio_file = Path(audio_path)
    if not audio_file.exists():
        print(f"❌ Audio file not found: {audio_path}")
        sys.exit(1)

    print(f"🎙️  File: {audio_file.name} ({audio_file.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"📋  Mode: {mode} | Language: {language} | Speakers: {num_speakers}")
    print()

    # Init client
    client = SarvamAI(api_subscription_key=SARVAM_API_KEY)

    # Step 1: Create job
    print("1️⃣  Creating batch job...")
    job = client.speech_to_text_job.create_job(
        model="saaras:v4",
        mode=mode,
        language_code=language,
        with_diarization=True,
        num_speakers=num_speakers,
    )
    print(f"   Job created: {job.job_id}")

    # Step 2: Upload file
    print("2️⃣  Uploading audio file...")
    job.upload_files(file_paths=[str(audio_file)])
    print("   Upload complete ✅")

    # Step 3: Start processing
    print("3️⃣  Starting transcription...")
    job.start()

    # Step 4: Wait for completion
    print("4️⃣  Processing... (polling every 5s)")
    start_time = time.time()
    job.wait_until_complete(poll_interval=5, timeout=1200)  # 20 min timeout
    elapsed = time.time() - start_time
    print(f"   Done in {elapsed:.0f}s ✅")

    # Step 5: Download results
    print("5️⃣  Downloading transcript...")
    OUTPUT_DIR.mkdir(exist_ok=True)
    job.download_outputs(output_dir=str(OUTPUT_DIR))

    # Find and parse the output JSON
    result_files = list(OUTPUT_DIR.glob("*.json"))
    if not result_files:
        print("❌ No output JSON found")
        sys.exit(1)

    result_file = result_files[-1]  # latest
    with open(result_file, "r") as f:
        result = json.load(f)

    # Also save a human-readable version
    readable_path = OUTPUT_DIR / f"{audio_file.stem}_transcript.txt"
    with open(readable_path, "w") as f:
        f.write(f"=== Transcript: {audio_file.name} ===\n")
        f.write(f"Mode: {mode} | Language: {language}\n")
        f.write(f"{'=' * 50}\n\n")

        # Full transcript
        if "transcript" in result:
            f.write("📝 FULL TRANSCRIPT:\n")
            f.write(result["transcript"])
            f.write("\n\n")

        # Diarized transcript (who said what)
        diarized = result.get("diarized_transcript", {})
        entries = diarized.get("entries", [])
        if entries:
            f.write("🗣️  SPEAKER-WISE TRANSCRIPT:\n")
            f.write("-" * 40 + "\n")
            for entry in entries:
                speaker = f"Speaker {int(entry.get('speaker_id', '0')) + 1}"
                start = entry.get("start_time_seconds", 0)
                end = entry.get("end_time_seconds", 0)
                text = entry.get("transcript", "")
                f.write(f"[{_fmt_time(start)} → {_fmt_time(end)}] {speaker}:\n")
                f.write(f"  {text}\n\n")

    print(f"\n{'=' * 50}")
    print(f"✅ TRANSCRIPTION COMPLETE")
    print(f"{'=' * 50}")
    print(f"📄 Raw JSON:    {result_file}")
    print(f"📝 Readable:    {readable_path}")
    print()

    # Print summary to console
    if "transcript" in result:
        transcript_text = result["transcript"]
        print("📝 TRANSCRIPT:")
        print(transcript_text[:2000])
        if len(transcript_text) > 2000:
            print(f"\n... ({len(transcript_text)} chars total, see full file)")
        print()

    if entries:
        print("🗣️  SPEAKERS:")
        speaker_ids = set(e.get("speaker_id", "0") for e in entries)
        print(f"   {len(speaker_ids)} speaker(s) identified")
        for sid in sorted(speaker_ids):
            count = sum(1 for e in entries if e.get("speaker_id") == sid)
            print(f"   Speaker {int(sid) + 1}: {count} segments")

    return result


def _fmt_time(seconds: float) -> str:
    """Format seconds as MM:SS."""
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def main():
    parser = argparse.ArgumentParser(description="Transcribe audio using Sarvam Batch API")
    parser.add_argument("audio", help="Path to audio file (mp3, wav, etc.)")
    parser.add_argument("--mode", default="transcribe",
                        choices=["transcribe", "translate", "verbatim", "codemix", "translit"],
                        help="Transcription mode (default: transcribe)")
    parser.add_argument("--lang", default="en-IN",
                        help="Language code (default: en-IN)")
    parser.add_argument("--speakers", type=int, default=2,
                        help="Expected number of speakers (default: 2)")

    args = parser.parse_args()
    transcribe(args.audio, mode=args.mode, language=args.lang, num_speakers=args.speakers)


if __name__ == "__main__":
    main()
