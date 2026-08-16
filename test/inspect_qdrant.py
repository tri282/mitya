from collections import Counter
from qdrant_client import QdrantClient
from fastembed import TextEmbedding
from src.config import Config

c = QdrantClient(path=Config.VECTOR_DB_DIR)
COLL = Config.QDRANT_COLLECTION

# 1. point count
print("points:", c.count(COLL).count)                       # expect 3284

# 2. collection config: named vectors + IDF modifier
info = c.get_collection(COLL)
print("dense:", info.config.params.vectors["dense"])        # size=768, COSINE
print("bm25 modifier:", info.config.params.sparse_vectors["bm25"].modifier)  # idf

# 3. inspect one real point: payload shape + that BOTH vectors exist
pts, _ = c.scroll(COLL, limit=1, with_payload=True, with_vectors=True)
p = pts[0]
print("payload keys:", sorted(p.payload))                   # book, canonical_entities, chapter, part, text
print("book/part/chapter:", p.payload["book"], p.payload["part"], p.payload["chapter"])
print("dense dim:", len(p.vector["dense"]))                 # 768
print("bm25 nonzero terms:", len(p.vector["bm25"].indices)) # > 0  (sparse actually populated)
print("canonical_entities:", p.payload["canonical_entities"])  # []  (empty in v1, by design)

# 4. per-book point distribution (chunks per novel)
books = Counter()
off = None
while True:
    batch, off = c.scroll(COLL, limit=1000, offset=off, with_payload=["book"])
    for pt in batch: books[pt.payload["book"]] += 1
    if off is None: break
print("chunks/book:", dict(books))   # C&P 686, BK 1179, Idiot 812, Possessed 461, Notes 146

# 5. semantic smoke test — MUST use search_query: prefix (retrieval owes it)
emb = TextEmbedding(model_name=Config.EMBED_MODEL)
qv = list(emb.embed(["search_query: Raskolnikov axe"]))[0].tolist()
hits = c.query_points(COLL, query=qv, using="dense", limit=5, with_payload=True).points
for h in hits:
    print(f"  {h.score:.3f}  {h.payload['book']} p{h.payload['part']} c{h.payload['chapter']}")
