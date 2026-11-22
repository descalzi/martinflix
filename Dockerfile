# One image for all of MartinFlix: the dashboard, the controller and setup, with the Compose files they share.
# GitHub builds it for every push to main (the edge channel) and every version tag (the stable channel).
FROM docker:27-cli AS docker

FROM python:3.13-alpine
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
WORKDIR /app
COPY assets/preroll/ assets/preroll/
COPY server.py ./
COPY web/ web/
COPY deploy/controller/ deploy/controller/
COPY deploy/bootstrap/ deploy/bootstrap/
COPY deploy/image/martinflix /usr/local/bin/martinflix
ARG VERSION=dev
ARG REVISION=
ARG CHANNEL=dev
ENV MARTINFLIX_VERSION=$VERSION MARTINFLIX_REVISION=$REVISION MARTINFLIX_CHANNEL=$CHANNEL \
    MARTINFLIX_IMAGE_REPO=ghcr.io/descalzi/martinflix PYTHONDONTWRITEBYTECODE=1
LABEL org.opencontainers.image.title=MartinFlix \
      org.opencontainers.image.source=https://github.com/descalzi/martinflix \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.revision=$REVISION \
      io.martinflix.channel=$CHANNEL
RUN chmod -R a+rX /app && chmod a+rx /usr/local/bin/martinflix
ENTRYPOINT ["martinflix"]
CMD ["help"]
