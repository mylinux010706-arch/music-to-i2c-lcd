from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import traceback
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

LCD_ADDRESS = 0x27
LCD_COLUMNS = 16
LCD_ROWS = 2

MIN_DISPLAY_TIME = 0.5
MIN_GAP = 0.1

LCD_SLEEP_GAP = 3.0
LCD_DARK_AT_START = False

OUTPUT_FILE = "generated_lcd_song.ino"

LONG_WORD_MODE = "split"
SCROLL_STEP_TIME = 0.3

TIME_OFFSET = 0.0

START_COUNTDOWN = 3
START_BUTTON_PIN = None
LOOP_PLAYBACK = False
END_HOLD_TIME = 3.0
CREATE_SKETCH_FOLDER = False

TRANSCRIPTION_ENGINE = "openai"
LANGUAGE = None

OPENAI_MODEL = "whisper-1"
OPENAI_TIMEOUT = 600.0
OPENAI_MAX_RETRIES = 3
OPENAI_CHUNK_SECONDS = 600.0
OPENAI_MAX_UPLOAD_BYTES = 24 * 1024 * 1024

LOCAL_MODEL_SIZE = "small"
LOCAL_DEVICE = "cpu"
LOCAL_COMPUTE_TYPE = "int8"

SUPPORTED_EXTENSIONS = (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma")
LONG_WORD_MODES = ("split", "scroll")
ENGINES = ("openai", "local")

FLASH_WARNING_BYTES = 24000
MIN_CHUNK_SECONDS = 60.0
MAX_EVENTS = 65000
DELAY_TOLERANCE = 0.001
MICROSECONDS = 1_000_000


class GeneratorError(Exception):
    pass


class ConfigError(GeneratorError):
    pass


class AudioError(GeneratorError):
    pass


class TranscriptionError(GeneratorError):
    pass


class LayoutError(GeneratorError):
    pass


@dataclass
class Settings:
    output: str
    engine: str
    language: Optional[str]
    offset: float
    sleep_gap: float


@dataclass
class AudioChunk:
    path: Path
    offset: float


@dataclass
class PreparedAudio:
    source: Path
    duration: float
    chunks: list[AudioChunk]


@dataclass
class RawTranscription:
    source: str
    language: str
    duration: float
    text: str
    engine: str
    words: list[dict]
    segments: list[dict]


@dataclass
class TimedWord:
    text: str
    start: float
    end: float
    duration: float
    gap_before: float
    gap_after: float


@dataclass
class LCDToken:
    text: str
    start: float
    end: float
    scroll: bool = False


@dataclass
class LCDEvent:
    time: float
    row: int
    col: int
    text: str
    clear: bool = False
    wake: bool = False
    sleep: bool = False
    scroll: bool = False


@dataclass
class Screen:
    index: int
    start: float
    end: float
    rows: list[str]
    scrolling_rows: list[int]
    wakes: bool
    sleeps_at: Optional[float]


@dataclass
class Playback:
    events: list[LCDEvent]
    screens: list[Screen]
    end_time: float
    delayed: int
    max_delay: float
    initial_sleep_at: Optional[float]
    sleep_gap: float

    @property
    def sleep_count(self) -> int:
        return sum(1 for event in self.events if event.sleep)


TAG_PATTERN = re.compile(r"\[[^\]]*\]")
COMMENT_MARKER_PATTERN = re.compile(r"/(?=[/*])")
LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}$")

TRANSLITERATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2033": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u2026": "...",
        "\u00a0": " ",
        "\u2009": " ",
        "\u200b": "",
        "\u00df": "ss",
        "\u00e6": "ae",
        "\u00c6": "AE",
        "\u00f8": "o",
        "\u00d8": "O",
        "\u0153": "oe",
        "\u0152": "OE",
        "\u0111": "d",
        "\u0110": "D",
        "\u0142": "l",
        "\u0141": "L",
        "\\": "/",
        "~": "-",
    }
)

FLAG_LINES = [
    "const uint8_t FLAG_CLEAR = 1;",
    "const uint8_t FLAG_WAKE = 2;",
    "const uint8_t FLAG_SLEEP = 4;",
]

EVENT_STRUCT_LINES = [
    "struct LcdEvent {",
    "    uint32_t atMs;",
    "    uint8_t row;",
    "    uint8_t col;",
    "    uint8_t flags;",
    "    const char *text;",
    "};",
]

STATE_LINES = [
    "uint16_t nextEvent = 0;",
    "uint32_t playbackStart = 0;",
    "bool playing = false;",
]

PLAY_EVENT_LINES = [
    "bool playEventIfDue(uint16_t index, uint32_t elapsed) {",
    "    LcdEvent event;",
    "    memcpy_P(&event, &EVENTS[index], sizeof(LcdEvent));",
    "    if (elapsed < event.atMs) {",
    "        return false;",
    "    }",
    "    if (event.flags & FLAG_SLEEP) {",
    "        lcd.clear();",
    "        lcd.noBacklight();",
    "        return true;",
    "    }",
    "    if (event.flags & FLAG_WAKE) {",
    "        lcd.backlight();",
    "    }",
    "    if (event.flags & FLAG_CLEAR) {",
    "        lcd.clear();",
    "    }",
    "    lcd.setCursor(event.col, event.row);",
    "    lcd.print(reinterpret_cast<const __FlashStringHelper *>(event.text));",
    "    return true;",
    "}",
]

BEGIN_PLAYBACK_LINES = [
    "void beginPlayback() {",
    "    lcd.backlight();",
    "    lcd.clear();",
    "    nextEvent = 0;",
    "    playbackStart = millis();",
    "    playing = true;",
    "}",
]


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def load_environment() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).resolve().parent / ".env")
    load_dotenv(Path.cwd() / ".env")


def last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else "no details available"


