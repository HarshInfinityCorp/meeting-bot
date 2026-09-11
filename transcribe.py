#!/usr/bin/env python3
"""
Meeting Bot Transcriber
- Sarvam Batch API for speech-to-text (with diarization)
- Composer 2.5 (via NBMG xAI proxy) for meeting summary
- Discord webhook for auto-delivery

Usage:
    python transcribe.py <audio_file> [--mode transcribe|translate|verbatim|codemix] [--lang en-IN] [--speakers 2] [--no-summary]

Examples:
    python transcribe.py meeting.mp3
    python transcribe.py meeting.wav --mode translate --speakers 4
    python transcribe.py call.mp3 --lang hi-IN --mode codemix
    python transcribe.py meeting.mp3 --no-summary
"""

import os
import sys
import json
import argparse
import time
from pathlib import Path
from dotenv import load_dotenv
from sarvamai import SarvamAI
import httpx

# Load .env from script directory
load_dotenv(Path(__file__).parent / ".env")

SARVAM_API_KEY = os.getenv("SARVAM_API_KEY")
NBMG_API_KEY = os.getenv("NBMG_API_KEY")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")
XAI_BASE_URL = "https://nextbase-model-gateway.infinitycorp.tech/v1/xai"
COMPOSER_MODEL = "composer-2.5"
OUTPUT_DIR = Path(__file__).parent / "output"


def _fmt_time(seconds: float) -> str:
    """Format seconds as MM:SS."""
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def generate_summary(transcript_text: str, diarized_entries: list = None) -> str:
    """
    Generate a meeting summary using Composer 2.5 via NBMG xAI proxy.
    Uses the OpenAI Responses API format.
    Returns formatted summary with: Meeting Agenda, Overall Summary, Outcome.
    """
    if not NBMG_API_KEY:
        print("   Skipping summary - NBMG_API_KEY not found in .env")
        return ""

    # Build context from diarized transcript if available
    if diarized_entries:
        context = "\n".join(
            f"Speaker {int(e.get('speaker_id', '0')) + 1} "
            f"[{_fmt_time(e.get('start_time_seconds', 0))}]: "
            f"{e.get('transcript', '')}"
            for e in diarized_entries
        )
    else:
        context = transcript_text

    system_prompt = (
        "You are a professional meeting summarizer. "
        "You MUST produce all three sections completely. "
        "Be concise - use short bullet points, not long paragraphs.\n\n"
        "## MEETING AGENDA\n"
        "List only the 3-5 most important topics discussed. "
        "Group related items together. Keep it short.\n\n"
        "## OVERALL SUMMARY\n"
        "A concise 3-5 sentence paragraph covering the key discussion points "
        "and important details.\n\n"
        "## OUTCOME\n"
        "List decisions made, action items, and next steps "
        "(max 8-10 bullet points, one line each).\n\n"
        "Rules: Be concise. Only include facts from the transcript. "
        "Complete ALL three sections."
    )

    print(f"6. Generating meeting summary ({COMPOSER_MODEL} via NBMG)...")

    try:
        url = f"{XAI_BASE_URL}/responses"

        headers = {
            "Authorization": f"Bearer {NBMG_API_KEY}",
            "Content-Type": "application/json",
        }

        # Responses API format
        payload = {
            "model": COMPOSER_MODEL,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Here is the meeting transcript:\n\n{context}"},
            ],
        }

        response = httpx.post(url, headers=headers, json=payload, timeout=180.0)
        response.raise_for_status()
        result = response.json()

        # Parse Responses API output
        # output[0] = reasoning (type: "reasoning"), output[1] = message (type: "message")
        summary = ""
        for item in result.get("output", []):
            if item.get("type") == "message":
                for content_part in item.get("content", []):
                    if content_part.get("type") == "output_text":
                        summary = content_part.get("text", "")
                        break
                break

        if not summary:
            print("   Warning: API returned empty content.")
            return ""

        # Token usage
        usage = result.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)

        print(f"   Summary generated! (tokens: {input_tokens} in / {output_tokens} out)")
        print(f"   Model: {result.get('model', COMPOSER_MODEL)}")

        return summary

    except Exception as e:
        print(f"   Warning: Summary generation failed - {e}")
        return ""


