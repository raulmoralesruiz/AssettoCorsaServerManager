FROM python:3.11-slim

WORKDIR /opt/acweb

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python3", "app.py"]
