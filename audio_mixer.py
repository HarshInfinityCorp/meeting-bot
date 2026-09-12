"""Streaming WAV mixer for synchronized Pycord participant recordings."""

from __future__ import annotations

import audioop
import contextlib
import wave
from pathlib import Path

CHUNK_FRAMES = 4_800  # 100 ms at Discord's usual 48 kHz PCM rate.


class AudioMixError(ValueError):
    """Raised when participant recordings cannot safely be mixed."""


def mix_wav_files(inputs: list[Path], output: Path) -> Path:
    """Mix aligned PCM WAV files into one WAV without loading the meeting into memory.

    Pycord's ``sync_start=True`` pads participants that begin speaking later, so each
    WAV shares the same zero-time. Every input must therefore use the same PCM format.
    Participant samples are scaled before summing to avoid clipping.
    """
    if not inputs:
        raise AudioMixError("At least one participant recording is required.")

    output.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        readers = [stack.enter_context(wave.open(str(path), "rb")) for path in inputs]
        first = readers[0]
        format_key = (first.getnchannels(), first.getsampwidth(), first.getframerate(), first.getcomptype())
        if format_key[3] != "NONE":
            raise AudioMixError("Only uncompressed PCM WAV files can be mixed.")
        for reader, source in zip(readers[1:], inputs[1:], strict=True):
            candidate = (
                reader.getnchannels(),
                reader.getsampwidth(),
                reader.getframerate(),
                reader.getcomptype(),
            )
            if candidate != format_key:
                raise AudioMixError(f"Recording format does not match: {source.name}")

        channels, width, rate, _ = format_key
        frame_width = channels * width
        scale = 1 / len(readers)
        with wave.open(str(output), "wb") as writer:
            writer.setnchannels(channels)
            writer.setsampwidth(width)
            writer.setframerate(rate)
            writer.setcomptype("NONE", "not compressed")

            while True:
                chunks = [reader.readframes(CHUNK_FRAMES) for reader in readers]
                if not any(chunks):
                    break
                max_size = max(len(chunk) for chunk in chunks)
                if max_size % frame_width:
                    raise AudioMixError("Participant WAV contains incomplete PCM frames.")
                mixed = b"\x00" * max_size
                for chunk in chunks:
                    padded = chunk.ljust(max_size, b"\x00")
                    mixed = audioop.add(mixed, audioop.mul(padded, width, scale), width)
                writer.writeframesraw(mixed)

    return output
