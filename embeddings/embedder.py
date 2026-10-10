# -*- coding: utf-8 -*-
"""
embeddings/embedder.py

Automated Vectorization script for Neo4j TrainingProgram nodes using local Ollama (bge-m3 model).
Generates 1024-dimensional embeddings and persists vector properties directly onto Neo4j nodes.
Centralized configuration imported from rag_api.utils.config.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
import neo4j

# Ensure data-scrawler root directory is in sys.path for direct script execution
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from rag_api.utils.config import settings
from rag_api.utils.logger import logger


def get_neo4j_driver() -> neo4j.Driver:
    """Create and verify Neo4j driver connection."""
    if not settings.NEO4J_PASSWORD:
        logger.error("NEO4J_PASSWORD environment variable is missing.")
        sys.exit(1)

    try:
        driver = neo4j.GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD)
        )
        driver.verify_connectivity()
        logger.info(f"Connected to Neo4j database '{settings.NEO4J_DATABASE}' at {settings.NEO4J_URI}")
        return driver
    except Exception as e:
        logger.error(f"Failed to connect to Neo4j: {e}")
        sys.exit(1)


def get_ollama_embedding(
    text: str,
    model: Optional[str] = None,
    ollama_url: Optional[str] = None,
    timeout: Optional[float] = None
) -> List[float]:
    """
    Generate embedding vector for input text via Ollama REST API.

    Args:
        text (str): Input text string to embed.
        model (Optional[str]): Ollama model name (defaults to settings.OLLAMA_EMBED_MODEL).
        ollama_url (Optional[str]): Base URL of Ollama service (defaults to settings.OLLAMA_HOST).
        timeout (Optional[float]): Request timeout in seconds (defaults to settings.OLLAMA_TIMEOUT).

    Returns:
        List[float]: 1024-dimensional embedding float list.

    Raises:
        RuntimeError: If HTTP request fails or invalid response returned.
    """
    model_name = model or settings.OLLAMA_EMBED_MODEL
    host_url = ollama_url or settings.OLLAMA_HOST
    timeout_val = timeout or settings.OLLAMA_TIMEOUT

    endpoint = f"{host_url.rstrip('/')}/api/embeddings"
    payload = json.dumps({"model": model_name, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_val) as response:
            if response.status == 200:
                res_body = json.loads(response.read().decode("utf-8"))
                embedding = res_body.get("embedding")
                if isinstance(embedding, list) and len(embedding) > 0:
                    return [float(v) for v in embedding]
                else:
                    raise RuntimeError("Ollama API returned empty embedding vector.")
            else:
                raise RuntimeError(f"Ollama HTTP error status: {response.status}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to connect to Ollama endpoint ({endpoint}): {e}") from e
    except Exception as e:
        raise RuntimeError(f"Unexpected error calling Ollama API: {e}") from e


def fetch_unembedded_training_programs(
    driver: neo4j.Driver,
    database: Optional[str] = None,
    limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Query Neo4j for TrainingProgram nodes that do not yet have an embedding property.

    Args:
        driver (neo4j.Driver): Neo4j Driver instance.
        database (Optional[str]): Target database name (defaults to settings.NEO4J_DATABASE).
        limit (Optional[int]): Maximum records to fetch per iteration (defaults to settings.EMBEDDER_BATCH_SIZE).

    Returns:
        List[Dict[str, Any]]: Unembedded TrainingProgram node properties & university metadata.
    """
    db_name = database or settings.NEO4J_DATABASE
    fetch_limit = limit or settings.EMBEDDER_BATCH_SIZE

    cypher_query = """
    MATCH (tp:TrainingProgram)
    WHERE tp.embedding IS NULL
    OPTIONAL MATCH (u:University)-[:OFFERS_PROGRAM]->(tp)
    RETURN 
        tp.uid AS uid,
        tp.program_code AS program_code,
        tp.name AS program_name,
        u.code AS university_code,
        u.name AS university_name
    LIMIT $limit
    """

    records = []
    with driver.session(database=db_name) as session:
        result = session.run(cypher_query, limit=fetch_limit)
        for record in result:
            records.append(record.data())
    return records


