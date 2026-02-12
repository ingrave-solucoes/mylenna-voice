FROM python:3.11

# Instalar FFmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiar requirements primeiro (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código
COPY main.py .

# Variável de ambiente padrão (pode ser sobrescrita pelo Render)
ENV PORT=10000

# Comando de start (usando $PORT)
CMD sh -c "uvicorn main:app --host 0.0.0.0 --port ${PORT}"
