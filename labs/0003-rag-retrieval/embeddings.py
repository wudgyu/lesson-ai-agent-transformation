"""可选的 embedding 适配层。

只有显式使用 `--embed-api` 或 dense 检索时才会 import openai；
离线 BM25 / lexical-vector / 测试都不依赖它。

兼容任何 OpenAI-compatible 的 `/v1/embeddings` 端点：
    EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL
缺省时回退到 LLM_API_KEY / LLM_BASE_URL。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def load_dotenv(path: str = ".env") -> None:
    """极简 .env 读取：不覆盖已存在的环境变量。"""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def embedding_config() -> Dict[str, str]:
    return {
        "api_key": os.getenv("EMBEDDING_API_KEY") or os.getenv("LLM_API_KEY") or "",
        "base_url": os.getenv("EMBEDDING_BASE_URL") or os.getenv("LLM_BASE_URL") or "",
        "model": os.getenv("EMBEDDING_MODEL") or "text-embedding-3-small",
    }


def embed_texts(
    texts: Sequence[str],
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    batch_size: int = 64,
) -> Tuple[List[List[float]], Dict[str, int]]:
    """把一批文本转成向量。

    返回 (vectors, usage)。vectors 的顺序与输入一致。
    """
    config = embedding_config()
    key = api_key or config["api_key"]
    if not key:
        raise RuntimeError(
            "No embedding API key. Set EMBEDDING_API_KEY or LLM_API_KEY in .env, "
            "or run the offline BM25/lexical-vector path."
        )
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - 只在真实 API 路径触发
        raise RuntimeError(
            "openai package is not installed. Run: pip install -r requirements.txt"
        ) from exc

    client = OpenAI(
        api_key=key,
        base_url=(base_url or config["base_url"]) or None,
        timeout=60.0,
        max_retries=2,
    )
    model_name = model or config["model"]
    vectors: List[List[float]] = []
    usage = {"prompt_tokens": 0, "total_tokens": 0}

    for start in range(0, len(texts), batch_size):
        batch = list(texts[start : start + batch_size])
        response = client.embeddings.create(model=model_name, input=batch)
        items = sorted(response.data, key=lambda item: getattr(item, "index", 0))
        vectors.extend([list(item.embedding) for item in items])
        if getattr(response, "usage", None) is not None:
            usage["prompt_tokens"] += getattr(response.usage, "prompt_tokens", 0) or 0
            usage["total_tokens"] += getattr(response.usage, "total_tokens", 0) or 0

    if len(vectors) != len(texts):
        raise RuntimeError(
            "embedding API returned %d vectors for %d inputs" % (len(vectors), len(texts))
        )
    return vectors, usage


def make_query_embedder(
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
):
    """返回一个 query -> vector 的函数，供 DenseIndex 使用。"""

    def embed_query(query: str) -> List[float]:
        vectors, _ = embed_texts(
            [query], model=model, api_key=api_key, base_url=base_url, batch_size=1
        )
        return vectors[0]

    return embed_query
