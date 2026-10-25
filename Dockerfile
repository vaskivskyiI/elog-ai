FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY elogai ./elogai

ENV ELOGAI_HOST=0.0.0.0 ELOGAI_STATE_DIR=/state PYTHONUNBUFFERED=1
EXPOSE 8091
CMD ["python", "-m", "elogai", "--env-file", "/dev/null"]
