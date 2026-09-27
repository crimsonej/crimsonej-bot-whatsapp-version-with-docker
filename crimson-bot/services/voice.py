"""
services/voice.py
=================
Voice Notes Service: Speech-to-Text (Whisper) and Text-to-Speech (TTS).
Handles WhatsApp voice notes - download, transcribe, and synthesize speech.
"""

from __future__ import annotations

import os
import tempfile
import subprocess
import base64
import io
from pathlib import Path
from typing import Optional, Dict, Any, List

from core.config import cfg, log


# ─────────────────────────────────────────────────────────────────────────────
# WHISPER STT (Speech-to-Text)
# ─────────────────────────────────────────────────────────────────────────────

_whisper_model = None


def get_whisper_model():
    """Get or load Whisper model (lazy load)."""
    global _whisper_model
    if _whisper_model is None:
        try:
            from faster_whisper import WhisperModel
            model_size = cfg("whisper_model_size") or "base"  # tiny, base, small, medium, large
            device = cfg("whisper_device") or "cpu"
            compute_type = cfg("whisper_compute_type") or "int8"
            
            _whisper_model = WhisperModel(
                model_size,
                device=device,
                compute_type=compute_type,
            )
            log.info("[Voice] Loaded Whisper model: %s (%s/%s)", 
                     model_size, device, compute_type)
        except Exception as e:
            log.error("[Voice] Failed to load Whisper: %s", e)
            raise
    return _whisper_model


def transcribe_audio(audio_path: str, language: str = None) -> Dict[str, Any]:
    """
    Transcribe audio file to text using Whisper.
    
    Returns:
        Dict with keys: text, language, segments, duration
    """
    model = get_whisper_model()
    
    try:
        segments, info = model.transcribe(
            audio_path,
            language=language,
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        
        text_parts = []
        segments = []
        
        for segment in segments:
            text_parts.append(segment.text.strip())
            segments.append({
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip(),
            })
        
        full_text = " ".join(text_parts).strip()
        
        return {
            "ok": True,
            "text": full_text,
            "language": info.language,
            "language_probability": info.language_probability,
            "duration": info.duration,
            "segments": segments,
        }
        
    except Exception as e:
        log.error("[Voice] Transcription failed: %s", e)
        return {"ok": False, "error": str(e)}


def transcribe_audio_bytes(audio_bytes: bytes, mime_type: str = "audio/ogg") -> Dict[str, Any]:
    """Transcribe audio from bytes."""
    # Save to temp file
    suffix = _mime_to_suffix(mime_type)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes)
        temp_path = f.name
    
    try:
        return transcribe_audio(temp_path)
    finally:
        try:
            os.unlink(temp_path)
        except Exception:
            pass


def _mime_to_suffix(mime_type: str) -> str:
    """Convert MIME type to file suffix."""
    mapping = {
        "audio/ogg": ".ogg",
        "audio/mp4": ".m4a",
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "audio/webm": ".webm",
        "audio/3gpp": ".3gp",
    }
    return mapping.get(mime_type, ".ogg")


# ─────────────────────────────────────────────────────────────────────────────
# TEXT-TO-SPEECH (TTS)
# ─────────────────────────────────────────────────────────────────────────────

_tts_engine = None


def get_tts_engine():
    """Get or initialize TTS engine."""
    global _tts_engine
    if _tts_engine is None:
        tts_type = cfg("tts_engine") or "coqui"  # coqui, piper, gtts, edge
        
        if tts_type == "coqui":
            try:
                from TTS.api import TTS
                _tts_engine = {
                    "type": "coqui",
                    "model": TTS(model_name="tts_models/en/ljspeech/tacotron2-DDC", progress_bar=False),
                }
                log.info("[Voice] Loaded Coqui TTS")
            except Exception as e:
                log.warning("[Voice] Coqui TTS failed, falling back: %s", e)
                tts_type = "gtts"
        
        if tts_type == "gtts":
            _tts_engine = {"type": "gtts"}
            log.info("[Voice] Using gTTS")
        
        elif tts_type == "piper":
            try:
                import piper
                _tts_engine = {"type": "piper", "model": piper.PiperVoice.load("en_US-lessac-medium")}
                log.info("[Voice] Loaded Piper TTS")
            except Exception:
                tts_type = "gtts"
                _tts_engine = {"type": "gtts"}
    
    return _tts_engine


