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

COPY hb_preview.c /tmp/hb_preview.c

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
    && cp -r /tmp/handbrake/libhb/handbrake /usr/local/include/handbrake \
    && find /tmp/handbrake/build -name "project.h" -exec cp {} /usr/local/include/handbrake/project.h \; \
    && cp /tmp/handbrake/build/libhb/libhandbrake.a /usr/local/lib/libhandbrake.a \
    && cp -r /tmp/handbrake/build/contrib/lib/. /usr/local/lib/ \
    && cp -r /tmp/handbrake/build/contrib/include/. /usr/local/include/ \
    && echo "=== hb_scan signature ===" \
    && grep -A 5 "void.*hb_scan" /tmp/handbrake/libhb/handbrake/handbrake.h \
    && echo "=== hb_get_preview signatures ===" \
    && grep -A 3 "hb_get_preview" /tmp/handbrake/libhb/handbrake/handbrake.h \
    && echo "=== hb_init signature ===" \
    && grep -A 2 "hb_init" /tmp/handbrake/libhb/handbrake/handbrake.h \
    && echo "=== Compiling hb_preview ===" \
    && gcc -std=gnu99 -O2 \
        -I/tmp/handbrake/libhb \
        -I/tmp/handbrake/build/libhb \
        -I/tmp/handbrake/build/contrib/include \
        -D__LIBHB__ -DSYS_LINUX \
        -c /tmp/hb_preview.c -o /tmp/hb_preview.o \
        2>/tmp/hb_preview_error.log \
    && g++ -O2 \
        -o /usr/local/bin/hb_preview \
        /tmp/hb_preview.o \
        /tmp/handbrake/build/libhb/libhandbrake.a \
        -L/tmp/handbrake/build/contrib/lib \
        -Wl,--start-group \
        -lpthread -ldl -lm -lnuma -lass \
        -lavformat -lbz2 -lavfilter -lm -lzimg -lavcodec -lvpx -llzma -ldav1d \
        -ldl -lspeex -lmp3lame -lopus -lz -lswresample -ldvdnav -ldvdread \
        -lswscale -lavutil -lm -latomic -ltheoraenc -ltheoradec \
        -lvorbis -lvorbisenc -logg -lx264 -lbluray -pthread \
        -ljansson -lturbojpeg -lSvtAv1Enc -lx265 \
        -Wl,--end-group \
        2>>/tmp/hb_preview_error.log \
        && echo "hb_preview compiled successfully!" \
        || (echo "hb_preview compile failed:"; cat /tmp/hb_preview_error.log) \
    && rm -rf /tmp/handbrake

RUN HandBrakeCLI --version 2>&1 | grep HandBrake

RUN mkdir -p /config /output /storage
EXPOSE 8888
ENV PYTHONUNBUFFERED=1
CMD ["python3", "/app/server.py"]
