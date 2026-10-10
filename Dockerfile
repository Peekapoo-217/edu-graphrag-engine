FROM python:3.11-slim

# Set Python environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install system dependencies if required
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy and install python dependencies
COPY requirements-rag.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements-rag.txt

# Copy RAG API application source code
COPY rag_api ./rag_api

# Create non-root user for security compliance
RUN useradd --create-home --shell /bin/bash appuser && \
    chown -R appuser:appuser /app
USER appuser

# Expose API port
EXPOSE 8000

# Start Uvicorn ASGI server
CMD ["uvicorn", "rag_api.main_api:app", "--host", "0.0.0.0", "--port", "8000"]
