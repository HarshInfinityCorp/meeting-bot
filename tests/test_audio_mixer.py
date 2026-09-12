import tempfile
import unittest
import wave
from pathlib import Path

from audio_mixer import AudioMixError, mix_wav_files


class AudioMixerTests(unittest.TestCase):
    def write_wave(self, path: Path, frames: bytes, *, rate: int = 48_000) -> None:
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(rate)
            writer.writeframes(frames)

    def read_wave(self, path: Path) -> tuple[int, bytes]:
        with wave.open(str(path), "rb") as reader:
            return reader.getnframes(), reader.readframes(reader.getnframes())

    def test_mixes_aligned_participants_into_one_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = root / "one.wav"
            two = root / "two.wav"
            mixed = root / "meeting.wav"
            # 16-bit mono samples: 1000 and 3000. Scaling each by 1/2 produces 2000.
            self.write_wave(one, (1000).to_bytes(2, "little", signed=True) * 3)
            self.write_wave(two, (3000).to_bytes(2, "little", signed=True) * 3)

            self.assertEqual(mix_wav_files([one, two], mixed), mixed)
            frames, pcm = self.read_wave(mixed)
            self.assertEqual(frames, 3)
            self.assertEqual(
                [int.from_bytes(pcm[index : index + 2], "little", signed=True) for index in range(0, len(pcm), 2)],
                [2000, 2000, 2000],
            )

    def test_rejects_mismatched_audio_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = root / "one.wav"
            two = root / "two.wav"
            self.write_wave(one, b"\x00\x00", rate=48_000)
            self.write_wave(two, b"\x00\x00", rate=44_100)

            with self.assertRaises(AudioMixError):
                mix_wav_files([one, two], root / "meeting.wav")


if __name__ == "__main__":
    unittest.main()
