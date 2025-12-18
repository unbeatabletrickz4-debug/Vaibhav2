FROM python:3.10-slim

WORKDIR /app

# Install system utilities AND git
RUN apt-get update && apt-get install -y \
    procps \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "bot.py"]
