FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ shared/
COPY graph/ graph/
COPY branch_node/ branch_node/
COPY data/customers.json data/customers.json

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

CMD ["python", "branch_node/main.py"]
