"""持久化层。

- session.py       async engine / sessionmaker / init_db / get_session
- models.py        6+1 张表的 SQLAlchemy 2.0 声明式模型
- vector_store.py  EmbeddingStore 协议与两种实现（内存 / pgvector）
- repository.py    常用查询封装，API 层不写裸 SQL
"""
