FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY issues_app.py .
COPY operations_app.py .
COPY schedule_app.py .
COPY rules_app.py .
COPY employee_app.py .
COPY employee_public_app.py .
COPY staff_admin_app.py .
COPY templates ./templates
COPY static ./static

EXPOSE 8000

CMD ["uvicorn", "schedule_app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