def format_clock(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def to_milliseconds(seconds: float) -> int:
    return int(round(seconds * 1000))


def to_microseconds(seconds: float) -> int:
    return int(round(seconds * MICROSECONDS))


def reaches_sleep_gap(idle_start: float, idle_end: float, limit: float) -> bool:
    return to_microseconds(idle_end) - to_microseconds(idle_start) >= to_microseconds(limit)


def clean_number(value: object) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def has_letters(text: str) -> bool:
    return any(char.isalnum() for char in text)


def sanitize_text(text: str) -> str:
    text = TAG_PATTERN.sub(" ", text)
    text = text.translate(TRANSLITERATION)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = COMMENT_MARKER_PATTERN.sub("", text)
    text = "".join(char if 32 <= ord(char) <= 126 else " " for char in text)
    return " ".join(text.split())


def c_string(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def ensure_comment_free(code: str) -> None:
    if "//" in code or "/*" in code or "*/" in code:
        raise GeneratorError("The generated Arduino code unexpectedly contains a comment marker.")


def validate_config() -> None:
    if not 0 <= LCD_ADDRESS <= 0x7F:
        raise ConfigError("LCD_ADDRESS must be between 0x00 and 0x7F.")
    if not 12 <= LCD_COLUMNS <= 40:
        raise ConfigError("LCD_COLUMNS must be between 12 and 40.")
    if not 1 <= LCD_ROWS <= 4:
        raise ConfigError("LCD_ROWS must be between 1 and 4.")
    for name, value in (
        ("MIN_DISPLAY_TIME", MIN_DISPLAY_TIME),
        ("MIN_GAP", MIN_GAP),
        ("END_HOLD_TIME", END_HOLD_TIME),
    ):
        if value < 0:
            raise ConfigError(f"{name} must not be negative.")
    if SCROLL_STEP_TIME <= 0:
        raise ConfigError("SCROLL_STEP_TIME must be greater than zero.")
    if LCD_SLEEP_GAP is not None and LCD_SLEEP_GAP < 0:
        raise ConfigError("LCD_SLEEP_GAP must not be negative. Use 0 or None to disable LCD sleep.")
    if LONG_WORD_MODE not in LONG_WORD_MODES:
        raise ConfigError('LONG_WORD_MODE must be "split" or "scroll".')
    if TRANSCRIPTION_ENGINE not in ENGINES:
        raise ConfigError('TRANSCRIPTION_ENGINE must be "openai" or "local".')
    if not isinstance(START_COUNTDOWN, int) or not 0 <= START_COUNTDOWN <= 255:
        raise ConfigError("START_COUNTDOWN must be a whole number between 0 and 255.")
    if START_BUTTON_PIN is not None and (not isinstance(START_BUTTON_PIN, int) or START_BUTTON_PIN < 0):
        raise ConfigError("START_BUTTON_PIN must be None or a pin number such as 2.")


def resolve_settings(args: argparse.Namespace) -> Settings:
    validate_config()
    language = args.language if args.language is not None else LANGUAGE
    if language is not None:
        language = language.strip().lower()
        if not LANGUAGE_PATTERN.match(language):
            raise ConfigError("The language must be a short ISO-639-1 code such as 'en' or 'id'.")
    if args.sleep_gap is not None and args.sleep_gap < 0:
        raise ConfigError("--sleep-gap must not be negative. Use 0 to disable LCD sleep.")
    sleep_gap = args.sleep_gap if args.sleep_gap is not None else LCD_SLEEP_GAP
    if sleep_gap is None or sleep_gap <= 0:
        sleep_gap = 0.0
    return Settings(
        output=args.output or OUTPUT_FILE,
        engine=args.engine or TRANSCRIPTION_ENGINE,
        language=language,
        offset=args.offset if args.offset is not None else TIME_OFFSET,
        sleep_gap=float(sleep_gap),
    )


def prompt_for_audio() -> str:
    print("Enter audio file:")
    try:
        return input("> ")
    except EOFError as error:
        raise AudioError("No audio file was provided.") from error


def load_audio(raw_path: str) -> Path:
    cleaned = raw_path.strip().strip('"').strip("'")
    if not cleaned:
        raise AudioError("No audio file was provided.")
    path = Path(cleaned).expanduser()
    if not path.exists():
        raise AudioError(f"File not found: {path}")
    if not path.is_file():
        raise AudioError(f"This is not a file: {path}")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        supported = ", ".join(SUPPORTED_EXTENSIONS)
        raise AudioError(f"Unsupported audio format '{path.suffix}'. Supported formats: {supported}")
    return path


def find_tool(name: str) -> str:
    location = shutil.which(name)
    if location is None:
        raise AudioError(
            f"{name} was not found in PATH. FFmpeg is required. Install it "
            "(Windows: winget install Gyan.FFmpeg, macOS: brew install ffmpeg, "
            "Linux: sudo apt install ffmpeg), then open a new terminal."
        )
    return location


def probe_duration(source: Path) -> float:
    ffprobe = find_tool("ffprobe")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source),
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise AudioError(
            f"FFprobe could not read '{source.name}'. The file may be corrupt or not real audio: "
            f"{last_line(result.stderr)}"
        )
    try:
        duration = float(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError) as error:
        raise AudioError(f"Could not determine the duration of '{source.name}'.") from error
    if not math.isfinite(duration) or duration <= 0:
        raise AudioError(f"'{source.name}' has no playable audio.")
    return duration


def run_ffmpeg(arguments: list[str]) -> None:
    ffmpeg = find_tool("ffmpeg")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"] + arguments
    result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise AudioError(f"FFmpeg failed to convert the audio: {last_line(result.stderr)}")


def render_chunks(source: Path, workdir: Path, duration: float, length: Optional[float]) -> list[AudioChunk]:
    count = 1 if not length or duration <= length else math.ceil(duration / length)
    step = duration / count
    chunks = []
    for index in range(count):
        offset = index * step
        target = workdir / f"part_{index + 1:03d}.flac"
        arguments: list[str] = []
        if count > 1:
            arguments += ["-ss", f"{offset:.3f}"]
        arguments += ["-i", str(source)]
        if count > 1 and index < count - 1:
            arguments += ["-t", f"{step:.3f}"]
        arguments += ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "flac", str(target)]
        run_ffmpeg(arguments)
        chunks.append(AudioChunk(path=target, offset=offset))
    return chunks


