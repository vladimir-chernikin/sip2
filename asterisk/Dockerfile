FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive

# Базовые пакеты для сборки Asterisk
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      build-essential \
      wget curl ca-certificates \
      subversion git \
      autoconf automake libtool pkg-config \
      libxml2-dev \
      libsqlite3-dev \
      uuid-dev \
      libedit-dev \
      libssl-dev \
      libjansson-dev \
      libncurses5-dev \
      libcurl4-openssl-dev \
      libspeexdsp-dev \
      && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src

# Скачиваем и распаковываем Asterisk 20
RUN curl -sSL https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-20-current.tar.gz -o asterisk.tar.gz && \
    tar xzf asterisk.tar.gz && \
    rm asterisk.tar.gz

# Сборка Asterisk с включенным ARI + externalMedia
RUN cd /usr/src/asterisk-20* && \
    contrib/scripts/get_mp3_source.sh && \
    ./configure --with-pjproject-bundled --with-jansson-bundled && \
    make menuselect.makeopts && \
    menuselect/menuselect \
      --enable res_http_websocket \
      --enable res_ari \
      --enable res_ari_channels \
      --enable res_ari_applications \
      --enable res_ari_bridges \
      menuselect.makeopts && \
    make -j"$(nproc)" && \
    make install && \
    make samples && \
    # Установка стандартных звуков Asterisk (core sounds) для работы Playback(hello-world)
    make config && \
    ldconfig && \
    rm -rf /usr/src/*

# Создаём директорию для логов
RUN mkdir -p /var/log/asterisk

EXPOSE 5060/udp 7077/tcp 7077/udp 8088/tcp 10000-10100/udp

# Обработка CRLF для Windows-файлов перед запуском Asterisk
# Конфиги монтируются как volumes, поэтому обрабатываем их при старте
CMD sh -c "sed -i 's/\r$//' /etc/asterisk/pjsip.conf /etc/asterisk/extensions.conf 2>/dev/null || true && asterisk -fvvv"