def synthesize_speech(text: str, language: str = "en", voice: str = None) -> Dict[str, Any]:
    """
    Synthesize speech from text.
    
    Returns:
        Dict with: ok, audio_base64, mime_type, duration, error?
    """
    engine = get_tts_engine()
    
    try:
        if engine["type"] == "coqui":
            return _synthesize_coqui(engine["model"], text, language, voice)
        elif engine["type"] == "piper":
            return _synthesize_piper(engine["model"], text, language, voice)
        else:
            return _synthesize_gtts(text, language)
    except Exception as e:
        log.error("[Voice] TTS failed: %s", e)
        return {"ok": False, "error": str(e)}


def _synthesize_coqui(model, text: str, language: str, voice: str) -> Dict[str, Any]:
    """Synthesize using Coqui TTS."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        temp_path = f.name
    
    try:
        model.tts_to_file(text=text, file_path=temp_path, language=language)
        
        with open(temp_path, "rb") as f:
            audio_bytes = f.read()
        
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        
        # Get duration using ffprobe
        duration = _get_audio_duration(temp_path)
        
        return {
            "ok": True,
            "audio_base64": audio_b64,
            "mime_type": "audio/wav",
            "duration": duration,
        }
    finally:
        try:
            os.unlink(temp_path)
        except Exception:
            pass


def _synthesize_piper(model, text: str, language: str, voice: str) -> Dict[str, Any]:
    """Synthesize using Piper TTS."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        temp_path = f.name
    
    try:
        model.synthesize(text, temp_path)
        
        with open(temp_path, "rb") as f:
            audio_bytes = f.read()
        
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        duration = _get_audio_duration(temp_path)
        
        return {
            "ok": True,
            "audio_base64": audio_b64,
            "mime_type": "audio/wav",
            "duration": duration,
        }
    finally:
        try:
            os.unlink(temp_path)
        except Exception:
            pass


def _synthesize_gtts(text: str, language: str) -> Dict[str, Any]:
    """Synthesize using gTTS (Google Text-to-Speech)."""
    from gtts import gTTS
    
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        temp_path = f.name
    
    try:
        tts = gTTS(text=text, lang=language, slow=False)
        tts.save(temp_path)
        
        with open(temp_path, "rb") as f:
            audio_bytes = f.read()
        
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
        duration = _get_audio_duration(temp_path)
        
        return {
            "ok": True,
            "audio_base64": audio_b64,
            "mime_type": "audio/mp3",
            "duration": duration,
        }
    finally:
        try:
            os.unlink(temp_path)
        except Exception:
            pass