def convert_audio(
    source: Path,
    workdir: Path,
    chunk_seconds: Optional[float] = None,
    max_bytes: Optional[int] = None,
) -> PreparedAudio:
    duration = probe_duration(source)
    length = chunk_seconds
    while True:
        chunks = render_chunks(source, workdir, duration, length)
        too_large = max_bytes is not None and any(chunk.path.stat().st_size > max_bytes for chunk in chunks)
        if not too_large:
            return PreparedAudio(source=source, duration=duration, chunks=chunks)
        for chunk in chunks:
            chunk.path.unlink()
        next_length = (length or duration) / 2
        if next_length < MIN_CHUNK_SECONDS:
            raise AudioError("The converted audio is too large for the transcription upload limit.")
        length = next_length


def to_entry(item: object) -> Optional[dict]:
    if hasattr(item, "model_dump"):
        item = item.model_dump()
    if not isinstance(item, dict):
        return None
    text = item.get("text")
    if text is None:
        text = item.get("word")
    start = clean_number(item.get("start"))
    end = clean_number(item.get("end"))
    if text is None or start is None or end is None:
        return None
    return {"text": str(text), "start": start, "end": max(start, end)}


def collect_entries(items: object) -> list[dict]:
    entries = []
    for item in items or []:
        entry = to_entry(item)
        if entry is not None:
            entries.append(entry)
    return entries


def shift_entries(entries: list[dict], offset: float) -> list[dict]:
    return [
        {"text": entry["text"], "start": entry["start"] + offset, "end": entry["end"] + offset}
        for entry in entries
    ]


def describe_api_error(error: Exception) -> str:
    message = getattr(error, "message", None)
    return str(message) if message else str(error)


class AudioTranscriber:
    name = "unknown"

    def transcribe_file(self, path: Path) -> dict:
        raise NotImplementedError

    def transcribe(self, prepared: PreparedAudio) -> RawTranscription:
        words: list[dict] = []
        segments: list[dict] = []
        texts: list[str] = []
        language = "unknown"
        total = len(prepared.chunks)
        for number, chunk in enumerate(prepared.chunks, start=1):
            print(f"Transcribing part {number}/{total} with {self.name}...")
            result = self.transcribe_file(chunk.path)
            words.extend(shift_entries(result["words"], chunk.offset))
            segments.extend(shift_entries(result["segments"], chunk.offset))
            text = str(result.get("text") or "").strip()
            if text:
                texts.append(text)
            if result.get("language"):
                language = str(result["language"])
        return RawTranscription(
            source=prepared.source.name,
            language=language,
            duration=prepared.duration,
            text=" ".join(texts),
            engine=self.name,
            words=words,
            segments=segments,
        )


class OpenAITranscriber(AudioTranscriber):
    def __init__(self, language: Optional[str]) -> None:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise TranscriptionError(
                "OPENAI_API_KEY is not set. Put it in a .env file (see .env.example) or export it in "
                "your terminal. To transcribe without an API key, use --engine local."
            )
        try:
            import openai
        except ImportError as error:
            raise TranscriptionError(
                "The 'openai' package is not installed. Run: pip install -r requirements.txt"
            ) from error
        self.openai = openai
        self.client = openai.OpenAI(api_key=api_key, timeout=OPENAI_TIMEOUT, max_retries=OPENAI_MAX_RETRIES)
        self.language = language
        self.name = f"OpenAI {OPENAI_MODEL}"

    def transcribe_file(self, path: Path) -> dict:
        openai = self.openai
        parameters = {
            "model": OPENAI_MODEL,
            "response_format": "verbose_json",
            "timestamp_granularities": ["word", "segment"],
        }
        if self.language:
            parameters["language"] = self.language
        try:
            with open(path, "rb") as handle:
                response = self.client.audio.transcriptions.create(file=handle, **parameters)
        except openai.AuthenticationError as error:
            raise TranscriptionError("OpenAI rejected the API key. Check OPENAI_API_KEY.") from error
        except openai.PermissionDeniedError as error:
            raise TranscriptionError(
                f"OpenAI denied access to model '{OPENAI_MODEL}': {describe_api_error(error)}"
            ) from error
        except openai.RateLimitError as error:
            raise TranscriptionError(
                "OpenAI rate limit or quota exceeded. Check your billing and usage limits, then try again."
            ) from error
        except openai.APITimeoutError as error:
            raise TranscriptionError(
                "The request to OpenAI timed out. Try again, or use a shorter audio file."
            ) from error
        except openai.APIConnectionError as error:
            raise TranscriptionError(
                "Could not connect to the OpenAI API. Check your internet connection."
            ) from error
        except openai.BadRequestError as error:
            raise TranscriptionError(f"OpenAI rejected the request: {describe_api_error(error)}") from error
        except openai.APIStatusError as error:
            raise TranscriptionError(
                f"OpenAI API error {error.status_code}: {describe_api_error(error)}"
            ) from error
        except openai.OpenAIError as error:
            raise TranscriptionError(f"OpenAI error: {error}") from error
        except OSError as error:
            raise TranscriptionError(f"Could not read '{path}': {error}") from error
        return parse_openai_response(response)


