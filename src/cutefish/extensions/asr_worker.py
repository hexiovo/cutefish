"""Single-shot, local-only audio decoding and faster-whisper inference."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
import wave
from pathlib import Path
from typing import Any


MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_AUDIO_SAMPLE_RATE = 192_000
MIN_AUDIO_SAMPLE_RATE = 8_000
MAX_AUDIO_CHANNELS = 8
NORMALIZED_SAMPLE_RATE = 16_000

MEDIA_CODECS = {
    "audio/wav": {
        "pcm_u8",
        "pcm_s8",
        "pcm_s16le",
        "pcm_s16be",
        "pcm_s24le",
        "pcm_s24be",
        "pcm_s32le",
        "pcm_s32be",
        "pcm_f32le",
        "pcm_f32be",
        "pcm_f64le",
        "pcm_f64be",
    },
    "audio/mpeg": {"mp3"},
    "audio/mp4": {"aac", "alac", "mp3", "flac"},
    "audio/ogg": {"opus", "vorbis", "flac"},
    "audio/flac": {"flac"},
}


class WorkerFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _resolve_child(path: str, workspace: Path, *, must_exist: bool = True) -> Path:
    candidate = Path(path).resolve(strict=must_exist)
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise WorkerFailure("invalid_audio") from exc
    return candidate


def _detect_demuxer(audio_path: Path) -> str:
    try:
        with audio_path.open("rb") as source:
            prefix = source.read(4096)
    except OSError as exc:
        raise WorkerFailure("invalid_audio") from exc
    text = prefix.decode("utf-8-sig", errors="ignore").lstrip().casefold()
    if text.startswith(("#extm3u", "[playlist]", "<asx", "<?xml", "<playlist")):
        raise WorkerFailure("invalid_audio_container")
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WAVE":
        return "wav"
    if len(prefix) >= 8 and prefix[4:8] in {b"ftyp", b"styp"}:
        return "mov"
    if prefix.startswith(b"OggS"):
        return "ogg"
    if prefix.startswith(b"fLaC"):
        return "flac"
    if prefix.startswith(b"ID3") or (
        len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0
    ):
        return "mp3"
    raise WorkerFailure("invalid_audio_container")


def _media_type(container_format: str) -> str | None:
    names = {name.strip().casefold() for name in container_format.split(",")}
    if "wav" in names:
        return "audio/wav"
    if "mp3" in names:
        return "audio/mpeg"
    if names.intersection({"mp4", "m4a", "mov", "3gp", "3g2", "mj2"}):
        return "audio/mp4"
    if "ogg" in names:
        return "audio/ogg"
    if "flac" in names:
        return "audio/flac"
    return None


def _validate_audio_stream(container: Any, stream: Any) -> tuple[str, str]:
    media_type = _media_type(str(getattr(container.format, "name", "")))
    if media_type is None:
        raise WorkerFailure("unsupported_audio_format")
    codec_name = str(getattr(stream.codec_context, "name", "")).casefold()
    if codec_name not in MEDIA_CODECS[media_type]:
        raise WorkerFailure("unsupported_audio_format")
    return media_type, codec_name


def _write_normalized_audio(audio_path: Path, output_path: Path, max_seconds: int) -> tuple[str, float]:
    demuxer = _detect_demuxer(audio_path)
    try:
        import av
    except ImportError as exc:
        raise WorkerFailure("invalid_audio") from exc

    av.logging.set_level(av.logging.ERROR)
    try:
        # Feeding a local file object keeps FFmpeg in custom-IO mode: an input
        # cannot be interpreted as an HTTP, HLS, or other network URL.
        with audio_path.open("rb") as input_file:
            open_options = {"protocol_whitelist": "pipe"}
            if demuxer == "mov":
                open_options.update({"enable_drefs": "0", "use_absolute_path": "0"})
            with av.open(
                input_file,
                mode="r",
                format=demuxer,
                options=open_options,
            ) as container:
                audio_streams = [stream for stream in container.streams if stream.type == "audio"]
                if not audio_streams:
                    raise WorkerFailure("invalid_audio")
                stream = audio_streams[0]
                media_type, _codec_name = _validate_audio_stream(container, stream)
                resampler = av.AudioResampler(
                    format="s16",
                    layout="mono",
                    rate=NORMALIZED_SAMPLE_RATE,
                )
                input_seconds = 0.0
                timeline_start: float | None = None
                timeline_end: float | None = None
                output_samples = 0

                with wave.open(str(output_path), "wb") as normalized:
                    normalized.setnchannels(1)
                    normalized.setsampwidth(2)
                    normalized.setframerate(NORMALIZED_SAMPLE_RATE)

                    for frame in container.decode(stream):
                        frame_samples = int(frame.samples or 0)
                        sample_rate = int(frame.sample_rate or 0)
                        channels = int(getattr(frame.layout, "nb_channels", 0) or 0)
                        if frame_samples <= 0:
                            continue
                        if (
                            sample_rate < MIN_AUDIO_SAMPLE_RATE
                            or sample_rate > MAX_AUDIO_SAMPLE_RATE
                            or channels < 1
                            or channels > MAX_AUDIO_CHANNELS
                        ):
                            raise WorkerFailure("invalid_audio")

                        frame_seconds = frame_samples / sample_rate
                        input_seconds += frame_seconds
                        if frame.pts is not None and frame.time_base is not None:
                            frame_start = float(frame.pts * frame.time_base)
                            if not math.isfinite(frame_start):
                                raise WorkerFailure("invalid_audio")
                            frame_end = frame_start + frame_seconds
                            if timeline_start is None:
                                timeline_start = frame_start
                            timeline_end = max(timeline_end or frame_end, frame_end)
                        timeline_seconds = (
                            timeline_end - timeline_start
                            if timeline_start is not None and timeline_end is not None
                            else 0.0
                        )
                        if max(input_seconds, timeline_seconds) > max_seconds:
                            raise WorkerFailure("audio_too_long")

                        for converted in resampler.resample(frame):
                            pcm = converted.to_ndarray().tobytes()
                            normalized.writeframesraw(pcm)
                            output_samples += int(converted.samples)

                    for converted in resampler.resample(None):
                        pcm = converted.to_ndarray().tobytes()
                        normalized.writeframesraw(pcm)
                        output_samples += int(converted.samples)

                duration = output_samples / NORMALIZED_SAMPLE_RATE
                if output_samples <= 0 or duration <= 0:
                    raise WorkerFailure("invalid_audio")
                if max(input_seconds, timeline_end - timeline_start if timeline_start is not None else 0.0) > max_seconds:
                    raise WorkerFailure("audio_too_long")
                return media_type, duration
    except WorkerFailure:
        raise
    except (OSError, ValueError, TypeError, wave.Error, EOFError, av.error.FFmpegError) as exc:
        raise WorkerFailure("invalid_audio") from exc


def _transcribe(model_path: Path, audio_path: Path, media_type: str) -> dict[str, Any]:
    try:
        from faster_whisper import WhisperModel

        # model_path is checked as a local directory before this call, and the
        # parent lane also sets Hugging Face offline/cache variables per job.
        model = WhisperModel(
            str(model_path),
            device="cpu",
            compute_type="int8",
        )
        segments, info = model.transcribe(
            str(audio_path),
            task="transcribe",
            beam_size=1,
            best_of=1,
            temperature=0,
            condition_on_previous_text=False,
            vad_filter=False,
            word_timestamps=False,
        )
        result_segments: list[dict[str, Any]] = []
        transcript_parts: list[str] = []
        for segment in segments:
            text = str(segment.text)
            start = float(segment.start)
            end = float(segment.end)
            if not math.isfinite(start) or not math.isfinite(end):
                raise WorkerFailure("invalid_audio")
            transcript_parts.append(text)
            result_segments.append({"start": start, "end": end, "text": text})
        transcript = "".join(transcript_parts).strip()
        if len(transcript) > 12_000 or len(result_segments) > 2_000:
            raise WorkerFailure("invalid_audio")
        return {
            "text": transcript,
            "language": getattr(info, "language", None),
            "media_type": media_type,
            "segments": result_segments,
        }
    except WorkerFailure:
        raise
    except Exception as exc:
        # Avoid returning exception text: provider/library messages can expose
        # host paths, model details, or implementation internals.
        raise WorkerFailure("asr_runtime_error") from exc


def _emit_error(code: str) -> int:
    print(json.dumps({"error": code}, ensure_ascii=False, allow_nan=False))
    return 2


def _configure_offline_workspace(workspace: Path) -> None:
    cache_root = workspace / "cache"
    cache_paths = {
        "HF_HOME": cache_root,
        "HF_HUB_CACHE": cache_root / "hub",
        "HUGGINGFACE_HUB_CACHE": cache_root / "hub",
        "TRANSFORMERS_CACHE": cache_root / "transformers",
        "HF_DATASETS_CACHE": cache_root / "datasets",
        "TORCH_HOME": cache_root / "torch",
        "XDG_CACHE_HOME": cache_root,
    }
    os.environ.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "TMP": str(workspace),
            "TEMP": str(workspace),
            "TMPDIR": str(workspace),
            **{key: str(path) for key, path in cache_paths.items()},
        }
    )
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_HUB_TOKEN"):
        os.environ.pop(name, None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--audio-path", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--max-seconds", type=int, required=True)
    args = parser.parse_args()

    try:
        workspace = Path(args.workspace).resolve(strict=True)
        if not workspace.is_dir() or not (1 <= args.max_seconds <= 120):
            raise WorkerFailure("invalid_audio")
        _configure_offline_workspace(workspace)
        audio_path = _resolve_child(args.audio_path, workspace)
        if not audio_path.is_file() or audio_path.stat().st_size <= 0:
            raise WorkerFailure("invalid_audio")
        if audio_path.stat().st_size > MAX_INPUT_BYTES:
            raise WorkerFailure("invalid_audio")
        model_path = Path(args.model_path).resolve(strict=True)
        if (
            not model_path.is_dir()
            or not (model_path / "model.bin").is_file()
            or not (model_path / "config.json").is_file()
        ):
            raise WorkerFailure("asr_runtime_error")
        normalized_path = _resolve_child(str(workspace / "normalized.wav"), workspace, must_exist=False)
        with contextlib.redirect_stdout(sys.stderr):
            media_type, duration = _write_normalized_audio(audio_path, normalized_path, args.max_seconds)
            result = _transcribe(model_path, normalized_path, media_type)
        result["duration_seconds"] = round(duration, 3)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except WorkerFailure as exc:
        return _emit_error(exc.code)
    except Exception:
        return _emit_error("asr_runtime_error")


if __name__ == "__main__":
    raise SystemExit(main())
