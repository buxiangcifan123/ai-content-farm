"""
向量存储客户端统一工厂。
FAISS + SQLite，替换 ChromaDB（1.5.9 Rust HNSW 有 bug）。
"""
from vector_store import get_client