class LocalWhisperTranscriber(AudioTranscriber):
    def __init__(self, language: Optional[str]) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as error:
            raise TranscriptionError(
                "The 'faster-whisper' package is not installed. Run: pip install -r requirements-local.txt"
            ) from error
        print(f"Loading local Whisper model '{LOCAL_MODEL_SIZE}' (the first run downloads it)...")
        try:
            self.model = WhisperModel(LOCAL_MODEL_SIZE, device=LOCAL_DEVICE, compute_type=LOCAL_COMPUTE_TYPE)
        except Exception as error:
            raise TranscriptionError(f"Could not load the local Whisper model: {error}") from error
        self.language = language
        self.name = f"local faster-whisper {LOCAL_MODEL_SIZE}"

    def transcribe_file(self, path: Path) -> dict:
        words: list[dict] = []
        segments: list[dict] = []
        texts: list[str] = []
        try:
            iterator, info = self.model.transcribe(
                str(path),
                language=self.language,
                word_timestamps=True,
                vad_filter=False,
                condition_on_previous_text=False,
            )
            for segment in iterator:
                texts.append(segment.text.strip())
                segments.append({"text": segment.text, "start": segment.start, "end": segment.end})
                for word in segment.words or []:
                    words.append({"text": word.word, "start": word.start, "end": word.end})
        except Exception as error:
            raise TranscriptionError(f"Local transcription failed: {error}") from error
        return {
            "language": info.language,
            "text": " ".join(texts),
            "words": collect_entries(words),
            "segments": collect_entries(segments),
        }


def parse_openai_response(response: object) -> dict:
    if hasattr(response, "model_dump"):
        data = response.model_dump()
    elif isinstance(response, dict):
        data = response
    else:
        data = dict(vars(response))
    return {
        "language": data.get("language"),
        "text": data.get("text") or "",
        "words": collect_entries(data.get("words")),
        "segments": collect_entries(data.get("segments")),
    }


def create_transcriber(engine: str, language: Optional[str]) -> AudioTranscriber:
    if engine == "openai":
        return OpenAITranscriber(language)
    if engine == "local":
        return LocalWhisperTranscriber(language)
    raise ConfigError(f"Unknown transcription engine '{engine}'.")


def transcribe_audio(prepared: PreparedAudio, transcriber: AudioTranscriber) -> RawTranscription:
    return transcriber.transcribe(prepared)


