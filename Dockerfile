FROM python:3.10-slim

WORKDIR /app

# 1. Install Basic System Tools & Git
# procps is needed for the 'psutil' library to manage processes
RUN apt-get update && apt-get install -y \
    curl \
    git \
    build-essential \
    procps \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Node.js (v18)
# This enables running .js files and using npm
RUN curl -fsSL https://deb.nodesource.com/setup_18.x | bash - && \
    apt-get install -y nodejs

# 3. Copy your bot files into the container
COPY . .

# 4. Install Python Dependencies for the Bot itself
RUN pip install --no-cache-dir -r requirements.txt

# 5. Start the Bot
CMD ["python", "bot.py"]
