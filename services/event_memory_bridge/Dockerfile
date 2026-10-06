FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir 'fastapi>=0.116,<1' 'uvicorn>=0.35,<1'
COPY dashboard/event_memory_protocol.py ./
COPY services/event_memory_bridge/app.py ./
USER 1000:1000
EXPOSE 8099
CMD ["uvicorn", "app:from_environment", "--factory", "--host", "0.0.0.0", "--port", "8099", "--workers", "1", "--no-access-log"]
