import os
from pathlib import Path


HOST = "0.0.0.0"
PORT = 7575

# Телефония (Asterisk ↔ externalMedia)
DEFAULT_SAMPLE_RATE = 8000
DEFAULT_SAMPLE_WIDTH = 2

# OpenAI Realtime session parameters
# Оптимизация: используем 24 кГц для входа (нативный формат Realtime)
# вместо 16 кГц для лучшего качества и меньших потерь при ресемплинге
OPENAI_INPUT_RATE = 24000
OPENAI_OUTPUT_RATE = 24000
REALTIME_INPUT_FORMAT = "pcm16"
REALTIME_OUTPUT_FORMAT = "pcm16"
REALTIME_MODALITIES = ["text", "audio"]
REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "alloy")

DRAIN_CHUNK_SIZE = 960  # ~20 мс PCM при 24 kHz перед отдачей в Asterisk
MIN_OPENAI_INPUT_CHUNK = 1440  # ~30 мс PCM16 @ 24 kHz перед отправкой в OpenAI
READER_HEADER_SIZE = 3
READER_PAYLOAD_SIZE = 160
READER_BYTES_LIMIT = READER_HEADER_SIZE + READER_PAYLOAD_SIZE

# Ограничители буферов, чтобы избежать неограниченного роста
JITTER_BUFFER_MAX_FRAMES = int(
    os.getenv("JITTER_BUFFER_MAX_FRAMES", str(int(200)))
)
OUTPUT_BUFFER_MAX_FRAMES = int(
    os.getenv("OUTPUT_BUFFER_MAX_FRAMES", str(int(200)))
)

INPUT_FORMAT = "g711_alaw"
OUTPUT_FORMAT = "pcm16"

DEFAULT_LANG = "ru"

REALTIME_MODEL = os.getenv(
    "OPENAI_REALTIME_MODEL",
    "gpt-4o-mini-realtime-preview",
)
REALTIME_URL = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"

# VAD параметры (улучшены для более отзывчивого поведения)
VAD_SILENCE_MS = int(os.getenv("AUDIO_VAD_SILENCE_MS", "550"))  # Уменьшено с 900 для быстрого ответа
VAD_RMS_THRESHOLD = float(os.getenv("AUDIO_VAD_RMS_THRESHOLD", "0.08"))

# Jitter-buffer параметры
ENABLE_JITTER_BUFFER = os.getenv("ENABLE_JITTER_BUFFER", "true").lower() == "true"
JITTER_BUFFER_TARGET_MS = int(os.getenv("JITTER_BUFFER_TARGET_MS", "40"))
OUTPUT_BUFFER_TARGET_MS = int(os.getenv("OUTPUT_BUFFER_TARGET_MS", "40"))

# Barge-in параметры
ENABLE_LOCAL_BARGE_IN = os.getenv("ENABLE_LOCAL_BARGE_IN", "true").lower() == "true"
BARGE_IN_FRAMES_THRESHOLD = int(os.getenv("BARGE_IN_FRAMES_THRESHOLD", "2"))  # Количество фреймов подряд для детекции


def get_openai_api_key() -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY не установлен в переменных окружения")
    return api_key


def load_instructions() -> str:
    instructions_path = Path(__file__).parent.parent / "instructions.md"
    try:
        with open(instructions_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return (
            "Ты — вежливый голосовой ассистент. "
            "Общайся с собеседником естественно, по-деловому и коротко. "
            "Отвечай на русском языке."
        )

