import asyncio
import logging
from collections import deque
from typing import Callable

from src.constants import (
    DEFAULT_SAMPLE_RATE,
    DEFAULT_SAMPLE_WIDTH,
    ENABLE_JITTER_BUFFER,
    JITTER_BUFFER_TARGET_MS,
    JITTER_BUFFER_MAX_FRAMES,
    OUTPUT_BUFFER_TARGET_MS,
    OUTPUT_BUFFER_MAX_FRAMES,
)

logger = logging.getLogger(__name__)

# Размер одного RTP фрейма (20 мс при 8 кГц)
RTP_FRAME_SIZE = DEFAULT_SAMPLE_RATE * DEFAULT_SAMPLE_WIDTH // 50  # 320 байт


class JitterBuffer:
    """
    Jitter-buffer для входящего аудио потока (Asterisk → OpenAI).
    
    Сглаживает сетевой джиттер, накапливая небольшое количество фреймов
    и отдавая их в ровном ритме каждые 20 мс.
    """

    def __init__(
        self,
        output_callback: Callable[[bytes], None],
        target_ms: int = JITTER_BUFFER_TARGET_MS,
    ):
        self.output_callback = output_callback
        self.target_ms = target_ms
        self.buffer: deque[tuple[float, bytes]] = deque()
        self.running = False
        self.output_task: asyncio.Task | None = None
        self.frame_interval = 0.020  # 20 мс между фреймами
        self.last_output_time: float | None = None
        self.total_frames_received = 0
        self.total_frames_output = 0

        if ENABLE_JITTER_BUFFER:
            logger.info(
                "JitterBuffer создан: target_ms=%d, frame_interval=%.3f",
                self.target_ms,
                self.frame_interval,
            )

    def add_frame(self, pcm_data: bytes, timestamp: float | None = None) -> None:
        """
        Добавляет фрейм в буфер.
        
        :param pcm_data: PCM16 данные (320 байт = 20 мс при 8 кГц)
        :param timestamp: Временная метка (опционально)
        """
        if not ENABLE_JITTER_BUFFER:
            # Если буфер отключен, сразу передаём дальше
            self.output_callback(pcm_data)
            return

        if timestamp is None:
            timestamp = asyncio.get_event_loop().time()

        self.buffer.append((timestamp, pcm_data))
        if len(self.buffer) > JITTER_BUFFER_MAX_FRAMES:
            dropped = self.buffer.popleft()
            logger.warning(
                "JitterBuffer: переполнение, удалён старый фрейм "
                "(buffer_size=%d, max=%d, dropped_ts=%.6f)",
                len(self.buffer),
                JITTER_BUFFER_MAX_FRAMES,
                dropped[0],
            )
        self.total_frames_received += 1

        # Запускаем output loop при первом фрейме
        if not self.running:
            self.running = True
            self.output_task = asyncio.create_task(self._output_loop())
            logger.debug(
                "JitterBuffer: запущен output loop, первый фрейм добавлен "
                "(buffer_size=%d)",
                len(self.buffer),
            )

        # Логируем периодически
        if self.total_frames_received % 100 == 0:
            logger.debug(
                "JitterBuffer: добавлен фрейм (total_received=%d, buffer_size=%d, "
                "target_ms=%d)",
                self.total_frames_received,
                len(self.buffer),
                self.target_ms,
            )

    async def _output_loop(self) -> None:
        """
        Асинхронный цикл, который каждые 20 мс отдаёт фрейм из буфера.
        """
        try:
            while self.running or len(self.buffer) > 0:
                now = asyncio.get_event_loop().time()

                # Вычисляем целевой размер буфера в фреймах
                target_frames = max(1, int(self.target_ms / 20.0))

                if len(self.buffer) >= target_frames:
                    # Достаточно фреймов - отдаём один
                    _, pcm_data = self.buffer.popleft()
                    self.output_callback(pcm_data)
                    self.total_frames_output += 1
                    self.last_output_time = now

                    # Логируем периодически
                    if self.total_frames_output % 500 == 0:
                        logger.debug(
                            "JitterBuffer: отдан фрейм (total_output=%d, "
                            "buffer_size=%d, target_frames=%d)",
                            self.total_frames_output,
                            len(self.buffer),
                            target_frames,
                        )
                elif len(self.buffer) > 0:
                    # Есть фреймы, но меньше целевого - отдаём с небольшой задержкой
                    await asyncio.sleep(self.frame_interval * 0.5)
                    continue
                else:
                    # Буфер пуст - ждём следующий фрейм
                    await asyncio.sleep(self.frame_interval)

                # Соблюдаем интервал между фреймами
                if self.last_output_time is not None:
                    elapsed = now - self.last_output_time
                    if elapsed < self.frame_interval:
                        await asyncio.sleep(self.frame_interval - elapsed)

        except asyncio.CancelledError:
            logger.debug("JitterBuffer: output loop отменён")
        except Exception as e:
            logger.error(
                "JitterBuffer: ошибка в output loop: %s",
                e,
                exc_info=True,
            )

    def stop(self) -> None:
        """Останавливает jitter-buffer."""
        self.running = False
        if self.output_task:
            try:
                awaitable = self.flush()
                if asyncio.iscoroutine(awaitable):
                    try:
                        asyncio.get_event_loop().create_task(awaitable)
                    except RuntimeError:
                        pass
            except Exception:
                logger.debug("JitterBuffer: flush при stop завершился с ошибкой")

            self.output_task.cancel()
            logger.debug("JitterBuffer: остановлен")

    async def flush(self) -> None:
        """Очищает буфер, отдавая все оставшиеся фреймы."""
        if not ENABLE_JITTER_BUFFER:
            return

        while len(self.buffer) > 0:
            _, pcm_data = self.buffer.popleft()
            self.output_callback(pcm_data)
            self.total_frames_output += 1
            await asyncio.sleep(self.frame_interval)

        logger.debug(
            "JitterBuffer: буфер очищен (total_output=%d)",
            self.total_frames_output,
        )


