import asyncio
import logging
import struct
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from aiohttp import web
from src.codecs import alaw_to_pcm16, pcm16_to_alaw
from src.constants import (
    DEFAULT_SAMPLE_WIDTH,
    ENABLE_JITTER_BUFFER,
    HOST,
    PORT,
)
from src.audio_websocket_client import AudioWebSocketClient
from src.audio_handler import AudioHandler
from src.jitter_buffer import JitterBuffer

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Отключаем DEBUG-логирование websockets, чтобы не логировать API ключ
logging.getLogger("websockets.client").setLevel(logging.WARNING)


def make_send_pcm_callback(
    write_queue: asyncio.Queue[bytes],
) -> Callable[[bytes], None]:
    """
    Создаёт синхронный callback для отправки PCM данных.
    Данные помещаются в очередь, из которой их читает write_loop.
    """

    def send_pcm(data: bytes) -> None:
        try:
            write_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning(
                "Очередь записи переполнена, PCM данные пропущены (размер=%d байт)",
                len(data),
            )

    return send_pcm


async def write_loop(
    transport: asyncio.DatagramTransport,
    remote_addr: tuple[str, int],
    write_queue: asyncio.Queue[bytes],
    session: "UdpSession",
) -> None:
    """
    Асинхронный цикл записи PCM данных в UDP-соединение, упаковывая их в RTP пакеты.
    """
    # Время последней отправки пакета (для соблюдения темпа RTP)
    last_send_time: float | None = None
    RTP_PACKET_INTERVAL = 0.020  # 20 мс между пакетами (для G.711 A-law)
    
    try:
        while True:
            try:
                data = await asyncio.wait_for(write_queue.get(), timeout=1.0)
                
                # Если inbound_pt/ssrc ещё не известны, используем дефолты
                # Для G.711 A-law стандартный payload type = 8
                pt = session.inbound_pt if session.inbound_pt is not None else 8
                ssrc = session.ssrc if session.ssrc is not None else 0x12345678
                
                # Проверка: для A-law ожидается pt=8
                if pt != 8:
                    logger.warning(
                        "Исходящий RTP использует pt=%s вместо ожидаемого 8 "
                        "(G.711 A-law). Возможны проблемы с аудио! "
                        "(session_uuid=%s)",
                        pt,
                        session.session_uuid,
                    )
                
                # Инкремент sequence number
                session.seq_out = (session.seq_out + 1) & 0xFFFF
                
                if len(data) == 0 or len(data) % DEFAULT_SAMPLE_WIDTH != 0:
                    logger.warning(
                        "Неверный размер PCM чанка, пропущено (len=%d, "
                        "session_uuid=%s)",
                        len(data),
                        session.session_uuid,
                    )
                    continue

                samples = len(data) // DEFAULT_SAMPLE_WIDTH
                session.ts_out = (session.ts_out + samples) & 0xFFFFFFFF
                
                # Логируем timestamp только очень редко (для отладки)
                # if session.seq_out % 1000 == 0:
                #     logger.debug(
                #         "RTP timestamp: seq=%s, ts=%s, samples=%s (session_uuid=%s)",
                #         session.seq_out,
                #         session.ts_out,
                #         samples,
                #         session.session_uuid,
                #     )
                
                # Конвертируем PCM16 → G.711 A-law
                try:
                    payload = pcm16_to_alaw(data)
                except ValueError as exc:
                    logger.error(
                        "Ошибка кодирования PCM16→A-law (session_uuid=%s): %s",
                        session.session_uuid,
                        exc,
                    )
                    continue

                # Проверка размера payload: для G.711 A-law ожидается 160 байт (20 мс)
                expected_payload_size = len(data) // 2  # PCM16 → A-law: 2:1
                if len(payload) != expected_payload_size:
                    logger.warning(
                        "[RTP] Неожиданный размер payload после кодирования "
                        "(session_uuid=%s, PCM_len=%d, payload_len=%d, expected=%d)",
                        session.session_uuid,
                        len(data),
                        len(payload),
                        expected_payload_size,
                    )

                # Проверка: для G.711 A-law стандартный размер = 160 байт (20 мс)
                if len(payload) != 160:
                    logger.warning(
                        "[RTP] НЕСТАНДАРТНЫЙ размер payload! "
                        "(session_uuid=%s, payload_len=%d байт, ожидается 160 байт) "
                        "Это может вызывать скрипы/искажения!",
                        session.session_uuid,
                        len(payload),
                    )

                # Сборка RTP заголовка (12 байт)
                # v=2, p=0, x=0, cc=0 → первый байт = 0x80
                # m=0, pt → второй байт = pt & 0x7F
                header = struct.pack(
                    "!BBHII",
                    0x80,  # V=2, P=0, X=0, CC=0
                    pt & 0x7F,  # без marker
                    session.seq_out,  # sequence number
                    session.ts_out,  # timestamp
                    ssrc,  # SSRC
                )
                
                # Объединение заголовка и PCM данных
                packet = header + payload
                
                # Задержка для соблюдения темпа RTP: 20 мс между пакетами
                # (для G.711 A-law: 160 байт = 20 мс при 8 кГц)
                # Это предотвращает отправку пакетов пачками и устраняет скрипы
                if last_send_time is not None:
                    elapsed = asyncio.get_running_loop().time() - last_send_time
                    if elapsed < RTP_PACKET_INTERVAL:
                        await asyncio.sleep(RTP_PACKET_INTERVAL - elapsed)
                
                # Отправка через transport
                transport.sendto(packet, remote_addr)
                last_send_time = asyncio.get_running_loop().time()
                
                # Логируем только при проблемах или очень редко
                if len(payload) != 160:
                    logger.warning(
                        "[RTP] Отправлен RTP с нестандартным размером: "
                        "addr=%s, pt=%s, seq=%s, payload_len=%d байт (ожидается 160) "
                        "(session_uuid=%s)",
                        remote_addr,
                        pt,
                        session.seq_out,
                        len(payload),
                        session.session_uuid,
                    )
                elif session.seq_out % 500 == 0:
                    logger.debug(
                        "[RTP] Отправлен RTP в Asterisk: addr=%s, pt=%s, seq=%s, ts=%s, "
                        "payload_len=%d байт (session_uuid=%s)",
                        remote_addr,
                        pt,
                        session.seq_out,
                        session.ts_out,
                        len(payload),
                        session.session_uuid,
                    )
                
            except asyncio.TimeoutError:
                # Проверяем, не закрыт ли transport
                if transport.is_closing():
                    break
                continue
    except Exception as e:
        logger.error(
            "Ошибка в write_loop: %s",
            e,
            exc_info=True,
        )