def save_transcript(raw: RawTranscription, path: Path) -> None:
    def rounded(entry: dict) -> dict:
        return {
            "text": entry["text"],
            "start": round(entry["start"], 3),
            "end": round(entry["end"], 3),
        }

    payload = {
        "audio": raw.source,
        "engine": raw.engine,
        "language": raw.language,
        "duration": round(raw.duration, 3),
        "text": raw.text,
        "words": [rounded(entry) for entry in raw.words],
        "segments": [rounded(entry) for entry in raw.segments],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except OSError as error:
        raise GeneratorError(f"Could not save the transcription to '{path}': {error}") from error


def load_transcript(path: Path) -> RawTranscription:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as error:
        raise TranscriptionError(f"Transcription file not found: {path}") from error
    except (OSError, ValueError) as error:
        raise TranscriptionError(f"Could not read the transcription file '{path}': {error}") from error
    if not isinstance(payload, dict):
        raise TranscriptionError("The transcription file must contain a JSON object.")
    words = payload.get("words")
    segments = payload.get("segments")
    return RawTranscription(
        source=str(payload.get("audio") or path.name),
        language=str(payload.get("language") or "unknown"),
        duration=clean_number(payload.get("duration")) or 0.0,
        text=str(payload.get("text") or ""),
        engine="transcription file",
        words=words if isinstance(words, list) else [],
        segments=segments if isinstance(segments, list) else [],
    )


def spread_text(text: str, start: float, end: float) -> list[dict]:
    parts = [part for part in text.split() if has_letters(part)]
    if not parts:
        return []
    total = sum(len(part) for part in parts)
    span = max(end - start, 0.0)
    entries = []
    cursor = start
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        finish = start + span if last else cursor + span * len(part) / total
        entries.append({"text": part, "start": cursor, "end": max(finish, cursor)})
        cursor = finish
    return entries


def normalize_words(items: object) -> list[dict]:
    entries = []
    for entry in collect_entries(items):
        entries.extend(spread_text(sanitize_text(entry["text"]), entry["start"], entry["end"]))
    return entries


def normalize_segments(items: object) -> list[dict]:
    segments = []
    for entry in collect_entries(items):
        text = sanitize_text(entry["text"])
        if has_letters(text):
            segments.append({"text": text, "start": entry["start"], "end": entry["end"]})
    return segments


def has_words(words: list[dict], segment: dict) -> bool:
    return any(word["end"] > segment["start"] and word["start"] < segment["end"] for word in words)


def extract_timestamps(raw: RawTranscription) -> tuple[list[dict], str]:
    words = normalize_words(raw.words)
    segments = normalize_segments(raw.segments)
    if not words and not segments:
        if has_letters(sanitize_text(raw.text)):
            raise TranscriptionError(
                "The transcription contains text but no usable timestamps, so the timing cannot be built."
            )
        raise TranscriptionError(
            "No clear speech was detected, so there is nothing to display. Try a version of the song "
            "with louder vocals, or set the language with --language."
        )
    entries = list(words)
    interpolated = 0
    for segment in segments:
        if not has_words(words, segment):
            entries.extend(spread_text(segment["text"], segment["start"], segment["end"]))
            interpolated += 1
    if not words:
        level = "segment-level fallback (word times interpolated)"
    elif interpolated:
        level = "word-level, with segment-level fallback for some parts"
    else:
        level = "word-level"
    entries.sort(key=lambda entry: (entry["start"], entry["end"]))
    return entries, level


def process_timing(entries: list[dict], offset: float = 0.0) -> list[TimedWord]:
    ordered = sorted(entries, key=lambda entry: (entry["start"], entry["end"]))
    spans: list[dict] = []
    for entry in ordered:
        start = max(0.0, entry["start"] + offset)
        end = max(start, entry["end"] + offset)
        if spans and start < spans[-1]["end"]:
            spans[-1]["end"] = max(spans[-1]["start"], start)
        spans.append({"text": entry["text"], "start": start, "end": end})
    words: list[TimedWord] = []
    previous_end = 0.0
    for span in spans:
        words.append(
            TimedWord(
                text=span["text"],
                start=span["start"],
                end=span["end"],
                duration=span["end"] - span["start"],
                gap_before=max(0.0, span["start"] - previous_end),
                gap_after=0.0,
            )
        )
        previous_end = span["end"]
    for current, following in zip(words, words[1:]):
        current.gap_after = following.gap_before
    return words


def split_long_word(word: TimedWord, columns: int) -> list[LCDToken]:
    letters = word.text
    part_count = math.ceil(len(letters) / (columns - 1))
    base, extra = divmod(len(letters), part_count)
    tokens = []
    position = 0
    cursor = word.start
    for index in range(part_count):
        size = base + (1 if index < extra else 0)
        piece = letters[position:position + size]
        position += size
        last = index == part_count - 1
        end = word.end if last else cursor + word.duration * size / len(letters)
        tokens.append(LCDToken(text=piece if last else piece + "-", start=cursor, end=max(end, cursor)))
        cursor = end
    return tokens


def wrap_text_for_lcd(
    words: list[TimedWord],
    columns: Optional[int] = None,
    mode: Optional[str] = None,
) -> list[LCDToken]:
    columns = LCD_COLUMNS if columns is None else columns
    mode = LONG_WORD_MODE if mode is None else mode
    tokens: list[LCDToken] = []
    for word in words:
        if len(word.text) <= columns:
            tokens.append(LCDToken(text=word.text, start=word.start, end=word.end))
        elif mode == "scroll":
            tokens.append(LCDToken(text=word.text, start=word.start, end=word.end, scroll=True))
        else:
            tokens.extend(split_long_word(word, columns))
    return tokens


class LCDLayoutEngine:
    def __init__(
        self,
        columns: int = LCD_COLUMNS,
        rows: int = LCD_ROWS,
        min_display_time: float = MIN_DISPLAY_TIME,
        min_gap: float = MIN_GAP,
        sleep_gap: Optional[float] = LCD_SLEEP_GAP,
        scroll_step_time: float = SCROLL_STEP_TIME,
        dark_at_start: bool = LCD_DARK_AT_START,
    ) -> None:
        self.columns = columns
        self.rows = rows
        self.min_display_time = min_display_time
        self.min_gap = min_gap
        self.sleep_gap = float(sleep_gap) if sleep_gap and sleep_gap > 0 else 0.0
        self.scroll_step_time = scroll_step_time
        self.dark_at_start = dark_at_start
        self.reset()

    def reset(self) -> None:
        self.row = 0
        self.col = 0
        self.events: list[LCDEvent] = []
        self.last_text_time: Optional[float] = None
        self.audio_end = 0.0
        self.delayed = 0
        self.max_delay = 0.0

    def find_slot(self, length: int, needs_full_row: bool) -> tuple[int, int, bool]:
        if self.col == 0:
            return self.row, 0, False
        if needs_full_row:
            if self.row + 1 < self.rows:
                return self.row + 1, 0, False
            return 0, 0, True
        if self.col + 1 + length <= self.columns:
            return self.row, self.col + 1, False
        if self.row + 1 < self.rows:
            return self.row + 1, 0, False
        return 0, 0, True

    def schedule(self, wanted: float, clear: bool) -> float:
        time = max(wanted, 0.0)
        if self.last_text_time is not None:
            time = max(time, self.last_text_time + self.min_gap)
            if clear:
                time = max(time, self.last_text_time + self.min_display_time)
        delay = time - wanted
        if delay > DELAY_TOLERANCE:
            self.delayed += 1
            self.max_delay = max(self.max_delay, delay)
        return time

    def sleep_before(self, token: LCDToken) -> bool:
        if self.sleep_gap <= 0:
            return False
        if not reaches_sleep_gap(self.audio_end, token.start, self.sleep_gap):
            return False
        due = self.audio_end + self.sleep_gap
        if self.last_text_time is None and self.dark_at_start:
            due = 0.0
        latest = token.start
        if self.last_text_time is not None:
            due = max(due, self.last_text_time + max(self.min_display_time, self.min_gap))
            latest = max(latest, self.last_text_time + self.min_gap)
        self.events.append(LCDEvent(time=min(due, latest), row=0, col=0, text="", sleep=True))
        self.row = 0
        self.col = 0
        return True

    def sleep_after_last(self) -> None:
        if self.sleep_gap <= 0 or self.last_text_time is None:
            return
        due = max(
            self.audio_end + self.sleep_gap,
            self.last_text_time + max(self.min_display_time, self.min_gap),
        )
        self.events.append(LCDEvent(time=due, row=0, col=0, text="", sleep=True))

    def place_text(self, token: LCDToken, wake: bool) -> None:
        length = len(token.text)
        row, col, clear = self.find_slot(length, False)
        time = self.schedule(token.start, clear)
        self.events.append(
            LCDEvent(time=time, row=row, col=col, text=token.text, clear=clear, wake=wake)
        )
        self.last_text_time = time
        self.row = row
        self.col = col + length

    def place_scrolling_text(self, token: LCDToken, wake: bool) -> None:
        row, _, clear = self.find_slot(self.columns, True)
        windows = [
            token.text[index:index + self.columns]
            for index in range(len(token.text) - self.columns + 1)
        ]
        span = max(token.end - token.start, self.min_display_time)
        step = max(self.scroll_step_time, self.min_gap, span / max(1, len(windows) - 1))
        hold = max(step, self.min_display_time)
        time = self.schedule(token.start, clear)
        for index, window in enumerate(windows):
            first = index == 0
            self.events.append(
                LCDEvent(
                    time=time,
                    row=row,
                    col=0,
                    text=window,
                    clear=clear and first,
                    wake=wake and first,
                    scroll=True,
                )
            )
            self.last_text_time = time
            time += hold if first else step
        self.row = row
        self.col = self.columns

    def verify(self) -> None:
        previous = 0.0
        asleep = False
        for event in self.events:
            if event.time < previous:
                raise LayoutError("The LCD events are not in chronological order.")
            previous = event.time
            if event.sleep:
                if asleep:
                    raise LayoutError("The LCD would be put to sleep twice in a row.")
                asleep = True
                continue
            if asleep and not event.wake:
                raise LayoutError("Text would be shown while the LCD backlight is off.")
            if event.wake and not asleep:
                raise LayoutError("The LCD would be woken up while it is not asleep.")
            asleep = False
            fits = (
                event.text
                and 0 <= event.row < self.rows
                and event.col >= 0
                and event.col + len(event.text) <= self.columns
            )
            if not fits:
                raise LayoutError(f"Text '{event.text}' does not fit on a {self.columns}x{self.rows} LCD.")

    @staticmethod
    def finish_screen(screen: Screen, grid: list[list[str]], scrolling: set[int]) -> None:
        screen.rows = ["".join(row) for row in grid]
        screen.scrolling_rows = sorted(scrolling)

    def build_screens(self, end_time: float) -> tuple[list[Screen], Optional[float]]:
        screens: list[Screen] = []
        grid: list[list[str]] = []
        scrolling: set[int] = set()
        initial_sleep_at: Optional[float] = None
        for event in self.events:
            if event.sleep:
                if screens:
                    screens[-1].sleeps_at = event.time
                else:
                    initial_sleep_at = event.time
                continue
            if not screens or event.clear or event.wake:
                if screens:
                    self.finish_screen(screens[-1], grid, scrolling)
                grid = [[" "] * self.columns for _ in range(self.rows)]
                scrolling = set()
                screens.append(
                    Screen(
                        index=len(screens) + 1,
                        start=event.time,
                        end=event.time,
                        rows=[],
                        scrolling_rows=[],
                        wakes=event.wake,
                        sleeps_at=None,
                    )
                )
            for offset, character in enumerate(event.text):
                grid[event.row][event.col + offset] = character
            if event.scroll:
                scrolling.add(event.row)
        self.finish_screen(screens[-1], grid, scrolling)
        for current, following in zip(screens, screens[1:]):
            current.end = current.sleeps_at if current.sleeps_at is not None else following.start
        last = screens[-1]
        last.end = last.sleeps_at if last.sleeps_at is not None else end_time
        return screens, initial_sleep_at

    def generate(self, tokens: list[LCDToken]) -> Playback:
        if not tokens:
            raise LayoutError("There is no text to place on the LCD.")
        self.reset()
        for token in tokens:
            woke = self.sleep_before(token)
            if token.scroll:
                self.place_scrolling_text(token, woke)
            else:
                self.place_text(token, woke)
            self.audio_end = token.end
        self.sleep_after_last()
        self.verify()
        end_time = max(self.events[-1].time, self.audio_end)
        screens, initial_sleep_at = self.build_screens(end_time)
        return Playback(
            events=list(self.events),
            screens=screens,
            end_time=end_time,
            delayed=self.delayed,
            max_delay=self.max_delay,
            initial_sleep_at=initial_sleep_at,
            sleep_gap=self.sleep_gap,
        )


def generate_lcd_sequence(
    tokens: list[LCDToken],
    sleep_gap: Optional[float] = None,
    dark_at_start: Optional[bool] = None,
) -> Playback:
    engine = LCDLayoutEngine(
        columns=LCD_COLUMNS,
        rows=LCD_ROWS,
        min_display_time=MIN_DISPLAY_TIME,
        min_gap=MIN_GAP,
        sleep_gap=LCD_SLEEP_GAP if sleep_gap is None else sleep_gap,
        scroll_step_time=SCROLL_STEP_TIME,
        dark_at_start=LCD_DARK_AT_START if dark_at_start is None else dark_at_start,
    )
    return engine.generate(tokens)


class ArduinoGenerator:
    def __init__(
        self,
        address: int = LCD_ADDRESS,
        columns: int = LCD_COLUMNS,
        rows: int = LCD_ROWS,
        countdown: int = START_COUNTDOWN,
        button_pin: Optional[int] = START_BUTTON_PIN,
        loop_playback: bool = LOOP_PLAYBACK,
        end_hold: float = END_HOLD_TIME,
    ) -> None:
        self.address = address
        self.columns = columns
        self.rows = rows
        self.countdown = countdown
        self.button_pin = button_pin
        self.loop_playback = loop_playback
        self.end_hold = end_hold

    @staticmethod
    def build_symbols(events: list[LCDEvent]) -> dict[str, str]:
        symbols: dict[str, str] = {}
        for event in events:
            if event.text and event.text not in symbols:
                symbols[event.text] = f"T{len(symbols):04d}"
        return symbols

    @staticmethod
    def event_line(event: LCDEvent, symbols: dict[str, str]) -> str:
        names = []
        if event.sleep:
            names.append("FLAG_SLEEP")
        if event.wake:
            names.append("FLAG_WAKE")
        if event.clear:
            names.append("FLAG_CLEAR")
        flags = " | ".join(names) if names else "0"
        text = symbols[event.text] if event.text else "0"
        return f"    {{{to_milliseconds(event.time)}UL, {event.row}, {event.col}, {flags}, {text}}},"

    @staticmethod
    def estimate_data_bytes(events: list[LCDEvent], symbols: dict[str, str]) -> int:
        return len(events) * 9 + sum(len(text) + 1 for text in symbols)

    def status_lines(self, first: str, second: str, indent: str) -> list[str]:
        lines = [f"{indent}lcd.setCursor(0, 0);", f'{indent}lcd.print(F("{first}"));']
        if self.rows >= 2 and second:
            lines += [f"{indent}lcd.setCursor(0, 1);", f'{indent}lcd.print(F("{second}"));']
        return lines

    def wait_for_start_lines(self) -> list[str]:
        if self.button_pin is None and self.countdown <= 0:
            return []
        lines = ["void waitForStart() {", "    lcd.backlight();"]
        if self.button_pin is not None:
            lines.append("    lcd.clear();")
            lines += self.status_lines("Press button", "to start", "    ")
            lines += [
                "    while (digitalRead(START_BUTTON_PIN) == HIGH) {",
                "        delay(10);",
                "    }",
                "    delay(30);",
            ]
        if self.countdown > 0:
            lines.append(f"    for (uint8_t seconds = {self.countdown}; seconds > 0; seconds--) {{")
            lines.append("        lcd.clear();")
            if self.rows >= 2:
                lines += self.status_lines("Starting in", "", "        ")
                lines += ["        lcd.setCursor(0, 1);", "        lcd.print(seconds);"]
            else:
                lines += [
                    "        lcd.setCursor(0, 0);",
                    '        lcd.print(F("Start in "));',
                    "        lcd.print(seconds);",
                ]
            lines.append("        delay(1000);")
            lines.append("    }")
        lines.append("}")
        return lines

    def setup_lines(self, has_wait: bool) -> list[str]:
        lines = ["void setup() {", "    lcd.init();", "    lcd.backlight();"]
        if self.button_pin is not None:
            lines.append("    pinMode(START_BUTTON_PIN, INPUT_PULLUP);")
        if has_wait:
            lines.append("    waitForStart();")
        lines.append("    beginPlayback();")
        lines.append("}")
        return lines

    def loop_lines(self, has_wait: bool) -> list[str]:
        lines = [
            "void loop() {",
            "    if (!playing) {",
            "        return;",
            "    }",
            "    uint32_t elapsed = millis() - playbackStart;",
            "    while (nextEvent < EVENT_COUNT && playEventIfDue(nextEvent, elapsed)) {",
            "        nextEvent++;",
            "    }",
        ]
        if self.loop_playback:
            lines.append("    if (nextEvent >= EVENT_COUNT && elapsed >= END_TIME_MS) {")
            if has_wait:
                lines.append("        waitForStart();")
            lines.append("        beginPlayback();")
            lines.append("    }")
        else:
            lines.append("    if (nextEvent >= EVENT_COUNT) {")
            lines.append("        playing = false;")
            lines.append("    }")
        lines.append("}")
        return lines

    def generate(self, playback: Playback) -> str:
        if len(playback.events) > MAX_EVENTS:
            raise GeneratorError(
                f"The song produces {len(playback.events)} LCD events, more than the "
                f"{MAX_EVENTS} that the generated sketch supports."
            )
        symbols = self.build_symbols(playback.events)
        estimate = self.estimate_data_bytes(playback.events, symbols)
        if estimate > FLASH_WARNING_BYTES:
            print(
                f"Warning: the lyric data needs about {estimate} bytes of flash. An Arduino Uno or Nano "
                "has 32 KB in total, so the sketch may not fit. Consider an ESP32 or a shorter song.",
                file=sys.stderr,
            )
        lines: list[str] = ["#include <Wire.h>", "#include <LiquidCrystal_I2C.h>", ""]
        lines.append(f"LiquidCrystal_I2C lcd(0x{self.address:02X}, {self.columns}, {self.rows});")
        if self.button_pin is not None:
            lines.append(f"const uint8_t START_BUTTON_PIN = {self.button_pin};")
        lines.append("")
        lines.extend(FLAG_LINES)
        lines.append("")
        lines.extend(EVENT_STRUCT_LINES)
        lines.append("")
        for text, name in symbols.items():
            lines.append(f"const char {name}[] PROGMEM = {c_string(text)};")
        lines.append("")
        lines.append("const LcdEvent EVENTS[] PROGMEM = {")
        lines.extend(self.event_line(event, symbols) for event in playback.events)
        lines.append("};")
        lines.append("")
        lines.append("const uint16_t EVENT_COUNT = sizeof(EVENTS) / sizeof(EVENTS[0]);")
        if self.loop_playback:
            end_ms = to_milliseconds(playback.end_time + self.end_hold)
            lines.append(f"const uint32_t END_TIME_MS = {end_ms}UL;")
        lines.append("")
        lines.extend(STATE_LINES)
        lines.append("")
        lines.extend(PLAY_EVENT_LINES)
        lines.append("")
        wait_lines = self.wait_for_start_lines()
        if wait_lines:
            lines.extend(wait_lines)
            lines.append("")
        lines.extend(BEGIN_PLAYBACK_LINES)
        lines.append("")
        lines.extend(self.setup_lines(bool(wait_lines)))
        lines.append("")
        lines.extend(self.loop_lines(bool(wait_lines)))
        code = "\n".join(lines) + "\n"
        ensure_comment_free(code)
        return code


def generate_arduino_code(playback: Playback) -> str:
    generator = ArduinoGenerator(
        address=LCD_ADDRESS,
        columns=LCD_COLUMNS,
        rows=LCD_ROWS,
        countdown=START_COUNTDOWN,
        button_pin=START_BUTTON_PIN,
        loop_playback=LOOP_PLAYBACK,
        end_hold=END_HOLD_TIME,
    )
    return generator.generate(playback)


def save_ino_file(code: str, output: str, sketch_folder: Optional[bool] = None) -> Path:
    use_folder = CREATE_SKETCH_FOLDER if sketch_folder is None else sketch_folder
    path = Path(output).expanduser()
    if path.suffix.lower() != ".ino":
        path = path.with_name(path.name + ".ino")
    if use_folder:
        path = path.parent / path.stem / path.name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(code)
    except OSError as error:
        raise GeneratorError(f"Could not write '{path}': {error}") from error
    return path


def print_transcription(words: list[TimedWord], level: str, sleep_gap: float) -> None:
    print("=== TRANSCRIPTION RESULT ===")
    print()
    print(f"Timestamp source: {level}")
    print()
    previous_end = 0.0
    for word in words:
        notes = f"dur {word.duration:.2f}s, gap {word.gap_before:.2f}s"
        if sleep_gap > 0 and reaches_sleep_gap(previous_end, word.start, sleep_gap):
            notes += ", LCD off during gap"
        print(f"[{word.start:.2f}s - {word.end:.2f}s] {word.text}  ({notes})")
        previous_end = word.end
    print()


def print_layout(playback: Playback, columns: Optional[int] = None) -> None:
    columns = LCD_COLUMNS if columns is None else columns
    border = "+" + "-" * columns + "+"
    print("=== LCD LAYOUT ===")
    print()
    if playback.initial_sleep_at is not None:
        print(f"LCD OFF at {playback.initial_sleep_at:.2f}s (no words at the start of the song)")
        print()
    for screen in playback.screens:
        title = f"Screen {screen.index}  [{screen.start:.2f}s - {screen.end:.2f}s]"
        if screen.wakes:
            title += "  LCD ON"
        print(title)
        print(border)
        for number, row in enumerate(screen.rows):
            suffix = "  scrolling" if number in screen.scrolling_rows else ""
            print(f"|{row}|{suffix}")
        print(border)
        if screen.sleeps_at is not None:
            print(f"LCD OFF at {screen.sleeps_at:.2f}s (no words for {playback.sleep_gap:.2f}s)")
        print()
    if playback.delayed:
        print(
            f"Timing note: {playback.delayed} event(s) were delayed by up to {playback.max_delay:.2f}s "
            "to respect MIN_GAP and MIN_DISPLAY_TIME."
        )
        print()


def print_summary(raw: RawTranscription, words: list[TimedWord], playback: Playback, path: Path) -> None:
    duration = raw.duration if raw.duration > 0 else words[-1].end
    detected = textwrap.shorten(" ".join(word.text for word in words), width=400, placeholder=" ...")
    print("Analysis complete.")
    print()
    print("Audio:")
    print(raw.source)
    print()
    print("Duration:")
    print(format_clock(duration))
    print()
    print("Detected text:")
    print(detected)
    print()
    print("LCD layout:")
    print(f"{len(playback.screens)} screens, {len(playback.events)} events on a {LCD_COLUMNS}x{LCD_ROWS} LCD")
    if playback.sleep_gap > 0:
        print(
            f"Backlight sleeps {playback.sleep_count} time(s), "
            f"after {playback.sleep_gap:.2f}s without words"
        )
    print()
    print("Arduino file generated successfully:")
    print()
    print(path)


def transcribe_from_audio(args: argparse.Namespace, settings: Settings) -> RawTranscription:
    audio = load_audio(args.audio if args.audio else prompt_for_audio())
    transcriber = create_transcriber(settings.engine, settings.language)
    uses_api = settings.engine == "openai"
    print(f"Audio: {audio.name}")
    print("Converting audio with FFmpeg...")
    with tempfile.TemporaryDirectory(prefix="music_to_lcd_") as workdir:
        prepared = convert_audio(
            audio,
            Path(workdir),
            OPENAI_CHUNK_SECONDS if uses_api else None,
            OPENAI_MAX_UPLOAD_BYTES if uses_api else None,
        )
        print(f"Duration: {format_clock(prepared.duration)}")
        raw = transcribe_audio(prepared, transcriber)
    if args.save_transcript:
        save_transcript(raw, Path(args.save_transcript).expanduser())
        print(f"Transcription saved to {args.save_transcript}")
    print()
    return raw


def run(args: argparse.Namespace) -> int:
    load_environment()
    settings = resolve_settings(args)
    if args.load_transcript:
        raw = load_transcript(Path(args.load_transcript).expanduser())
        print(f"Using saved transcription: {args.load_transcript}")
        print()
    else:
        raw = transcribe_from_audio(args, settings)
    entries, level = extract_timestamps(raw)
    words = process_timing(entries, settings.offset)
    tokens = wrap_text_for_lcd(words)
    playback = generate_lcd_sequence(tokens, settings.sleep_gap)
    print_transcription(words, level, settings.sleep_gap)
    print_layout(playback)
    print("Generating Arduino file...")
    print()
    code = generate_arduino_code(playback)
    path = save_ino_file(code, settings.output)
    print_summary(raw, words, playback, path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music_to_lcd.py",
        description="Music to LCD I2C Generator: turn a song into an Arduino sketch that shows "
        "the lyrics on an I2C LCD, following the timing of the song.",
    )
    parser.add_argument("audio", nargs="?", help="path to the audio file (mp3, wav, m4a, flac, ogg, opus, aac, wma)")
    parser.add_argument("-o", "--output", help=f"output .ino file (default: {OUTPUT_FILE})")
    parser.add_argument("--engine", choices=ENGINES, help=f"speech-to-text engine (default: {TRANSCRIPTION_ENGINE})")
    parser.add_argument("--language", help="ISO-639-1 language code of the lyrics, for example en or id")
    parser.add_argument(
        "--offset",
        type=float,
        help="shift every timestamp by this many seconds (positive values delay the text)",
    )
    parser.add_argument(
        "--sleep-gap",
        type=float,
        help=f"turn the LCD backlight off after this many seconds without words (default: {LCD_SLEEP_GAP}, 0 disables)",
    )
    parser.add_argument("--save-transcript", metavar="FILE", help="save the raw transcription as JSON")
    parser.add_argument(
        "--load-transcript",
        metavar="FILE",
        help="use a saved transcription JSON instead of transcribing an audio file",
    )
    parser.add_argument("--debug", action="store_true", help="show the full traceback when an error happens")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    configure_console()
    args = build_parser().parse_args(argv)
    print("Music to LCD I2C Generator")
    print()
    try:
        return run(args)
    except GeneratorError as error:
        print(f"Error: {error}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Unexpected error: {error}", file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        else:
            print("Run again with --debug to see the full traceback.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
