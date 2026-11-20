# The Docker CLI and its Compose plugin come from Docker's own image; the controller itself is Python's standard library.
FROM docker:27-cli AS docker

FROM python:3.13-alpine
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
WORKDIR /app
COPY deploy/controller/controller.py .
RUN chmod -R a+rX /app
CMD ["python", "-B", "controller.py"]
