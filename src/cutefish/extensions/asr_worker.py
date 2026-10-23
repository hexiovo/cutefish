"""Single-shot faster-whisper subprocess; process exit releases model memory."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--audio-path", required=True)
    args = parser.parse_args()

    with contextlib.redirect_stdout(sys.stderr):
        from faster_whisper import WhisperModel

        model = WhisperModel(
            args.model_path,
            device="cpu",
            compute_type="int8",
        )
        segments, info = model.transcribe(
            args.audio_path,
            task="transcribe",
            beam_size=1,
            best_of=1,
            temperature=0,
            condition_on_previous_text=False,
            vad_filter=False,
            word_timestamps=False,
        )
        text = "".join(segment.text for segment in segments).strip()
    print(json.dumps({"text": text, "language": getattr(info, "language", None)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
