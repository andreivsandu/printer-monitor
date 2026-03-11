FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends snmp \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt

RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./app.py
COPY generate_dashboard.py ./generate_dashboard.py
COPY printers.csv ./printers.csv
COPY alert_state.json ./alert_state.json
COPY printer_dashboard.html ./printer_dashboard.html

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]
