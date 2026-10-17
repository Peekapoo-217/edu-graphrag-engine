# -*- coding: utf-8 -*-
"""
utils/config.py

Centralized configuration manager loading environment variables from .env file.
Enforces no hardcoded connection parameters, API keys, or endpoints in source code.
"""

import os
from dataclasses import dataclass
from typing import Optional


def load_env_file(env_path: Optional[str] = None) -> None:
    """Load environment variables from .env file into os.environ if present."""
    if env_path is None:
        # Dynamically locate .env file in data-scrawler root folder
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env_path = os.path.join(base_dir, ".env")

    if os.path.exists(env_path):
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
        except ImportError:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"\''))


# Automatically load .env on module import before class Settings definition
load_env_file()


def _database_url() -> Optional[str]:
    configured = os.getenv("DATABASE_URL")
    if configured:
        return configured
    jdbc_url = os.getenv("SPRING_DATASOURCE_URL", "")
    if not jdbc_url.startswith("jdbc:postgresql://"):
        return None
    from urllib.parse import quote, urlsplit, urlunsplit
    parsed = urlsplit(jdbc_url.removeprefix("jdbc:"))
    username = quote(os.getenv("SPRING_DATASOURCE_USERNAME", ""), safe="")
    password = quote(os.getenv("SPRING_DATASOURCE_PASSWORD", ""), safe="")
    auth = f"{username}:{password}@" if username else ""
    return urlunsplit(("postgresql", auth + (parsed.hostname or "") + (f":{parsed.port}" if parsed.port else ""), parsed.path, parsed.query, ""))


@dataclass
class Settings:
    """Centralized System Settings loaded from environment variables."""
    # Database Settings
    DATABASE_URL: Optional[str] = _database_url()

    # Neo4j Settings
    NEO4J_URI: str = os.getenv("NEO4J_URI", "")
    NEO4J_USERNAME: str = os.getenv("NEO4J_USERNAME", "")
    NEO4J_PASSWORD: Optional[str] = os.getenv("NEO4J_PASSWORD")
    NEO4J_DATABASE: str = os.getenv("NEO4J_DATABASE", "")

    # LLM & 9router Gateway Settings
    LLM_API_KEY: Optional[str] = os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY")
    LLM_API_BASE: str = os.getenv("LLM_API_BASE", "")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "")
    LLM_MODEL_NAME: str = os.getenv("LLM_MODEL_NAME", "")
    LLM_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "30.0"))

    # API Server Settings
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))
    SUBJECT_PATHWAY_API_URL: str = os.getenv("SUBJECT_PATHWAY_API_URL", "http://localhost:8080").rstrip("/")
    SUBJECT_PATHWAY_API_TIMEOUT: float = float(os.getenv("SUBJECT_PATHWAY_API_TIMEOUT", "8.0"))
    # Ollama Embedding Settings
    OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "")
    OLLAMA_EMBED_MODEL: str = os.getenv("OLLAMA_EMBED_MODEL", "")
    OLLAMA_TIMEOUT: float = float(os.getenv("OLLAMA_TIMEOUT", "30.0"))
    EMBEDDER_BATCH_SIZE: int = int(os.getenv("EMBEDDER_BATCH_SIZE", "50"))

    # Vector Search Settings
    VECTOR_SIMILARITY_THRESHOLD: float = float(os.getenv("VECTOR_SIMILARITY_THRESHOLD", "0.65"))


settings = Settings()