def _get_audio_duration(file_path: str) -> float:
    """Get audio duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True, text=True, timeout=5
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# AUDIO CONVERSION
# ─────────────────────────────────────────────────────────────────────────────

def convert_audio(input_path: str, output_format: str = "wav", 
                  sample_rate: int = 16000, channels: int = 1) -> str:
    """
    Convert audio file to target format using ffmpeg.
    
    Returns path to converted file.
    """
    output_path = tempfile.mktemp(suffix=f".{output_format}")
    
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-c:a", "pcm_s16le" if output_format == "wav" else "libmp3lame",
        output_path,
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr}")
    
    return output_path


def convert_audio_bytes(audio_bytes: bytes, input_format: str, 
                       output_format: str = "wav") -> bytes:
    """Convert audio bytes from one format to another."""
    with tempfile.NamedTemporaryFile(suffix=f".{input_format}", delete=False) as f_in:
        f_in.write(audio_bytes)
        input_path = f_in.name
    
    output_format = output_format.lower()
    output_path = tempfile.mktemp(suffix=f".{output_format}")
    
    try:
        output_path = convert_audio(input_path, output_format)
        with open(output_path, "rb") as f:
            return f.read()
    finally:
        for path in [input_path, output_path]:
            try:
                os.unlink(path)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# VOICE NOTE TOOLS
# ─────────────────────────────────────────────────────────────────────────────

TRANSCRIBE_TOOL = {
    "type": "function",
    "function": {
        "name": "transcribe_voice",
        "description": "Transcribe a voice note/audio file to text. Provide audio as base64 or file path.",
        "parameters": {
            "type": "object",
            "properties": {
                "audio_base64": {"type": "string", "description": "Base64 encoded audio data"},
                "audio_path": {"type": "string", "description": "Local path to audio file"},
                "mime_type": {"type": "string", "description": "MIME type (audio/ogg, audio/mp4, etc.)"},
                "language": {"type": "string", "description": "Language code (en, es, fr, etc.)", "default": "en"},
            },
            "required": [],
        }
    }
}

SYNTHESIZE_TOOL = {
    "type": "function",
    "function": {
        "name": "synthesize_speech",
        "description": "Convert text to speech and return audio as base64.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to synthesize"},
                "language": {"type": "string", "default": "en"},
                "voice": {"type": "string", "description": "Voice name (optional)"},
            },
            "required": ["text"],
        }
    }
}


def execute_transcribe_voice(
    audio_base64: str = "",
    audio_path: str = "",
    mime_type: str = "audio/ogg",
    language: str = "en",
) -> Dict[str, Any]:
    """Execute voice transcription tool."""
    if audio_base64:
        audio_bytes = base64.b64decode(audio_base64)
        return transcribe_audio_bytes(audio_bytes, mime_type)
    elif audio_path:
        return transcribe_audio(audio_path, language)
    else:
        return {"ok": False, "error": "Provide audio_base64 or audio_path"}


def execute_synthesize_speech(
    text: str,
    language: str = "en",
    voice: str = None,
) -> Dict[str, Any]:
    """Execute speech synthesis tool."""
    return synthesize_speech(text, language, voice)


# ─────────────────────────────────────────────────────────────────────────────
# VOICE COMMANDS FOR BOT
# ─────────────────────────────────────────────────────────────────────────────

VOICE_COMMANDS = {
    "transcribe": "Transcribe a voice note (reply to voice with /transcribe)",
    "speak": "Convert text to speech: /speak Hello world",
    "voice_lang": "Set voice language: /voice_lang en",
    "voice_engine": "Set TTS engine: /voice_engine coqui|gtts|piper",
}

# Tool definitions for LLM
VOICE_TOOLS = [TRANSCRIBE_TOOL, SYNTHESIZE_TOOL]


def handle_voice_command(raw_question: str, user_id: str, sender_jid: str) -> Optional[Dict]:
    """Handle voice-related slash commands."""
    lower = raw_question.lower().strip()
    
    if lower.startswith("/speak "):
        text = raw_question[7:].strip()
        if not text:
            return {"reply": "Usage: `/speak <text to speak>`"}
        
        result = synthesize_speech(text)
        if result.get("ok"):
            return {
                "audio_base64": result["audio_base64"],
                "mime_type": result["mime_type"],
                "filename": f"speech.{result['mime_type'].split('/')[1]}",
                "reply": f"🔊 Here's your speech ({result.get('duration', 0):.1f}s)"
            }
        return {"reply": f"TTS failed: {result.get('error')}"}
    
    if lower == "/transcribe":
        return {"reply": "Reply to a voice note with `/transcribe` to transcribe it."}
    
    if lower.startswith("/voice_lang "):
        lang = lower.split()[1]
        # Store user preference
        from services.storage import profile_update
        profile_update(user_id, voice_language=lang)
        return {"reply": f"Voice language set to: {lang}"}
    
    if lower.startswith("/voice_engine "):
        engine = lower.split()[1]
        from core.config import _cfg
        _cfg["tts_engine"] = engine
        return {"reply": f"TTS engine set to: {engine}"}
    
    return None