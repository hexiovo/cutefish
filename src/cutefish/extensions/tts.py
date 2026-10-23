"""On-demand Windows System.Speech preview support.

The application never imports an optional TTS package or plays audio. SAPI is
queried only when an authenticated caller asks for capabilities or a preview.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any

from cutefish.core.paths import HostPathResolver, PathPolicyError


MAX_PREVIEW_TEXT = 240
MAX_PREVIEW_BYTES = 12 * 1024 * 1024
POWERSHELL_TIMEOUT_SECONDS = 20

_ENUMERATE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voices = @($synth.GetInstalledVoices() | ForEach-Object {
        [PSCustomObject]@{
            voice_id = [string]$_.VoiceInfo.Id
            name = [string]$_.VoiceInfo.Name
            culture = [string]$_.VoiceInfo.Culture.Name
        }
    })
    ConvertTo-Json -InputObject $voices -Compress -Depth 3
} finally {
    $synth.Dispose()
}
"""

_SYNTHESIZE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
Add-Type -AssemblyName System.Speech
$request = ConvertFrom-Json ([Environment]::GetEnvironmentVariable('CUTEFISH_SAPI_REQUEST'))
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $synth.SelectVoice([string]$request.voice_name)
    $synth.SetOutputToWaveFile([string]$request.output_path)
    $null = $synth.Speak([string]$request.text)
    $synth.SetOutputToNull()
} finally {
    $synth.Dispose()
}
"""


class SpeechError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class TtsCapabilityProbe:
    """Probe the built-in Windows SAPI on explicit request, with a short cache."""

    def __init__(self, paths: HostPathResolver | None = None):
        self.paths = paths
        self._lock = threading.Lock()
        self._voices: list[dict[str, str]] | None = None
        self._probe_state: str | None = None
        self._probe_detail: str | None = None

    def inspect(self) -> dict[str, Any]:
        if os.name != "nt":
            return self._capability_result(
                status="unsupported",
                runtime_available=False,
                voices=[],
                detail="Windows System.Speech/SAPI 仅可在 Windows 主机上使用。",
            )
        with self._lock:
            if self._voices is None:
                try:
                    self._voices = self._enumerate_voices()
                    self._probe_state = "ready" if self._voices else "needs_resource"
                    self._probe_detail = (
                        "已发现 Windows 系统声线；仅支持基础朗读，需用户手动试听和选择。"
                        if self._voices
                        else "System.Speech 可用，但当前没有已安装声线。"
                    )
                except SpeechError as exc:
                    self._voices = []
                    self._probe_state = "needs_resource"
                    self._probe_detail = exc.message
            voices = list(self._voices)
            state = self._probe_state or "needs_resource"
            detail = self._probe_detail or "尚未核验本机 SAPI 声线。"
        return self._capability_result(
            status="partial" if state == "ready" else "needs_resource",
            runtime_available=state == "ready",
            voices=voices,
            detail=detail,
        )

    def synthesize_preview(
        self,
        text: str,
        voice_id: str,
        *,
        max_text_chars: int = MAX_PREVIEW_TEXT,
    ) -> tuple[bytes, dict[str, str]]:
        content = text.strip()
        if type(max_text_chars) is not int or not 1 <= max_text_chars <= 2_000:
            raise SpeechError("invalid_preview_limit", "语音文本长度上限无效。", 500)
        if not content or len(content) > max_text_chars:
            raise SpeechError("invalid_preview_text", f"语音文本必须为1到{max_text_chars}个字符。")
        if not voice_id or len(voice_id) > 512:
            raise SpeechError("invalid_voice_id", "必须指定本机已安装的声线。")

        capability = self.inspect()
        voice = next((item for item in capability["voices"] if item["voice_id"] == voice_id), None)
        if not capability["runtime_available"] or voice is None:
            raise SpeechError("voice_unavailable", "所选声线当前不可用；请刷新本机声线列表。", 409)
        if self.paths is None:
            raise SpeechError("temp_root_unconfigured", "本机临时目录尚未配置。", 409)

        try:
            preview_root = self.paths.resolve("temp_root", "extensions/tts-preview", for_write=True)
            preview_root.mkdir(parents=True, exist_ok=True)
        except (PathPolicyError, OSError) as exc:
            raise SpeechError("temp_root_unavailable", "本机临时目录当前不可写。", 409) from exc

        try:
            with tempfile.TemporaryDirectory(prefix="preview-", dir=preview_root) as temp_dir:
                output_path = Path(temp_dir) / "preview.wav"
                request = json.dumps(
                    {
                        "voice_name": voice["name"],
                        "output_path": str(output_path),
                        "text": content,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                self._run_powershell(_SYNTHESIZE_SCRIPT, env={"CUTEFISH_SAPI_REQUEST": request})
                if not output_path.is_file() or output_path.stat().st_size > MAX_PREVIEW_BYTES:
                    raise SpeechError("speech_output_invalid", "SAPI 未生成有效的短 WAV 试听文件。", 502)
                audio = output_path.read_bytes()
                _validate_wave(audio, max_seconds=120)
                return audio, voice
        except SpeechError:
            raise
        except (OSError, wave.Error, EOFError) as exc:
            raise SpeechError("speech_output_invalid", "SAPI 试听文件未通过 WAV 校验。", 502) from exc

    @staticmethod
    def _capability_result(
        *, status: str, runtime_available: bool, voices: list[dict[str, str]], detail: str
    ) -> dict[str, Any]:
        return {
            "status": status,
            "engine_id": "windows.system_speech",
            "engine_name": "Windows System.Speech/SAPI",
            "runtime_available": runtime_available,
            "voices": voices,
            "approved_voice_id": None,
            "supports_custom_voice": False,
            "supports_instruction_expression": False,
            "supports_semantic_emphasis": False,
            "supports_pause_control": False,
            "default_reply_mode": "text",
            "automatic_voice_reply": False,
            "audio_played": False,
            "model_downloaded": False,
            "detail": detail,
            "limitations": [
                "SAPI 样例不代表原创声线或自定义声线。",
                "SAPI 路线不提供本项目要求的指令语气、语义重音或停顿控制。",
                "生成的 WAV 只用于用户手动试听，不会自动播放、保存为批准声线或进入 QQ Outbox。",
            ],
        }

    @classmethod
    def _enumerate_voices(cls) -> list[dict[str, str]]:
        output = cls._run_powershell(_ENUMERATE_SCRIPT)
        try:
            parsed = json.loads(output or "[]")
        except json.JSONDecodeError as exc:
            raise SpeechError("sapi_probe_failed", "System.Speech 返回的声线清单无法读取。", 503) from exc
        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            raise SpeechError("sapi_probe_failed", "System.Speech 返回了无效的声线清单。", 503)
        voices: list[dict[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            voice_id, name, culture = item.get("voice_id"), item.get("name"), item.get("culture")
            if all(isinstance(value, str) and value.strip() for value in (voice_id, name, culture)):
                voices.append({"voice_id": voice_id.strip(), "name": name.strip(), "culture": culture.strip()})
        return voices

    @staticmethod
    def _run_powershell(script: str, *, env: dict[str, str] | None = None) -> str:
        if os.name != "nt":
            raise SpeechError("tts_unsupported", "Windows System.Speech/SAPI 当前不可用。", 409)
        executable = shutil.which("powershell.exe") or shutil.which("powershell")
        if executable is None:
            raise SpeechError("sapi_runtime_unavailable", "未找到 Windows PowerShell 运行时。", 409)
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            result = subprocess.run(
                [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=process_env,
                timeout=POWERSHELL_TIMEOUT_SECONDS,
                creationflags=creationflags,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SpeechError("sapi_runtime_unavailable", "Windows System.Speech 调用失败或超时。", 503) from exc
        if result.returncode != 0:
            raise SpeechError("sapi_runtime_unavailable", "Windows System.Speech 当前不可用。", 503)
        return result.stdout.strip()


def _validate_wave(audio: bytes, *, max_seconds: float) -> float:
    import io

    with wave.open(io.BytesIO(audio), "rb") as stream:
        if stream.getcomptype() != "NONE" or stream.getnchannels() not in {1, 2}:
            raise wave.Error("unsupported WAV encoding")
        sample_rate = stream.getframerate()
        if not 8_000 <= sample_rate <= 48_000:
            raise wave.Error("unsupported WAV sample rate")
        frame_count = stream.getnframes()
        if frame_count <= 0:
            raise wave.Error("empty WAV")
        duration = frame_count / sample_rate
        if duration > max_seconds:
            raise wave.Error("WAV exceeds the duration limit")
        return duration
