FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m obito && mkdir -p work models && chown -R obito /app
USER obito
ENV PORT=5000
EXPOSE 5000
# ONE process only: job state is in memory. Threads handle concurrent requests.
CMD ["gunicorn", "-w", "1", "--threads", "8", "-t", "600", "-b", "0.0.0.0:5000", "app:app"]
