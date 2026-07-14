FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

ARG HB_VERSION=1.11.2

RUN apt-get update && apt-get install -y \
    autoconf \
    automake \
    build-essential \
    cmake \
    git \
    libass-dev \
    libbz2-dev \
    libfontconfig-dev \
    libfreetype-dev \
    libfribidi-dev \
    libharfbuzz-dev \
    libjansson-dev \
    liblzma-dev \
    libmp3lame-dev \
    libnuma-dev \
    libogg-dev \
    libopus-dev \
    libsamplerate-dev \
    libspeex-dev \
    libtheora-dev \
    libtool \
    libtool-bin \
    libturbojpeg0-dev \
    libvorbis-dev \
    libvpx-dev \
    libx264-dev \
    libxml2-dev \
    libzimg-dev \
    m4 \
    make \
    meson \
    nasm \
    ninja-build \
    patch \
    pkg-config \
    python3 \
    python3-pip \
    python3-flask \
    tar \
    wget \
    yasm \
    zlib1g-dev \
    ffmpeg \
    mkvtoolnix \
    tzdata \
    && pip3 install flask-cors --break-system-packages \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN wget -q "https://github.com/HandBrake/HandBrake/releases/download/${HB_VERSION}/handbrake-${HB_VERSION}-source.tar.bz2" \
    -O /tmp/handbrake.tar.bz2 \
    && mkdir -p /tmp/handbrake \
    && tar -xjf /tmp/handbrake.tar.bz2 --strip-components=1 -C /tmp/handbrake \
    && rm /tmp/handbrake.tar.bz2

RUN cd /tmp/handbrake \
    && ./configure --launch-jobs=$(nproc) --disable-gtk --enable-x265 \
    && cd build \
    && make -j$(nproc) \
    && make install \
    && rm -rf /tmp/handbrake

RUN HandBrakeCLI --version 2>&1 | grep HandBrake

RUN mkdir -p /config /output /storage
EXPOSE 8888
ENV PYTHONUNBUFFERED=1
CMD ["python3", "/app/server.py"]
