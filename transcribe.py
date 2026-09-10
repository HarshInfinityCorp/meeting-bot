#!/usr/bin/env python3
"""
Meeting Bot Transcriber
- Sarvam Batch API for speech-to-text (with diarization)
- Gemini 2.5 Pro (via NBMG proxy) for meeting summary

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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_BASE_URL = "https://nextbase-model-gateway.infinitycorp.tech/v1/gemini"
GEMINI_MODEL = "gemini-2.5-pro"
OUTPUT_DIR = Path(__file__).parent / "output"


def _fmt_time(seconds: float) -> str:
    """Format seconds as MM:SS."""
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def generate_summary(transcript_text: str, diarized_entries: list = None) -> str:
    """
    Generate a meeting summary using Gemini 2.5 Pro via NBMG proxy.
    Returns formatted summary with: Meeting Agenda, Overall Summary, Outcome.
    """
    if not GEMINI_API_KEY:
        print("   Skipping summary - GEMINI_API_KEY not found in .env")
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

    print(f"6. Generating meeting summary ({GEMINI_MODEL} via NBMG)...")

    try:
        url = f"{GEMINI_BASE_URL}/chat/completions"

        headers = {
            "Authorization": f"Bearer {GEMINI_API_KEY}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": GEMINI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Here is the meeting transcript:\n\n{context}"},
            ],
            "max_tokens": 4000,
            "temperature": 0.3,
        }

        response = httpx.post(url, headers=headers, json=payload, timeout=180.0)
        response.raise_for_status()
        result = response.json()

        # OpenAI-compatible response format
        summary = result["choices"][0]["message"]["content"]

        if not summary:
            print("   Warning: API returned empty content.")
            return ""

        # Token usage
        usage = result.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)

        print(f"   Summary generated! (tokens: {input_tokens} in / {output_tokens} out)")
        print(f"   Cost: FREE (NBMG proxy)")

        return summary

    except Exception as e:
        print(f"   Warning: Summary generation failed - {e}")
        return ""


def transcribe(
    audio_path: str,
    mode: str = "transcribe",
    language: str = "en-IN",
    num_speakers: int = 2,
    skip_summary: bool = False,
) -> dict:
    """
    Transcribe an audio file using Sarvam Batch API with speaker diarization,
    then generate a meeting summary using Gemini 2.5 Pro.
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

    # Step 6: Generate meeting summary (Gemini 2.5 Pro via NBMG)
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
                f.write(f"Generated using: {GEMINI_MODEL} via NBMG\n")
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

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Transcribe audio using Sarvam + Gemini 2.5 Pro summary"
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
