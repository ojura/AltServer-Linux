# AltServer-Linux - self-contained image.
#
# Builds the binary from this repo and installs every RUNTIME prerequisite, so nothing has to be
# apt-installed on the host by hand. The two that are easy to miss, and both fail SILENTLY:
#
#   python3                       - AltServer is a -static binary and therefore cannot dlopen
#                                   Bonjour itself. It shells out to python3, which dlopens
#                                   libdns_sd.so on its behalf (libraries/dnssd_loader).
#   libavahi-compat-libdnssd-dev  - NOT ...-libdnssd1. The runtime package ships only
#                                   libdns_sd.so.1, while the code dlopens the UNVERSIONED
#                                   soname, whose symlink comes from the -dev package.
#
# Without either, the server starts, reports nothing wrong, and is permanently undiscoverable
# by the phone. The image verifies both at build time so that cannot ship broken.

# ---------------------------------------------------------------------------------------------
# Build stage - uses the project's own alpine toolchain, which carries the static corecrypto,
# cpprestsdk, boost and libzip this build needs. Override BUILDER for a different architecture:
#   ..._amd64 (default) | ..._aarch64 | ..._armv7 | ..._i386
# ---------------------------------------------------------------------------------------------
ARG BUILDER=ghcr.io/ben-diehlci/altserver_builder_alpine_amd64

# ---------------------------------------------------------------------------------------------
# idevice stage - the Rust library behind src/DevicePairingManager.cpp (Remote AltServer setup).
# The builder images have no Rust toolchain, so this builds it for TARGETARCH with cargo-zigbuild;
# zig supplies the C compiler and musl headers for ring's C and assembly. The build stage
# prelinks the result. The cargo build also generates the C header,
# libraries/idevice/ffi/idevice.h, which the build stage copies from here because the build
# context does not contain it. No --platform=$BUILDPLATFORM: the legacy builder does not define
# BUILDPLATFORM and rejects the empty value, and every documented build is native anyway.
# ---------------------------------------------------------------------------------------------
FROM ghcr.io/rust-cross/cargo-zigbuild:0.23.4 AS idevice
ARG TARGETARCH
WORKDIR /src
COPY libraries/idevice /src/libraries/idevice
COPY makefiles/idevice-build/idevice-features.txt /src/makefiles/idevice-build/idevice-features.txt
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) target=x86_64-unknown-linux-musl ;; \
      arm64) target=aarch64-unknown-linux-musl ;; \
      arm) target=armv7-unknown-linux-musleabihf ;; \
      386) target=i686-unknown-linux-musl ;; \
      *) echo "no Rust target for TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    rustup target add "$target"; \
    cd libraries/idevice/ffi; \
    cargo zigbuild --release --locked --target "$target" --target-dir /idevice-target \
      --no-default-features --features "$(cat /src/makefiles/idevice-build/idevice-features.txt)"; \
    mkdir -p /out; \
    cp "/idevice-target/$target/release/libidevice_ffi.a" /out/libidevice_ffi.a; \
    cp idevice.h /out/idevice.h

FROM ${BUILDER} AS build

WORKDIR /src
COPY . /src
COPY --from=idevice /out/libidevice_ffi.a /idevice/libidevice_ffi.a
COPY --from=idevice /out/idevice.h /src/libraries/idevice/ffi/idevice.h

# The binary is named for the gcc triple (AltServer-x86_64, AltServer-aarch64, ...), so normalise
# it to a single known path for the runtime stage to copy.
RUN set -eux; \
    rm -rf build; mkdir -p build /out; \
    cd build; \
    make -f ../Makefile -j"$(nproc)" IDEVICE_FFI_LIB=/idevice/libidevice_ffi.a; \
    cp AltServer-* /out/AltServer; \
    chmod +x /out/AltServer; \
    ls -la /out/AltServer

