"""Build an offline/online retrieval index for lesson 0005.

用法：
    python3 index.py --chunker structure --out index/structure.json
    python3 index.py --chunker naive --size 400 --overlap 80 --out index/naive-400.json

可选（需要支持 /v1/embeddings 的 OpenAI-compatible 端点）：
    python3 index.py --chunker structure --embed-api --out index/structure-embedded.json

索引文件包含：chunk 元数据 + 可选的 embedding 向量。
BM25 的统计量在加载时重建，不写入磁盘——它是确定性的派生数据。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rag import (
    build_chunks,
    chunk_stats,
    load_documents,
    make_index,
    save_index,
)

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a RAG index (lesson 0005)")
    parser.add_argument("--corpus", default=str(ROOT / "corpus"), help="语料目录")
    parser.add_argument(
        "--chunker", choices=["structure", "naive"], default="structure"
    )
    parser.add_argument("--size", type=int, default=400, help="naive chunker 字符窗口")
    parser.add_argument("--overlap", type=int, default=80, help="naive chunker 重叠字符")
    parser.add_argument(
        "--out", default=str(ROOT / "index" / "structure.json"), help="索引输出路径"
    )
    parser.add_argument(
        "--embed-api",
        action="store_true",
        help="调用 embedding API，把向量写入索引",
    )
    parser.add_argument(
        "--embed-model",
        default=None,
        help="覆盖 EMBEDDING_MODEL，例如 text-embedding-3-small",
    )
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()

    corpus_dir = Path(args.corpus)
    if not corpus_dir.exists():
        parser.error("corpus directory not found: %s" % corpus_dir)

    docs = load_documents(corpus_dir)
    if not docs:
        parser.error("no documents found in %s" % corpus_dir)

    chunks = build_chunks(
        docs, chunker=args.chunker, size=args.size, overlap=args.overlap
    )
    stats = chunk_stats(chunks)

    print("=== index build ===")
    print("corpus      : %s" % corpus_dir)
    print("documents   : %d" % len(docs))
    print("chunker     : %s" % args.chunker)
    if args.chunker == "naive":
        print("window      : size=%d overlap=%d" % (args.size, args.overlap))
    print(
        "chunks      : %d (avg %.0f chars, min %d, max %d)"
        % (stats["chunks"], stats["avg_chars"], stats["min_chars"], stats["max_chars"])
    )

    vectors = None
    embedding_model = None
    if args.embed_api:
        from embeddings import embed_texts, embedding_config, load_dotenv

        load_dotenv()
        config = embedding_config()
        embedding_model = args.embed_model or config["model"]
        print("embedding   : %s (this may take a few seconds)" % embedding_model)
        try:
            vectors, usage = embed_texts(
                [chunk.text for chunk in chunks],
                model=embedding_model,
                batch_size=args.batch_size,
            )
        except RuntimeError as exc:
            print("ERROR: %s" % exc)
            print("Hint: fill EMBEDDING_* in .env, or rebuild without --embed-api.")
            raise SystemExit(2)
        print(
            "vectors     : %d (dim=%d, prompt_tokens=%d)"
            % (len(vectors), len(vectors[0]) if vectors else 0, usage["prompt_tokens"])
        )

    index = make_index(
        docs,
        chunks,
        chunker=args.chunker,
        params={
            "size": args.size,
            "overlap": args.overlap,
        },
        embedding_model=embedding_model,
        vectors=(
            {chunk.chunk_id: vector for chunk, vector in zip(chunks, vectors)}
            if vectors
            else None
        ),
    )
    out_path = Path(args.out)
    save_index(index, out_path)
    print("index       : %s" % out_path)
    print()
    print("sample chunk ids:")
    for chunk in chunks[:5]:
        print("  - %s  [%s]" % (chunk.chunk_id, chunk.heading))


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