class UdpSession:
    def __init__(
        self,
        addr: tuple[str, int],
        transport: asyncio.DatagramTransport,
        session_uuid: Optional[str] = None,
        protocol: Optional["AudioSocketUdpProtocol"] = None,
    ):
        self.addr = addr
        self.transport = transport
        self.session_uuid = session_uuid if session_uuid else str(uuid.uuid4())
        self.remote_addr = addr
        self.protocol = protocol  # Ссылка на protocol для удаления мэппинга

        # RTP параметры
        self.inbound_pt: int | None = None
        self.ssrc: int | None = None
        self.seq_out: int = 0
        self.ts_out: int = 0

        # 📝 Логирование разговора в файл
        self._log_file_path: Optional[Path] = None
        self._log_file: Optional[object] = None
        self._setup_conversation_log()

        logger.info(
            "Создана новая UDP-сессия для %s, uuid=%s, log_file=%s",
            addr,
            self.session_uuid,
            self._log_file_path,
        )

    def _setup_conversation_log(self) -> None:
        """Создаёт файл для логирования разговора."""
        try:
            logs_dir = Path("/tmp/conversation_logs")
            logs_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"call_{timestamp}_{self.session_uuid[:8]}.txt"
            self._log_file_path = logs_dir / filename

            self._log_file = open(self._log_file_path, "w", encoding="utf-8")
            self._log_write(f"=== НАЧАЛО ЗВОНКА ===\n")
            self._log_write(f"Session UUID: {self.session_uuid}\n")
            self._log_write(f"Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            self._log_write(f"Remote: {self.addr}\n")
            self._log_write(f"{'='*40}\n\n")
        except Exception as e:
            logger.warning(
                "Не удалось создать файл лога разговора: %s",
                e,
            )

    def _log_write(self, text: str) -> None:
        """Пишет текст в файл лога разговора."""
        if self._log_file and not self._log_file.closed:
            self._log_file.write(text)
            self._log_file.flush()

    def log_user_transcript(self, text: str) -> None:
        """Логирует текст пользователя."""
        if text:
            timestamp = datetime.now().strftime('%H:%M:%S')
            self._log_write(f"[{timestamp}] 👤 ПОЛЬЗОВАТЕЛЬ: {text}\n")

    def log_bot_transcript(self, text: str) -> None:
        """Логирует текст бота."""
        if text:
            timestamp = datetime.now().strftime('%H:%M:%S')
            self._log_write(f"[{timestamp}] 🤖 БОТ: {text}\n")

    def _close_conversation_log(self) -> None:
        """Закрывает файл лога разговора."""
        if self._log_file and not self._log_file.closed:
            self._log_write(f"\n{'='*40}\n")
            self._log_write(f"=== КОНЕЦ ЗВОНКА ===\n")
            self._log_write(f"Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            self._log_file.close()
            logger.info(
                "Файл лога разговора сохранён: %s",
                self._log_file_path,
            )

        # Очередь для записи PCM данных обратно в Asterisk
        write_queue: asyncio.Queue[bytes] = asyncio.Queue()

        # Создаём callback для отправки PCM
        send_pcm_callback = make_send_pcm_callback(write_queue)

        # Создаём AudioHandler и AudioWebSocketClient
        self.audio_handler = AudioHandler(
            session_uuid=self.session_uuid,
            send_pcm_callback=send_pcm_callback,
        )
        self.client = AudioWebSocketClient(
            session_uuid=self.session_uuid,
            audio_handler=self.audio_handler,
            user_transcript_callback=self.log_user_transcript,
            bot_transcript_callback=self.log_bot_transcript,
        )

        # Создаём jitter-buffer для входящего потока (Asterisk → OpenAI)
        # Если буфер отключен, callback будет напрямую вызывать client.push_pcm
        jitter_callback = self.client.push_pcm
        self.jitter_buffer = JitterBuffer(
            output_callback=jitter_callback,
        ) if ENABLE_JITTER_BUFFER else None

        # 🔧 Отправляем инициализирующий RTP пакет для запуска потока
        # 160 байт = 20ms silence для alaw (8kHz, 8bit)
        silence_packet = bytes(160)  # Все нули = silence в G.711 A-law
        transport.sendto(silence_packet, addr)
        logger.info(
            "[INIT] Отправлен инициализирующий RTP silence-пакет для %s (160 байт)",
            addr,
        )

        # Запускаем задачи
        self.client_task = asyncio.create_task(self.client.run())
        self.write_task = asyncio.create_task(
            write_loop(self.transport, self.remote_addr, write_queue, self)
        )
    
    async def handle_incoming_payload(self, payload: bytes, pt: int) -> None:
        """
        Обрабатывает входящие RTP payload от Asterisk (после парсинга RTP).
        """
        try:
            if pt == 8:
                pcm_payload = alaw_to_pcm16(payload)
                codec = "G.711 A-law"
            else:
                pcm_payload = payload
                codec = "linear PCM"

            # Логируем только периодически
            # (убрано избыточное логирование каждого пакета)

            if len(pcm_payload) % DEFAULT_SAMPLE_WIDTH != 0:
                logger.warning(
                    "PCM после декодирования имеет некорректную длину (%d байт), "
                    "session_uuid=%s",
                    len(pcm_payload),
                    self.session_uuid,
                )
                return

            # Передаём PCM16 8 kHz в jitter-buffer (если включен) или напрямую в клиент
            # Jitter-buffer сглаживает сетевой джиттер перед ресемплингом до 24 кГц
            if self.jitter_buffer:
                self.jitter_buffer.add_frame(pcm_payload)
            else:
                self.client.push_pcm(pcm_payload)
            
        except Exception as e:
            logger.error(
                "Ошибка обработки PCM от %s (session_uuid=%s): %s",
                self.addr,
                self.session_uuid,
                e,
                exc_info=True,
            )
    
    async def cleanup(self) -> None:
        logger.info(
            "[CLEANUP] Завершение сессии (session_uuid=%s, addr=%s)",
            self.session_uuid,
            self.addr,
        )

        # 🔧 Закрываем файл лога разговора
        self._close_conversation_log()

        # Удаляем мэппинг из protocol (если есть)
        if self.protocol and self.addr in self.protocol.uuid_mapping:
            del self.protocol.uuid_mapping[self.addr]
            logger.debug(
                "[CLEANUP] Удалён мэппинг для %s из uuid_mapping",
                self.addr,
            )
        
        # Останавливаем клиент
        self.client_task.cancel()
        try:
            await self.client_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(
                "Ошибка при завершении client_task (session_uuid=%s): %s",
                self.session_uuid,
                e,
            )
        
        # Останавливаем jitter-buffer
        if self.jitter_buffer:
            self.jitter_buffer.stop()
            try:
                await self.jitter_buffer.flush()
            except Exception as e:
                logger.error(
                    "Ошибка при очистке jitter-buffer (session_uuid=%s): %s",
                    self.session_uuid,
                    e,
                )
        
        # Останавливаем audio_handler
        await self.audio_handler.cleanup()
        
        # Останавливаем write_loop
        self.write_task.cancel()
        try:
            await self.write_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(
                "Ошибка при завершении write_task (session_uuid=%s): %s",
                self.session_uuid,
                e,
            )
        
        logger.info(
            "[CLEANUP] Сессия завершена (session_uuid=%s)",
            self.session_uuid,
        )


class AudioSocketUdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.transport = None
        self.sessions: dict[tuple[str, int], UdpSession] = {}
        # 🔧 Мэппинг {(ip, port): session_uuid} для предсозданных сессий
        self.uuid_mapping: dict[tuple[str, int], str] = {}

    def connection_made(self, transport):
        self.transport = transport
        sockname = transport.get_extra_info("sockname")
        logger.info(
            "UDP AudioSocket сервер (RTP/G.711 A-law) запущен, локальный адрес: %s",
            sockname,
        )

    def register_session_uuid(self, ip: str, port: int, session_uuid: str) -> bool:
        """
        Регистрирует session_uuid и СОЗДАЁТ сессию немедленно.

        Это разрывает deadlock - audiosocket отправит первый RTP пакет,
        что заставит Asterisk начать отправку RTP в ответ.
        """
        addr = (ip, port)

        # Проверяем нет ли уже сессии
        if addr in self.sessions:
            logger.warning(
                "[REGISTER] Сессия для %s:%d уже существует, обновляем UUID",
                ip, port
            )
            # Обновляем UUID существующей сессии
            self.sessions[addr].session_uuid = session_uuid
            return True

        logger.info(
            "[REGISTER] Создание UdpSession для %s:%d с session_uuid=%s",
            ip, port, session_uuid
        )

        # 🔧 СОЗДАЁМ сессию НЕМЕДЛЕННО и отправляем silence-пакет
        session = UdpSession(addr, self.transport, session_uuid=session_uuid, protocol=self)
        self.sessions[addr] = session
        self.uuid_mapping[addr] = session_uuid

        return True
    
    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            ip, port = addr
            
            # Фильтрация self-test пакетов
            if ip == "127.0.0.1" and data.startswith(b"TEST-UDP-SELF"):
                logger.debug(
                    "UDP self-test пакет от %s:%d, игнорируем",
                    ip,
                    port,
                )
                return
            
            logger.info(
                "Получен UDP пакет: %d байт от %s:%d",
                len(data),
                ip,
                port,
            )
            
            # Минимальная проверка RTP заголовка
            if len(data) < 12:
                logger.warning(
                    "UDP пакет слишком короткий: %d байт (минимум 12 для RTP)",
                    len(data),
                )
                return
            
            # Парсинг RTP заголовка
            first_byte = data[0]
            version = (first_byte >> 6) & 0x03
            
            if version != 2:
                logger.warning(
                    "Неверная версия RTP: %d (ожидается 2), игнорируем пакет",
                    version,
                )
                return
            
            # Второй байт: payload type
            second_byte = data[1]
            pt = second_byte & 0x7F
            
            # Читаем seq, ts, ssrc
            seq = struct.unpack("!H", data[2:4])[0]
            ts = struct.unpack("!I", data[4:8])[0]
            ssrc = struct.unpack("!I", data[8:12])[0]
            
            # Извлекаем payload (после 12-байтного заголовка)
            header_len = 12
            payload = data[header_len:]
            
            # Получаем или создаём сессию
            session = self.sessions.get(addr)
            if session is None:
                # 🔧 Проверяем есть ли зарегистрированный UUID для этого адреса
                session_uuid = self.uuid_mapping.get(addr)

                if session_uuid:
                    logger.info(
                        "[SESSION] Создание новой UDP-сессии для %s с ПРЕДЗАДАННЫМ uuid=%s "
                        "(всего активных сессий: %d)",
                        addr,
                        session_uuid,
                        len(self.sessions),
                    )
                else:
                    logger.info(
                        "[SESSION] Создание новой UDP-сессии для адреса %s "
                        "(всего активных сессий: %d)",
                        addr,
                        len(self.sessions),
                    )

                session = UdpSession(addr, self.transport, session_uuid=session_uuid, protocol=self)
                self.sessions[addr] = session
            else:
                logger.debug(
                    "[SESSION] Использование существующей сессии для %s "
                    "(session_uuid=%s)",
                    addr,
                    session.session_uuid,
                )
            
            # Если у сессии ещё не было inbound_pt/ssrc, сохраняем
            if session.inbound_pt is None or session.ssrc is None:
                session.inbound_pt = pt
                session.ssrc = ssrc
                session.seq_out = seq
                session.ts_out = ts
                logger.info(
                    "Инициализированы RTP параметры для %s: pt=%s, ssrc=%s, seq=%s, ts=%s",
                    addr,
                    pt,
                    ssrc,
                    seq,
                    ts,
                )
            
            # Логируем только периодически или при проблемах
            if seq % 500 == 0:
                logger.debug(
                    "Получен RTP от Asterisk: addr=%s, pt=%s, seq=%s, ts=%s, payload_len=%s",
                    addr,
                    pt,
                    seq,
                    ts,
                    len(payload),
                )
            
            # Передаём payload в сессию
            self.loop.create_task(session.handle_incoming_payload(payload, pt))
            
        except Exception as e:
            logger.exception(
                "Ошибка в datagram_received при обработке пакета от %s", addr
            )
    
    def connection_lost(self, exc):
        if exc:
            logger.error("UDP соединение потеряно: %s", exc)
        else:
            logger.info("UDP соединение закрыто")


async def udp_self_test():
    """
    Отправляет тестовый UDP пакет для проверки работы сервера.
    """
    await asyncio.sleep(1.0)
    logger.info("UDP self-test: отправка тестового пакета на 127.0.0.1:7575")
    try:
        loop = asyncio.get_event_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: asyncio.DatagramProtocol(),
            remote_addr=("127.0.0.1", 7575),
        )
        transport.sendto(b"TEST-UDP-SELF", ("127.0.0.1", 7575))
        await asyncio.sleep(0.1)
        transport.close()
        logger.info("UDP self-test: тестовый пакет отправлен")
    except Exception as e:
        logger.error("UDP self-test: ошибка отправки тестового пакета: %s", e)


async def main() -> None:
    """
    Запускает UDP AudioSocket сервер для RTP (G.711 A-law).
    """
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: AudioSocketUdpProtocol(loop),
        local_addr=(HOST, PORT),
    )

    # 🔧 Создаём HTTP API для регистрации session_uuid
    app = web.Application()

    async def register_uuid(request: web.Request) -> web.Response:
        """HTTP API endpoint для регистрации session_uuid."""
        try:
            data = await request.json()
            ip = data.get("ip")
            port = data.get("port")
            session_uuid = data.get("session_uuid")

            if not all([ip, port, session_uuid]):
                return web.json_response(
                    {"error": "Missing required fields: ip, port, session_uuid"},
                    status=400,
                )

            port = int(port)
            protocol.register_session_uuid(ip, port, session_uuid)

            return web.json_response(
                {
                    "status": "registered",
                    "ip": ip,
                    "port": port,
                    "session_uuid": session_uuid,
                }
            )
        except Exception as e:
            logger.error("Ошибка в register_uuid: %s", e, exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def unregister_uuid(request: web.Request) -> web.Response:
        """HTTP API endpoint для отзыва session_uuid и очистки RTP порта."""
        try:
            data = await request.json()
            session_uuid = data.get("session_uuid")

            if not session_uuid:
                return web.json_response(
                    {"error": "Missing required field: session_uuid"},
                    status=400,
                )

            # Находим и удаляем сессию по session_uuid
            removed_count = 0
            sessions_to_remove = []

            for addr, sess in protocol.sessions.items():
                if sess.session_uuid == session_uuid:
                    sessions_to_remove.append(addr)
                    # Очищаем сессию
                    try:
                        await sess.cleanup()
                        removed_count += 1
                        logger.info(
                            "[UNREGISTER] Удалена сессия session_uuid=%s, addr=%s",
                            session_uuid,
                            addr,
                        )
                    except Exception as e:
                        logger.error(
                            "[UNREGISTER] Ошибка cleanup сессии %s: %s",
                            session_uuid,
                            e,
                        )

            # Удаляем из словаря
            for addr in sessions_to_remove:
                protocol.sessions.pop(addr, None)
                protocol.uuid_mapping.pop(addr, None)

            if removed_count == 0:
                return web.json_response(
                    {"error": f"Session not found: {session_uuid}"},
                    status=404,
                )

            return web.json_response(
                {
                    "status": "unregistered",
                    "session_uuid": session_uuid,
                    "removed_count": removed_count,
                }
            )
        except Exception as e:
            logger.error("Ошибка в unregister_uuid: %s", e, exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    app.router.add_post("/register", register_uuid)
    app.router.add_post("/unregister", unregister_uuid)

    # Запускаем HTTP API на порту 8888
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8888)
    await site.start()
    logger.info("HTTP API сервер запущен на http://0.0.0.0:8888")

    try:
        logger.info(
            "Запуск AudioSocket UDP сервера (RTP/G.711 A-law) на %s:%s",
            HOST,
            PORT,
        )
        # Опциональный self-test (можно закомментировать)
        # loop.create_task(udp_self_test())
        await asyncio.Future()
    except KeyboardInterrupt:
        logger.info("Остановка сервера...")
    finally:
        for session in protocol.sessions.values():
            await session.cleanup()
        transport.close()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановка сервера...")
