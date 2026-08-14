FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ shared/
COPY fl_server/ fl_server/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "fl_server/server.py"]
