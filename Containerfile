# ─────────────────────────────────────────────────────────────────────────────
# Rembg — Podman Containerfile
# Kullanım:
#   podman build -f Containerfile -t rembg-web .
#   podman run -d -p 7000:7000 -v rembg-models:/root/.u2net:Z rembg-web
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Metadata
LABEL org.opencontainers.image.title="Rembg Web UI"
LABEL org.opencontainers.image.description="AI Background Removal — Gradio Web Interface"
LABEL org.opencontainers.image.source="https://github.com/danielgatis/rembg"

# Sistem bağımlılıkları
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        git \
        libgl1 \
        libglib2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Çalışma dizini
WORKDIR /rembg

# pip & poetry kurulumu
RUN pip install --upgrade --no-cache-dir pip \
    && pip install --no-cache-dir poetry poetry-dynamic-versioning

# Bağımlılık dosyalarını önce kopyala (layer cache için)
COPY pyproject.toml poetry.lock* ./

# Projeyi kopyala
COPY . .

# Poetry ile bağımlılıkları kur (CPU + CLI + Gradio)
RUN poetry config virtualenvs.create false \
    && poetry install --extras "cpu cli" --without dev --no-interaction --no-ansi

# Varsayılan model önceden indir (container başlatma süresini kısaltır)
# Model dosyası /root/.u2net/ dizinine kaydedilir → volume ile kalıcı hale gelir
RUN rembg d u2net

# Model cache dizini (volume mount noktası)
VOLUME ["/root/.u2net"]

# Web arayüzü portu
EXPOSE 7000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:7000/api || exit 1

# Doğrudan web sunucusu başlat
ENTRYPOINT ["rembg", "s"]
CMD ["--host", "0.0.0.0", "--port", "7000"]