def construct_embedding_text(node_data: Dict[str, Any]) -> str:
    """
    Combine node attributes into a rich text string for vectorization.

    Args:
        node_data (Dict[str, Any]): Node property values.

    Returns:
        str: Enriched text representation.
    """
    prog_name = node_data.get("program_name") or ""
    prog_code = node_data.get("program_code") or ""
    univ_name = node_data.get("university_name") or ""
    univ_code = node_data.get("university_code") or ""

    parts = [f"Ngành đào tạo: {prog_name}"]
    if prog_code:
        parts.append(f"Mã ngành: {prog_code}")
    if univ_name or univ_code:
        parts.append(f"Trường: {univ_name} ({univ_code})".strip())

    return ". ".join(parts) + "."


def update_node_vector_property(
    driver: neo4j.Driver,
    database: Optional[str],
    uid: str,
    embedding: List[float]
) -> None:
    """
    Persist 1024-dimensional embedding vector property to Neo4j node using Cypher.

    Args:
        driver (neo4j.Driver): Neo4j Driver instance.
        database (Optional[str]): Target database name (defaults to settings.NEO4J_DATABASE).
        uid (str): TrainingProgram node UID.
        embedding (List[float]): Vector embedding values.
    """
    db_name = database or settings.NEO4J_DATABASE
    cypher_query = """
    MATCH (tp:TrainingProgram {uid: $uid})
    CALL db.create.setNodeVectorProperty(tp, 'embedding', $embedding)
    """

    with driver.session(database=db_name) as session:
        session.run(cypher_query, uid=uid, embedding=embedding)


def run_embedding_pipeline() -> None:
    """
    Execute full vectorization ETL pipeline:
    1. Connect to Neo4j.
    2. Query unembedded TrainingProgram nodes.
    3. Generate vector via Ollama bge-m3.
    4. Write vector property to Neo4j.
    """
    logger.info("=== STARTING TRAININGPROGRAM VECTORIZATION ETL PIPELINE ===")
    logger.info(f"Ollama Model:    {settings.OLLAMA_EMBED_MODEL}")
    logger.info(f"Ollama Host:     {settings.OLLAMA_HOST}")
    logger.info(f"Target Database: {settings.NEO4J_DATABASE}")
    logger.info(f"Batch Size:      {settings.EMBEDDER_BATCH_SIZE}")

    driver = get_neo4j_driver()
    total_processed = 0

    try:
        while True:
            nodes = fetch_unembedded_training_programs(
                driver,
                database=settings.NEO4J_DATABASE,
                limit=settings.EMBEDDER_BATCH_SIZE
            )
            if not nodes:
                logger.info("No remaining TrainingProgram nodes without embedding. Pipeline complete!")
                break

            logger.info(f"Processing batch of {len(nodes)} TrainingProgram nodes...")
            batch_success = 0

            for node in nodes:
                uid = node.get("uid")
                if not uid:
                    continue

                text = construct_embedding_text(node)
                try:
                    embedding_vector = get_ollama_embedding(text)
                    vector_dim = len(embedding_vector)

                    # Update vector property in Neo4j
                    update_node_vector_property(driver, settings.NEO4J_DATABASE, uid, embedding_vector)

                    total_processed += 1
                    batch_success += 1
                    logger.info(
                        f"[{total_processed}] Processed UID '{uid}' | "
                        f"Dim: {vector_dim} | Text: '{text[:50]}...'"
                    )

                except Exception as e:
                    logger.error(f"Failed to embed node UID '{uid}': {e}")
                    time.sleep(1.0)  # Brief pause on failure before retrying next node

            logger.info(f"Completed batch: {batch_success}/{len(nodes)} nodes updated.")

    finally:
        driver.close()
        logger.info(f"=== ETL PIPELINE FINISHED: Total {total_processed} TrainingProgram nodes embedded. ===")


if __name__ == "__main__":
    run_embedding_pipeline()
