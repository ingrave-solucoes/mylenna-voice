FROM python:3.11-slim

# Instalar FFmpeg e dependências de sistema
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiar requirements e instalar
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código
COPY main.py .

# Expor porta
EXPOSE 10000

# Comando de start
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
