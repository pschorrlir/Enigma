FROM debian:bookworm-slim

RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends python3 gdb binutils socat \
 && rm -rf /var/lib/apt/lists/*

COPY bin/ /target/
COPY server/ /opt/homework/server/

WORKDIR /workspace
CMD ["sleep", "infinity"]
