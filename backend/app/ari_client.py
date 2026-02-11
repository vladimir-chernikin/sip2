import asyncio
import json
import logging
import os
import base64
import uuid
import httpx
import websockets

from app.settings import settings

logger = logging.getLogger(__name__)


class AriClient:
    def __init__(
        self,
        base_url: str,
        user: str,
        password: str,
        app_name: str,
    ):
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.password = password
        self.app_name = app_name
        self.auth = httpx.BasicAuth(user, password)
        timeout_env = os.getenv("ARI_HTTP_TIMEOUT", "10")
        try:
            self.timeout = float(timeout_env)
        except ValueError:
            self.timeout = 10.0

    async def create_bridge(self) -> str:
        async with httpx.AsyncClient() as client:
            # Создаём mixing bridge для транскодирования между форматами
            response = await client.post(
                f"{self.base_url}/bridges",
                auth=self.auth,
                json={"type": "mixing"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            bridge_id = data["id"]
            bridge_type = data.get("bridge_type", data.get("type", "unknown"))
            bridge_class = data.get("bridge_class", "unknown")
            logger.info(
                "Создан bridge %s: type=%s, bridge_type=%s, bridge_class=%s, "
                "channels=%s",
                bridge_id,
                data.get("type", "unknown"),
                bridge_type,
                bridge_class,
                data.get("channels", []),
            )
            return bridge_id

    async def create_external_media(
        self,
        bridge_id: str,
        session_uuid: str,
    ) -> str:
        """
        Создаёт externalMedia-канал, который подключается к AudioSocket серверу
        и передаёт туда UUID сессии.
        """
        url = f"{self.base_url}/channels/externalMedia"

        # Читаем host/port из переменных окружения
        audiosocket_host = os.getenv("AUDIOSOCKET_HOST", "audiosocket")
        audiosocket_port = os.getenv("AUDIOSOCKET_PORT", "7575")
        external_host = f"{audiosocket_host}:{audiosocket_port}"

        payload = {
            "app": self.app_name,
            "external_host": external_host,
            "format": "alaw",
            "direction": "both",
            "transport": "udp",
            "encapsulation": "rtp",
            "data": session_uuid,
        }

        logger.info(
            "Создание externalMedia (UDP/RTP): bridge_id=%s, session_uuid=%s, "
            "external_host=%s, payload=%s",
            bridge_id,
            session_uuid,
            external_host,
            payload,
        )

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                json=payload,
                auth=self.auth,
                timeout=self.timeout,
            )

        logger.debug(
            "ARI externalMedia response: status=%s, body=%s",
            response.status_code,
            response.text,
        )

        if response.status_code >= 200 and response.status_code < 300:
            data = response.json()
            external_channel_id = data.get("id", "")
            state = data.get("state", "unknown")
            logger.info(
                "ExternalMedia успешно создан: channel_id=%s, state=%s",
                external_channel_id,
                state,
            )
            return external_channel_id
        else:
            logger.error(
                "ARI externalMedia error (uuid=%s): status=%s, response=%s",
                session_uuid,
                response.status_code,
                response.text,
            )
            response.raise_for_status()
            return ""  # Недостижимо, но для type checker

    async def answer_channel(self, channel_id: str) -> None:
        """Переводит канал в состояние Up (ANSWER)."""
        url = f"{self.base_url}/channels/{channel_id}/answer"
        logger.info("Ответ на канал %s через ARI: %s", channel_id, url)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                auth=self.auth,
                timeout=self.timeout,
            )

            # Можно добавить debug лог тела ответа, если оно есть
            try:
                body = response.text
            except Exception:
                body = "<no body>"

            logger.debug(
                "ARI answer_channel response: status=%s, body=%s",
                response.status_code,
                body,
            )
            response.raise_for_status()

    async def get_channel_details(self, channel_id: str) -> dict:
        """Получает детали канала из ARI и логирует основную информацию."""
        url = f"{self.base_url}/channels/{channel_id}"
        logger.info("Запрос деталей канала %s через ARI: %s", channel_id, url)

        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                auth=self.auth,
                timeout=self.timeout,
            )
            text = response.text

            logger.debug(
                "ARI get_channel_details raw response: status=%s, body=%s",
                response.status_code,
                text,
            )
            response.raise_for_status()
            data = response.json()

        # Аккуратное логирование важных полей
        state = data.get("state")
        name = data.get("name")
        created = data.get("creationtime")
        formats = data.get("channelvars", {}).get("CHANNEL(audioreadformat)", "unknown")
        
        # Дополнительная информация о форматах
        read_format = data.get("channelvars", {}).get("CHANNEL(audioreadformat)", "unknown")
        write_format = data.get("channelvars", {}).get("CHANNEL(audiowriteformat)", "unknown")
        native_format = data.get("channelvars", {}).get("CHANNEL(audionativeformat)", "unknown")
        
        logger.info(
            "Детали канала %s: name=%s, state=%s, creationtime=%s, "
            "read_format=%s, write_format=%s, native_format=%s",
            channel_id,
            name,
            state,
            created,
            read_format,
            write_format,
            native_format,
        )

        # На всякий случай логируем caller/connected
        caller = data.get("caller", {})
        connected = data.get("connected", {})
        logger.info(
            "Канал %s: caller=%s, connected=%s",
            channel_id,
            caller,
            connected,
        )

        return data

    async def add_channel_to_bridge(
        self,
        bridge_id: str,
        channel_id: str,
    ) -> None:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/bridges/{bridge_id}/addChannel",
                auth=self.auth,
                timeout=self.timeout,
                params={"channel": channel_id},
            )
            response.raise_for_status()
            logger.info(
                "Канал %s добавлен в bridge %s (status=%s)",
                channel_id,
                bridge_id,
                response.status_code,
            )

    async def get_bridge_channels(self, bridge_id: str) -> list[str]:
        """Получает список каналов в bridge."""
        url = f"{self.base_url}/bridges/{bridge_id}"
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                auth=self.auth,
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            channels = data.get("channels", [])
            logger.info(
                "Bridge %s содержит %d каналов: %s",
                bridge_id,
                len(channels),
                channels,
            )
            return channels

    async def hangup_channel(self, channel_id: str) -> None:
        """Безопасно завершает канал."""
        url = f"{self.base_url}/channels/{channel_id}"
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                url,
                auth=self.auth,
                timeout=self.timeout,
            )
            if response.status_code in (404, 410):
                logger.info(
                    "Канал %s уже отсутствует (status=%s)",
                    channel_id,
                    response.status_code,
                )
                return
            response.raise_for_status()
            logger.info("Канал %s завершён (status=%s)", channel_id, response.status_code)

    async def delete_bridge(self, bridge_id: str) -> None:
        """Удаляет bridge, если он ещё существует."""
        url = f"{self.base_url}/bridges/{bridge_id}"
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                url,
                auth=self.auth,
                timeout=self.timeout,
            )
            if response.status_code in (404, 410):
                logger.info(
                    "Bridge %s уже отсутствует (status=%s)",
                    bridge_id,
                    response.status_code,
                )
                return
            response.raise_for_status()
            logger.info(
                "Bridge %s удалён (status=%s)",
                bridge_id,
                response.status_code,
            )

    async def get_variable(self, channel_id: str, variable: str) -> str | None:
        """
        Получает значение переменной канала из ARI.

        Используется для получения UNICASTRTP_LOCAL_PORT и UNICASTRTP_LOCAL_ADDRESS
        после создания ExternalMedia канала.
        """
        url = f"{self.base_url}/channels/{channel_id}/variable"
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                auth=self.auth,
                timeout=self.timeout,
                params={"variable": variable},
            )
            if response.status_code == 404:
                # Переменная не существует
                logger.debug(
                    "Переменная %s не найдена для канала %s", variable, channel_id
                )
                return None
            response.raise_for_status()

            # 🔧 ARI возвращает JSON: {"value": "..."}
            try:
                data = response.json()
                value = data.get("value", "").strip()
            except Exception:
                # Fallback: если не JSON, берём plain text
                value = response.text.strip()

            logger.debug(
                "Переменная %s канала %s = %s", variable, channel_id, value
            )
            return value


