FROM python:3.10-slim

WORKDIR /app

# 1. Install Basic System Tools, Git, and Node.js
# procps is needed for psutil to check running processes
# Node.js is needed to host .js files and run npm install
RUN apt-get update && apt-get install -y \
    curl \
    git \
    build-essential \
    procps \
    && curl -fsSL https://deb.nodesource.com/setup_18.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# 2. Copy your bot files into the container
COPY . .

# 3. Install Python Dependencies for the Bot
RUN pip install --no-cache-dir -r requirements.txt

# 4. Start the Bot
CMD ["python", "bot.py"]
