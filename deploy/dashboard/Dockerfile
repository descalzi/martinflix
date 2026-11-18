FROM python:3.13-slim
WORKDIR /app
COPY server.py .
COPY web/ web/
USER 65534:65534
CMD ["python", "-B", "server.py"]
