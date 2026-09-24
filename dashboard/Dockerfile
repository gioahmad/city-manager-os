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
COPY attention_engine.py .
COPY waiting_actions_app.py .
COPY employee_app.py .
COPY employee_public_app.py .
COPY staff_admin_app.py .
COPY operations_routines_app.py .
COPY today_board_engine.py .
COPY today_board_app.py .
COPY executive_workflow_app.py .
COPY operations_occurrence_controls.py .
COPY operations_engine.py .
COPY flood_app.py .
COPY gis_import.py .
COPY map_app.py .
COPY spatial_reference_app.py .
COPY spatial_watch_app.py .
COPY geo_resolver.py .
COPY integration_runtime.py .
COPY transcom_runtime.py .
COPY integration_engine.py .
COPY pseg_engine.py .
COPY integrations_app.py .
COPY source_onboarding.py .
COPY transit_runtime.py .
COPY transit_engine.py .
COPY transit_app.py .
COPY integration_worker.py .
COPY private_auth.py .
COPY phase3_app.py .
COPY templates ./templates
COPY static ./static

EXPOSE 8000

CMD ["uvicorn", "phase3_app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