def send_to_discord(summary: str, audio_name: str, transcript_path: Path, summary_path: Path):
    """
    Send meeting summary text + attach transcript and summary files to Discord via webhook.
    """
    if not DISCORD_WEBHOOK:
        return

    print("\n7. Sending to Discord...")

    try:
        # Truncate summary for Discord message (max 2000 chars)
        header = f"**Meeting Summary: {audio_name}**\n\n"
        max_summary_len = 1900 - len(header)
        display_summary = summary[:max_summary_len]
        if len(summary) > max_summary_len:
            display_summary += "\n\n_(full summary attached as file)_"
        message_content = header + display_summary

        # Build multipart form data with files
        files_to_send = {}
        if transcript_path.exists():
            files_to_send["file1"] = (
                transcript_path.name,
                open(transcript_path, "rb"),
                "text/plain",
            )
        if summary_path.exists():
            files_to_send["file2"] = (
                summary_path.name,
                open(summary_path, "rb"),
                "text/plain",
            )

        data = {"content": message_content}

        response = httpx.post(
            DISCORD_WEBHOOK,
            data=data,
            files=list(files_to_send.items()),
            timeout=30.0,
        )
        response.raise_for_status()

        # Close file handles
        for _, file_tuple in files_to_send.items():
            file_tuple[1].close()

        print("   Sent to Discord!")

    except Exception as e:
        print(f"   Warning: Discord webhook failed - {e}")


