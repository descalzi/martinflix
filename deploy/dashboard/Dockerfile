FROM python:3.13-slim
WORKDIR /app
COPY server.py .
COPY web/ web/
RUN chmod -R a+rX /app
USER 65534:65534
CMD ["python", "-B", "server.py"]
