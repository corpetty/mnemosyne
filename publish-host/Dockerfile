# Build the context from this folder:  docker build -t mnemosyne-publish-host publish-host
# Static musl binary in an empty image: nothing else is in it to attack or to patch.
FROM rust:1.93-alpine AS build
RUN apk add --no-cache musl-dev
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked
RUN mkdir /data /viewer /guest && chown 10001:10001 /data

FROM scratch
COPY --from=build /src/target/release/mnemosyne-publish-host /mnemosyne-publish-host
COPY --from=build --chown=10001:10001 /data /data
COPY --from=build /viewer /viewer
# Empty in the image: the guest page (static/guest) is mounted at /guest by deploy/publish/compose.yaml.
COPY --from=build /guest /guest
USER 10001:10001
ENV PUBLISH_LISTEN=0.0.0.0:8080 \
    PUBLISH_DATA_DIR=/data \
    PUBLISH_VIEWER_DIR=/viewer \
    PUBLISH_GUEST_DIR=/guest
EXPOSE 8080
VOLUME /data
ENTRYPOINT ["/mnemosyne-publish-host"]