class AriWsHandler:
    def __init__(
        self,
        ari_client: AriClient,
        ws_url: str,
        app_name: str,
    ):
        self.ari_client = ari_client
        self.ws_url = ws_url
        self.app_name = app_name
        self.running = False
        self.channel_to_bridge: dict[str, str] = {}
        self.session_channels: dict[str, tuple[str, str]] = {}
        # 🔧 Получаем host/port audiosocket API из переменных окружения
        self.audiosocket_api_host = os.getenv("AUDIOSOCKET_HOST", "audiosocket")
        self.audiosocket_api_port = int(os.getenv("AUDIOSOCKET_API_PORT", "8888"))

    async def register_session_uuid_audiosocket(
        self, ip: str, port: int, session_uuid: str
    ) -> bool:
        """
        Регистрирует session_uuid в AudioSocket через HTTP API.

        AudioSocket запомнит этот UUID и будет использовать его когда Asterisk
        начнёт отправлять RTP на (ip, port).
        """
        url = f"http://{self.audiosocket_api_host}:{self.audiosocket_api_port}/register"
        payload = {"ip": ip, "port": port, "session_uuid": session_uuid}

        logger.info(
            "Регистрация session_uuid=%s в AudioSocket: ip=%s, port=%s, url=%s",
            session_uuid,
            ip,
            port,
            url,
        )

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, timeout=5.0)
                response.raise_for_status()
                data = response.json()
                logger.info(
                    "Успешно зарегистрирован session_uuid в AudioSocket: %s", data
                )
                return True
        except Exception as e:
            logger.error(
                "Ошибка регистрации session_uuid в AudioSocket: %s",
                e,
                exc_info=True,
            )
            return False

    async def handle_stasis_start(self, event: dict) -> None:
        try:
            logger.debug(
                "StasisStart raw event: %s", json.dumps(event, ensure_ascii=False)
            )

            channel = event.get("channel") or {}
            channel_id = channel.get("id")
            channel_name = channel.get("name", "")

            logger.info(
                "StasisStart: channel_id=%s, channel_name=%s",
                channel_id,
                channel_name,
            )

            # Обрабатываем только входящий PJSIP (абонент)
            if not channel_name.startswith("PJSIP/"):
                logger.info(
                    "StasisStart: канал %s (%s) не PJSIP, "
                    "скорее всего externalMedia/AudioSocket — пропускаем",
                    channel_id,
                    channel_name,
                )
                return

            # Новый звонок: создаём сессию и бридж
            session_uuid = str(uuid.uuid4())
            logger.info(
                "Новый звонок: channel_id=%s, session_uuid=%s",
                channel_id,
                session_uuid,
            )

            # Создаём mixing bridge
            bridge_id = await self.ari_client.create_bridge()
            logger.info("Создан bridge %s для channel_id=%s", bridge_id, channel_id)

            # Добавляем абонента в bridge
            await self.ari_client.add_channel_to_bridge(bridge_id, channel_id)
            logger.info(
                "Канал абонента %s добавлен в bridge %s",
                channel_id,
                bridge_id,
            )

            # Создаём externalMedia с session_uuid
            external_channel_id = await self.ari_client.create_external_media(
                bridge_id=bridge_id,
                session_uuid=session_uuid,
            )
            logger.info(
                "Создан externalMedia канал %s для session_uuid=%s",
                external_channel_id,
                session_uuid,
            )
            self.channel_to_bridge[channel_id] = bridge_id
            self.channel_to_bridge[external_channel_id] = bridge_id
            self.session_channels[session_uuid] = (channel_id, external_channel_id)

            # Сначала добавляем в bridge, ПОТОМ answer
            await self.ari_client.add_channel_to_bridge(
                bridge_id, external_channel_id
            )
            logger.info(
                "ExternalMedia канал %s добавлен в bridge %s",
                external_channel_id,
                bridge_id,
            )

            # Answer ПОСЛЕ добавления в bridge - это запускает RTP
            await self.ari_client.answer_channel(external_channel_id)
            logger.info(
                "ExternalMedia канал %s активирован (ANSWER)", external_channel_id
            )

            # 🔧 Получаем RTP параметры для регистрации session_uuid в AudioSocket
            await asyncio.sleep(0.2)  # Даём время Asterisk создать переменные канала
            rtp_local_address = await self.ari_client.get_variable(
                external_channel_id, "UNICASTRTP_LOCAL_ADDRESS"
            )
            rtp_local_port = await self.ari_client.get_variable(
                external_channel_id, "UNICASTRTP_LOCAL_PORT"
            )

            if rtp_local_address and rtp_local_port:
                logger.info(
                    "RTP параметры ExternalMedia: address=%s, port=%s",
                    rtp_local_address,
                    rtp_local_port,
                )
                # Регистрируем session_uuid в AudioSocket
                await self.register_session_uuid_audiosocket(
                    ip=rtp_local_address, port=int(rtp_local_port), session_uuid=session_uuid
                )
            else:
                logger.warning(
                    "Не удалось получить RTP параметры ExternalMedia: "
                    "address=%s, port=%s - session_uuid НЕ зарегистрирован в AudioSocket!",
                    rtp_local_address,
                    rtp_local_port,
                )

            # Проверяем, что оба канала в bridge
            await asyncio.sleep(0.5)  # Даём время на добавление
            channels = await self.ari_client.get_bridge_channels(bridge_id)
            
            # Получаем детали bridge для диагностики
            async with httpx.AsyncClient() as client:
                bridge_url = f"{self.ari_client.base_url}/bridges/{bridge_id}"
                bridge_response = await client.get(
                    bridge_url, auth=self.ari_client.auth
                )
                bridge_response.raise_for_status()
                bridge_data = bridge_response.json()
                logger.info(
                    "Детали bridge %s: type=%s, class=%s, channels=%s",
                    bridge_id,
                    bridge_data.get("type", "unknown"),
                    bridge_data.get("bridge_class", "unknown"),
                    channels,
                )
            
            if channel_id not in channels:
                logger.error(
                    "ОШИБКА: Канал абонента %s НЕ в bridge %s! "
                    "Каналы в bridge: %s",
                    channel_id,
                    bridge_id,
                    channels,
                )
            if external_channel_id not in channels:
                logger.error(
                    "ОШИБКА: ExternalMedia канал %s НЕ в bridge %s! "
                    "Каналы в bridge: %s",
                    external_channel_id,
                    bridge_id,
                    channels,
                )
            if len(channels) != 2:
                logger.warning(
                    "Bridge %s содержит %d каналов вместо ожидаемых 2: %s",
                    bridge_id,
                    len(channels),
                    channels,
                )
            else:
                logger.info(
                    "✅ Bridge %s содержит оба канала: %s",
                    bridge_id,
                    channels,
                )

        except Exception as e:
            logger.error(f"Ошибка при обработке StasisStart: {e}", exc_info=True)

    async def _cleanup_by_channel(self, channel_id: str) -> None:
        bridge_id = self.channel_to_bridge.get(channel_id)
        if not bridge_id:
            logger.info(
                "Cleanup: для канала %s bridge не найден, пропуск",
                channel_id,
            )
            return

        # Ищем session_uuid по каналу
        session_uuid = None
        for sid, channels in self.session_channels.items():
            if channel_id in channels:
                session_uuid = sid
                break

        try:
            await self.ari_client.hangup_channel(channel_id)
        except Exception as exc:
            logger.warning(
                "Cleanup: не удалось завершить канал %s: %s",
                channel_id,
                exc,
            )

        try:
            await self.ari_client.delete_bridge(bridge_id)
        except Exception as exc:
            logger.warning(
                "Cleanup: не удалось удалить bridge %s (channel=%s): %s",
                bridge_id,
                channel_id,
                exc,
            )

        self.channel_to_bridge.pop(channel_id, None)
        if session_uuid:
            self.session_channels.pop(session_uuid, None)
            logger.info(
                "Cleanup завершён (session_uuid=%s, channel=%s, bridge=%s)",
                session_uuid,
                channel_id,
                bridge_id,
            )

    async def run(self) -> None:
        self.running = True
        logger.info(f"Запуск ARI WebSocket handler для приложения {self.app_name}")

        auth_string = f"{self.ari_client.user}:{self.ari_client.password}"
        auth_b64 = base64.b64encode(auth_string.encode()).decode()
        auth_header = f"Basic {auth_b64}"
        reconnect_attempt = 0

        while self.running:
            try:
                async with websockets.connect(
                    self.ws_url,
                    extra_headers={"Authorization": auth_header},
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=1000,
                ) as websocket:
                    logger.info("Подключено к ARI WebSocket")
                    reconnect_attempt = 0

                    async for message in websocket:
                        try:
                            if isinstance(message, str):
                                event_data = json.loads(message)
                                event_type = event_data.get("type")

                                if event_type == "StasisStart":
                                    await self.handle_stasis_start(event_data)
                                elif event_type in ("StasisEnd", "ChannelDestroyed"):
                                    channel = event_data.get("channel") or {}
                                    channel_id = channel.get("id")
                                    logger.info(
                                        "Получено завершение канала "
                                        "(type=%s, channel_id=%s)",
                                        event_type,
                                        channel_id,
                                    )
                                    if channel_id:
                                        await self._cleanup_by_channel(channel_id)

                        except Exception as e:
                            logger.error(f"Ошибка при обработке события: {e}")

            except Exception as e:
                logger.error(f"Ошибка WebSocket соединения: {e}")
                if self.running:
                    reconnect_attempt += 1
                    delay = min(5 * reconnect_attempt, 30)
                    logger.info(
                        "Повторное подключение через %.1f сек (attempt=%d)",
                        delay,
                        reconnect_attempt,
                    )
                    await asyncio.sleep(delay)

    def stop(self) -> None:
        self.running = False

