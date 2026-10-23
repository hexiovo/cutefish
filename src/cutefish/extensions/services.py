"""Explicit factory for the services owned by this optional extension."""

from __future__ import annotations

from typing import Any

from .asr import AsrService
from .candidates import CandidateService
from .rooms import ManagedMediaReader, RoomService, RoomSourceAuthorizer
from .tasks import TaskService
from .tts import TtsCapabilityProbe
from .voice import VoiceService
from .voice_candidates import VoiceCandidateService


def create_services(context: Any) -> dict[str, Any]:
    """Build services from an application Context without changing core state."""
    tasks = TaskService(
        context.session_factory,
        hard_lock_check=getattr(context, "task_hard_lock_check", None),
        get_deployment_profile=lambda: getattr(context, "deployment_profile", "local_trial"),
    )
    rooms = RoomService(
        context.session_factory,
        context.paths,
        source_authorizer=getattr(context, "room_source_authorizer", None)
        or RoomSourceAuthorizer(context.session_factory),
        media_reader=getattr(context, "room_media_reader", None) or ManagedMediaReader(context.paths),
    )
    tts = TtsCapabilityProbe(context.paths)
    voice_candidates = VoiceCandidateService(
        context.session_factory,
        context.paths,
        getattr(context, "qwen_tts_runtime", None),
        getattr(context, "local_audio_lane", None),
        deployment_profile=lambda: getattr(context, "deployment_profile", "local_trial"),
    )
    return {
        "tasks": tasks,
        "candidates": CandidateService(
            context.session_factory,
            current_revisions=getattr(context, "candidate_current_revisions", None),
        ),
        "rooms": rooms,
        "tts": tts,
        "voice_candidates": voice_candidates,
        "voice": VoiceService(
            context.session_factory,
            context.paths,
            tts,
            deployment_profile=lambda: getattr(context, "deployment_profile", "local_trial"),
            qq_service=getattr(context, "qq_service", None),
            voice_candidates=voice_candidates,
            qwen_tts_runtime=getattr(context, "qwen_tts_runtime", None),
            local_audio_lane=getattr(context, "local_audio_lane", None),
        ),
        "asr": AsrService(
            context.paths,
            rooms,
            tasks,
            chat_service=getattr(context, "chat_service", None),
            local_audio_lane=getattr(context, "local_audio_lane", None),
        ),
    }