class OutputBuffer:
    """
    Буфер для исходящего аудио потока (OpenAI → Asterisk).
    
    Сглаживает возможные неровности в поступлении PCM16 от OpenAI,
    обеспечивая ровную отправку RTP пакетов в Asterisk.
    """

    def __init__(
        self,
        output_callback: Callable[[bytes], None],
        target_ms: int = OUTPUT_BUFFER_TARGET_MS,
    ):
        self.output_callback = output_callback
        self.target_ms = target_ms
        self.buffer: deque[bytes] = deque()
        self.running = False
        self.output_task: asyncio.Task | None = None
        self.frame_interval = 0.020  # 20 мс между фреймами
        self.last_output_time: float | None = None
        self.total_chunks_received = 0
        self.total_frames_output = 0

        if ENABLE_JITTER_BUFFER:
            logger.info(
                "OutputBuffer создан: target_ms=%d, frame_interval=%.3f",
                self.target_ms,
                self.frame_interval,
            )

    def add_chunk(self, pcm_data: bytes) -> None:
        """
        Добавляет PCM чанк в буфер.
        
        :param pcm_data: PCM16 данные (может быть любого размера)
        """
        if not ENABLE_JITTER_BUFFER:
            # Если буфер отключен, сразу передаём дальше
            self.output_callback(pcm_data)
            return

        self.buffer.append(pcm_data)
        if len(self.buffer) > OUTPUT_BUFFER_MAX_FRAMES:
            dropped = self.buffer.popleft()
            logger.warning(
                "OutputBuffer: переполнение, удалён старый чанк "
                "(buffer_size=%d, max=%d, dropped_len=%d)",
                len(self.buffer),
                OUTPUT_BUFFER_MAX_FRAMES,
                len(dropped),
            )
        self.total_chunks_received += 1

        # Запускаем output loop при первом чанке
        if not self.running:
            self.running = True
            self.output_task = asyncio.create_task(self._output_loop())
            logger.debug(
                "OutputBuffer: запущен output loop, первый чанк добавлен "
                "(buffer_size=%d)",
                len(self.buffer),
            )

    async def _output_loop(self) -> None:
        """
        Асинхронный цикл, который каждые 20 мс отдаёт фрейм из буфера.
        """
        try:
            pending_pcm = bytearray()
            frame_size = RTP_FRAME_SIZE

            while self.running or len(self.buffer) > 0 or len(pending_pcm) > 0:
                now = asyncio.get_event_loop().time()

                # Собираем достаточно данных для одного RTP фрейма
                while len(pending_pcm) < frame_size and len(self.buffer) > 0:
                    chunk = self.buffer.popleft()
                    pending_pcm.extend(chunk)

                if len(pending_pcm) >= frame_size:
                    # Достаточно данных - отдаём один фрейм
                    frame = bytes(pending_pcm[:frame_size])
                    del pending_pcm[:frame_size]
                    self.output_callback(frame)
                    self.total_frames_output += 1
                    self.last_output_time = now

                    # Логируем периодически
                    if self.total_frames_output % 500 == 0:
                        logger.debug(
                            "OutputBuffer: отдан фрейм (total_output=%d, "
                            "buffer_size=%d, pending=%d байт)",
                            self.total_frames_output,
                            len(self.buffer),
                            len(pending_pcm),
                        )
                else:
                    # Недостаточно данных - ждём
                    await asyncio.sleep(self.frame_interval)

                # Соблюдаем интервал между фреймами
                if self.last_output_time is not None:
                    elapsed = asyncio.get_event_loop().time() - self.last_output_time
                    if elapsed < self.frame_interval:
                        await asyncio.sleep(self.frame_interval - elapsed)

        except asyncio.CancelledError:
            logger.debug("OutputBuffer: output loop отменён")
        except Exception as e:
            logger.error(
                "OutputBuffer: ошибка в output loop: %s",
                e,
                exc_info=True,
            )

    def stop(self) -> None:
        """Останавливает output-buffer."""
        self.running = False
        if self.output_task:
            try:
                awaitable = self.flush()
                if asyncio.iscoroutine(awaitable):
                    try:
                        asyncio.get_event_loop().create_task(awaitable)
                    except RuntimeError:
                        pass
            except Exception:
                logger.debug("OutputBuffer: flush при stop завершился с ошибкой")

            self.output_task.cancel()
            logger.debug("OutputBuffer: остановлен")

    async def flush(self) -> None:
        """Очищает буфер, отдавая все оставшиеся данные."""
        if not ENABLE_JITTER_BUFFER:
            return

        pending_pcm = bytearray()
        frame_size = RTP_FRAME_SIZE

        # Собираем все данные из буфера
        while len(self.buffer) > 0:
            chunk = self.buffer.popleft()
            pending_pcm.extend(chunk)

        # Отдаём оставшиеся фреймы
        while len(pending_pcm) >= frame_size:
            frame = bytes(pending_pcm[:frame_size])
            del pending_pcm[:frame_size]
            self.output_callback(frame)
            self.total_frames_output += 1
            await asyncio.sleep(self.frame_interval)

        logger.debug(
            "OutputBuffer: буфер очищен (total_output=%d, остаток=%d байт)",
            self.total_frames_output,
            len(pending_pcm),
        )

