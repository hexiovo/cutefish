"""Stable, domain-neutral errors for local audio worker jobs."""

from __future__ import annotations


class LocalAudioError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class LocalAudioBusy(LocalAudioError):
    def __init__(self, operation: str):
        super().__init__(
            "local_audio_busy",
            "本机语音资源正忙，请稍后重试。",
            409,
        )
        self.operation = operation


class LocalAudioUnavailable(LocalAudioError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, 409)


class LocalAudioTimeout(LocalAudioError):
    def __init__(self, operation: str):
        super().__init__(
            "local_audio_timeout",
            "本机语音任务超时，已停止处理。",
            504,
        )
        self.operation = operation


class LocalAudioWorkerStartError(LocalAudioError):
    def __init__(self, operation: str):
        super().__init__(
            "local_audio_worker_unavailable",
            "本机语音 worker 当前不可用。",
            503,
        )
        self.operation = operation