def transcribe(
    audio_path: str,
    mode: str = "transcribe",
    language: str = "en-IN",
    num_speakers: int = 2,
    skip_summary: bool = False,
) -> dict:
    """
    Transcribe an audio file using Sarvam Batch API with speaker diarization,
    then generate a meeting summary using Composer 2.5.
    """
    if not SARVAM_API_KEY:
        print("SARVAM_API_KEY not found. Set it in .env file.")
        sys.exit(1)

    audio_file = Path(audio_path)
    if not audio_file.exists():
        print(f"Audio file not found: {audio_path}")
        sys.exit(1)

    file_size_mb = audio_file.stat().st_size / 1024 / 1024
    print(f"File: {audio_file.name} ({file_size_mb:.1f} MB)")
    print(f"Mode: {mode} | Language: {language} | Speakers: {num_speakers}")
    print()

    # Init client
    client = SarvamAI(api_subscription_key=SARVAM_API_KEY)

    # Step 1: Create job
    print("1. Creating batch job...")
    job = client.speech_to_text_job.create_job(
        model="saaras:v4",
        mode=mode,
        language_code=language,
        with_diarization=True,
        num_speakers=num_speakers,
    )
    print(f"   Job created: {job.job_id}")

    # Step 2: Upload file
    print("2. Uploading audio file...")
    job.upload_files(file_paths=[str(audio_file)])
    print("   Upload complete")

    # Step 3: Start processing
    print("3. Starting transcription...")
    job.start()

    # Step 4: Wait for completion
    print("4. Processing... (polling every 5s)")
    start_time = time.time()
    job.wait_until_complete(poll_interval=5, timeout=1200)
    elapsed = time.time() - start_time
    print(f"   Done in {elapsed:.0f}s")

    # Step 5: Download results
    print("5. Downloading transcript...")
    OUTPUT_DIR.mkdir(exist_ok=True)
    job.download_outputs(output_dir=str(OUTPUT_DIR))

    # Find and parse the output JSON
    result_files = list(OUTPUT_DIR.glob("*.json"))
    if not result_files:
        print("No output JSON found")
        sys.exit(1)

    result_file = result_files[-1]
    with open(result_file, "r", encoding="utf-8") as f:
        result = json.load(f)

    # Save human-readable transcript
    readable_path = OUTPUT_DIR / f"{audio_file.stem}_transcript.txt"
    with open(readable_path, "w", encoding="utf-8") as f:
        f.write(f"=== Transcript: {audio_file.name} ===\n")
        f.write(f"Mode: {mode} | Language: {language}\n")
        f.write(f"{'=' * 50}\n\n")

        if "transcript" in result:
            f.write("FULL TRANSCRIPT:\n")
            f.write(result["transcript"])
            f.write("\n\n")

        diarized = result.get("diarized_transcript", {})
        entries = diarized.get("entries", [])
        if entries:
            f.write("SPEAKER-WISE TRANSCRIPT:\n")
            f.write("-" * 40 + "\n")
            for entry in entries:
                speaker = f"Speaker {int(entry.get('speaker_id', '0')) + 1}"
                start = entry.get("start_time_seconds", 0)
                end = entry.get("end_time_seconds", 0)
                text = entry.get("transcript", "")
                f.write(f"[{_fmt_time(start)} -> {_fmt_time(end)}] {speaker}:\n")
                f.write(f"  {text}\n\n")

    print(f"\n{'=' * 50}")
    print("TRANSCRIPTION COMPLETE")
    print(f"{'=' * 50}")
    print(f"Raw JSON:    {result_file}")
    print(f"Readable:    {readable_path}")
    print()

    # Print transcript preview to console
    if "transcript" in result:
        transcript_text = result["transcript"]
        print("TRANSCRIPT:")
        print(transcript_text[:2000])
        if len(transcript_text) > 2000:
            print(f"\n... ({len(transcript_text)} chars total, see full file)")
        print()

    if entries:
        print("SPEAKERS:")
        speaker_ids = set(e.get("speaker_id", "0") for e in entries)
        print(f"   {len(speaker_ids)} speaker(s) identified")
        for sid in sorted(speaker_ids):
            count = sum(1 for e in entries if e.get("speaker_id") == sid)
            print(f"   Speaker {int(sid) + 1}: {count} segments")

    # Step 6: Generate meeting summary (Composer 2.5 via NBMG)
    summary = ""
    if not skip_summary:
        diarized = result.get("diarized_transcript", {})
        entries_for_summary = diarized.get("entries", [])
        transcript_text = result.get("transcript", "")

        summary = generate_summary(transcript_text, entries_for_summary)

        if summary:
            # Save summary as separate file
            summary_path = OUTPUT_DIR / f"{audio_file.stem}_summary.txt"
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(f"=== Meeting Summary: {audio_file.name} ===\n")
                f.write(f"Generated using: {COMPOSER_MODEL} via NBMG\n")
                f.write(f"{'=' * 50}\n\n")
                f.write(summary)
                f.write("\n")

            # Append summary to the transcript file too
            with open(readable_path, "a", encoding="utf-8") as f:
                f.write(f"\n\n{'=' * 50}\n")
                f.write("MEETING SUMMARY (AI-Generated)\n")
                f.write(f"{'=' * 50}\n\n")
                f.write(summary)
                f.write("\n")

            print(f"\n{'=' * 50}")
            print("MEETING SUMMARY")
            print(f"{'=' * 50}")
            try:
                print(summary)
            except UnicodeEncodeError:
                print(summary.encode("ascii", errors="replace").decode())
            print(f"\nSummary saved: {summary_path}")

    # Step 7: Send to Discord via webhook
    if DISCORD_WEBHOOK:
        summary_path = OUTPUT_DIR / f"{audio_file.stem}_summary.txt"
        send_to_discord(
            summary=summary if summary else "(Summary generation was skipped or failed)",
            audio_name=audio_file.name,
            transcript_path=readable_path,
            summary_path=summary_path,
        )

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Transcribe audio using Sarvam + Composer 2.5 summary"
    )
    parser.add_argument("audio", help="Path to audio file (mp3, wav, etc.)")
    parser.add_argument(
        "--mode",
        default="transcribe",
        choices=["transcribe", "translate", "verbatim", "codemix", "translit"],
        help="Transcription mode (default: transcribe)",
    )
    parser.add_argument("--lang", default="en-IN", help="Language code (default: en-IN)")
    parser.add_argument(
        "--speakers", type=int, default=2, help="Expected number of speakers (default: 2)"
    )
    parser.add_argument(
        "--no-summary", action="store_true", help="Skip AI summary generation"
    )

    args = parser.parse_args()
    transcribe(
        args.audio,
        mode=args.mode,
        language=args.lang,
        num_speakers=args.speakers,
        skip_summary=args.no_summary,
    )


if __name__ == "__main__":
    main()
