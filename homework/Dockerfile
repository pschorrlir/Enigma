FROM debian:bookworm-slim

RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends python3 gdb binutils \
 && rm -rf /var/lib/apt/lists/*

COPY bin/ /target/

WORKDIR /workspace
CMD ["sleep", "infinity"]