# ---------------------------------------------------------------------------------------------
# Runtime stage
# ---------------------------------------------------------------------------------------------
# trixie, NOT bookworm. netmuxd's release binaries are dynamically linked and require GLIBC_2.38;
# bookworm ships 2.36, so the image built fine and then failed at `netmuxd --about` with
# "version `GLIBC_2.38' not found". trixie ships 2.41.
#
# Safe to bump because AltServer itself is linked -static -- the base image's libc is irrelevant to
# it. Only python3, libimobiledevice-utils, avahi-utils and netmuxd are affected, and all were
# verified on trixie (including the libdns_sd.so load below) before this change was pushed.
FROM debian:trixie-slim

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        python3 \
        libavahi-compat-libdnssd-dev \
        ca-certificates \
        curl \
        tzdata \
        \
        # Needed by the setup web UI, which shells out to these for its health checks. Without
        # them the pairing and mDNS panels report "unknown" from inside the container -- which is
        # honest but useless, and those two are the checks people actually need.
        libimobiledevice-utils \
        avahi-utils \
        procps; \
    rm -rf /var/lib/apt/lists/*; \
    # Fail the BUILD rather than ship an image that cannot advertise. This is the exact call
    # dnssd_loader.cpp makes, so if it works here it works at runtime.
    python3 -c "from ctypes import CDLL; CDLL('libdns_sd.so')"; \
    echo "libdns_sd.so loads OK"

COPY --from=build /out/AltServer /usr/local/bin/AltServer

# ---------------------------------------------------------------------------------------------
# netmuxd. This is what makes WIRELESS refresh possible at all.
#
# Stock usbmuxd enumerates USB only -- it has no network-device support whatsoever, so with the
# cable out AltServer sees no device and every InstallProvisioningProfilesRequest fails. On top of
# that, Ubuntu's usbmuxd unit is `static` and udev-activated: it starts when a cable is plugged in
# and EXITS when the last device is removed, so unattended operation has no mux running at all.
#
# netmuxd browses _apple-mobdev2._tcp over mDNS, tracks the phone's address, and serves the
# usbmuxd socket protocol with the device presented as ConnectionType Network. It uses the
# pure-Rust mdns-sd crate rather than avahi, so it needs host networking for multicast but NOT
# the D-Bus/avahi sockets AltServer itself requires.
#
# Pinned deliberately. "Latest" would let an upstream release change behaviour without a commit
# here; bumping is a one-line change and CI rebuilds the image.
# ---------------------------------------------------------------------------------------------
ARG NETMUXD_VERSION=v0.4.3
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) nm_triple=x86_64-unknown-linux-gnu ;; \
      arm64) nm_triple=aarch64-unknown-linux-gnu ;; \
      *) echo "netmuxd publishes no build for TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/netmuxd.tar.gz \
      "https://github.com/jkcoxson/netmuxd/releases/download/${NETMUXD_VERSION}/netmuxd-${nm_triple}.tar.gz"; \
    tar -xzf /tmp/netmuxd.tar.gz -C /tmp; \
    install -m 0755 /tmp/netmuxd /usr/local/bin/netmuxd; \
    rm -f /tmp/netmuxd.tar.gz /tmp/netmuxd; \
    # Fail the BUILD rather than ship a binary that cannot run here -- the release is dynamically
    # linked against glibc, so a runtime-image change could break it silently otherwise.
    # NOTE: the v0.4.3 binary self-reports "netmuxd v0.4.2" -- upstream did not bump the string.
    # That is cosmetic; the download is correct.
    /usr/local/bin/netmuxd --about

# Fetches the current AltStore Classic IPA, resolving the URL from AltStore's own catalogue rather
# than a hardcoded one -- a pinned URL silently installs an ever-older AltStore.
# The setup web UI: status dashboard, pairing wizard and the install flow. This image only
# CARRIES it -- the entrypoint runs AltServer, not the UI. deploy/altserver-stack.yml starts it as
# a separate `altserver-web` service from the same image. Running it by hand:
#   docker exec altserver python3 /opt/altserver-web/server.py --host 127.0.0.1
# Bind loopback unless you mean otherwise: it accepts an Apple ID password over plain HTTP.
COPY web/ /opt/altserver-web/

COPY web/fetch_altstore.py /usr/local/bin/fetch-altstore
COPY docker/docker-entrypoint.sh /usr/local/bin/docker-entrypoint
# Filters AltServer's output so credentials never reach docker logs. It imports _redact
# from /opt/altserver-web/installer.py above, so there is one redaction implementation.
COPY docker/redact-log.py /usr/local/bin/redact-log
RUN chmod +x /usr/local/bin/fetch-altstore /usr/local/bin/docker-entrypoint /usr/local/bin/redact-log

# AltServerApp writes to the RELATIVE path ./AltServerData, resolved against the working
# directory - so the workdir is load-bearing, not cosmetic. Mount a volume here to persist it.
WORKDIR /data

# Sane defaults; override in compose.
#   ALTSERVER_ANISETTE_SERVER is REQUIRED and deliberately has no default: the server that used
#   to be hardcoded is dead, and a shared anisette identity can get Apple IDs locked.
ENV ALTSERVER_ANISETTE_SERVER=""

# Documents intent only. mDNS needs the host network, so compose must set network_mode: host -
# a published port cannot help, and Bonjour does not cross a bridge.
EXPOSE 51820

# The entrypoint refreshes /data/AltStore.ipa, then execs AltServer with whatever arguments were
# given. No IPA argument = daemon mode; add one (or docker exec) for a one-time install.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
