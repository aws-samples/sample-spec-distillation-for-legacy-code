#!/usr/bin/env python3
"""
knowledge.py — Knowledge Graph + Vector Index for Specification Distillation.

Replaces LLM-based reconciliation with a deterministic graph + vector approach:
1. Parse concept blocks from raw concat files
2. Embed with all-mpnet-base-v2 (768-dim, local)
3. Store in NetworkX graph + SQLite vectors
4. KNN dedup + cross-still edge linking
5. Materialize per-manifest files from graph queries

Dependencies: sentence-transformers, networkx, numpy, sqlite3 (stdlib)
"""

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import networkx as nx
import numpy as np

import concept_key as ckey


# ── Data Model ───────────────────────────────────────────────────────────────


@dataclass
class ConceptBlock:
    """A single extracted concept from a map partial."""

    block_id: str  # deterministic hash of content
    block_type: str  # entity, rule, invariant, obligation, boundary, etc.
    title: str  # the ### header text
    body: str  # full block text including header
    source_page: str  # e.g. "page_042"
    source_still: str  # e.g. "data"
    classification: str  # extracted from "Classification:" line if present
    confidence: int  # extracted from "Confidence:" line if present (default 3)
    rule_type: str  # for rule blocks: declarative, decision_point, prohibition, etc.
    embedding: Optional[np.ndarray] = field(default=None, repr=False)


@dataclass
class UnifiedConcept:
    """A materialized unified concept aggregating one or more observation blocks.

    After dedup, every dedup group AND every standalone block becomes a
    UnifiedConcept node in the graph. This is the authoritative representation
    used for materialization (master blend output).

    The `summary` property provides a deterministic getter: returns
    body_synthesized if an LLM synthesis has been produced, otherwise
    falls back to body_raw (the lossless concat of all contributors).
    """

    concept_id: str  # deterministic hash
    title: str  # canonical title (best/longest from contributors)
    block_type: str  # dominant type from contributors
    body_raw: str  # lossless concat of all contributor bodies
    body_synthesized: Optional[str] = None  # LLM-merged version (populated later)
    contributor_ids: list[str] = field(default_factory=list)  # block_ids of contributing observations
    source_stills: list[str] = field(default_factory=list)  # which stills contributed
    source_pages: list[str] = field(default_factory=list)  # which pages contributed
    confidence: int = 0  # max confidence across contributors
    classification: str = ""

    @property
    def summary(self) -> str:
        """Deterministic getter: synthesized if available, else raw concat."""
        if self.body_synthesized is not None:
            return self.body_synthesized
        return self.body_raw

    @property
    def contributor_count(self) -> int:
        return len(self.contributor_ids)


# ── Block Type Detection ─────────────────────────────────────────────────────

# Patterns to classify block type from the ### header line
# Order matters: more specific patterns first
_TYPE_PATTERNS = [
    (r"^###\s+(Business Object|Entity)\b", "entity"),
    (r"^###\s+Rule\b", "rule"),
    (r"^###\s+Decision Point\b", "rule"),  # Normalize: decision_point → rule
    (r"^###\s+Business Rule\b", "rule"),   # Normalize: "Business Rule" → rule
    (r"^###\s+RULE\b", "rule"),            # Normalize: uppercase variant
    (r"^###\s+Invariant\b", "invariant"),
    (r"^###\s+(Data Obligation|Obligation)\b", "obligation"),
    (r"^###\s+(Data Boundary|Boundary)\b", "boundary"),
    (r"^###\s+State\b", "state"),
    (r"^###\s+Workflow\b", "workflow"),
    (r"^###\s+Interface\b", "interface"),
    (r"^###\s+Permission\b", "permission"),
    (r"^###\s+Exception\b", "exception"),
    (r"^###\s+Event\b", "event"),
    (r"^###\s+Command\b", "command"),
    (r"^###\s+Finding\b", "finding"),
    (r"^###\s+Observation\b", "observation"),
    (r"^###\s+Step\b", "step"),
]

# Sub-type classification: when a block is typed as "rule", determine rule_type
_RULE_SUBTYPE_PATTERNS = [
    (r"^###\s+Decision Point\b", "decision_point"),
    (r"Type:\s*(Prohibition|prohibition)", "prohibition"),
    (r"Type:\s*(Obligation|obligation)", "obligation"),
    (r"Type:\s*(Constraint|constraint)", "constraint"),
    (r"Type:\s*(Eligibility|eligibility)", "eligibility"),
]


def _detect_block_type(header_line: str, body: str = "") -> str:
    """Detect the concept block type from its ### header and body content.

    Uses header patterns first, then falls back to body-based classification
    keywords, then defaults to "entity" for named objects (most common case
    in the data still).
    """
    for pattern, block_type in _TYPE_PATTERNS:
        if re.match(pattern, header_line, re.IGNORECASE):
            return block_type

    # Body-based fallback: look for classification markers
    if body:
        body_lower = body[:500].lower()
        if "require unique" in body_lower or "require " in body_lower:
            return "invariant"
        if "type: audit" in body_lower or "type: retention" in body_lower:
            return "obligation"
        if "type: tenant" in body_lower or "type: isolation" in body_lower:
            return "boundary"
        if "classification:" in body_lower:
            cl = _extract_field(body, "Classification").lower()
            if "junction" in cl or "relationship" in cl:
                return "entity"
            if "technical" in cl:
                return "entity"
            if "primary" in cl or "supporting" in cl:
                return "entity"

    # Header-based fallback: if it contains known entity patterns
    header_lower = header_line.lower()
    if any(kw in header_lower for kw in [
        "oe_", "lifecycle", "table:", "schema", "session", "template",
        "model", "record", "log", "cache", "config",
    ]):
        return "entity"

    # If header starts with a known type word, use it
    m = re.match(r"^###\s+(\w+)", header_line)
    if m:
        first_word = m.group(1).lower()
        if first_word in {"rule", "invariant", "obligation", "boundary", "entity",
                          "state", "workflow", "interface", "permission", "exception",
                          "event", "command", "finding", "step", "observation"}:
            return first_word

    # Default to entity (the most common block type for named business objects)
    return "entity"


def _extract_field(body: str, field_name: str) -> str:
    """Extract a field value from block body (e.g. 'Classification: ...')."""
    pattern = rf"^{field_name}:\s*(.+)$"
    m = re.search(pattern, body, re.MULTILINE | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _extract_confidence(body: str) -> int:
    """Extract confidence score (1-5) from body text."""
    m = re.search(r"Confidence:\s*(\d)", body)
    return int(m.group(1)) if m else 3


def _detect_rule_type(header_line: str, body: str) -> str:
    """Detect the rule sub-type for blocks classified as 'rule'.

    Returns: declarative, decision_point, prohibition, obligation, constraint, or declarative (default).
    """
    for pattern, subtype in _RULE_SUBTYPE_PATTERNS:
        if re.search(pattern, header_line) or re.search(pattern, body[:500]):
            return subtype
    return "declarative"


# ── Parsing ──────────────────────────────────────────────────────────────────


def parse_raw_concat(concat_path: str, still_name: str) -> list[ConceptBlock]:
    """Parse a raw concat file into individual concept blocks.

    Splits on ### headers. Each block runs from one ### to the next ### or
    end of page section (<!-- END PAGE: ... -->).

    Args:
        concat_path: Path to the {still}_raw_concat.md file.
        still_name: Name of the still (e.g. "data").

    Returns:
        List of parsed ConceptBlock objects.
    """
    text = Path(concat_path).read_text(encoding="utf-8")
    blocks: list[ConceptBlock] = []

    current_page = "unknown"
    # Split into lines for streaming parse
    lines = text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # Track current page from PAGE markers
        page_match = re.match(r"<!--\s*PAGE:\s*(\S+)\s*-->", line)
        if page_match:
            current_page = page_match.group(1)
            i += 1
            continue

        # Detect a concept block start (### header)
        if line.startswith("### "):
            header_line = line
            block_lines = [line]
            i += 1

            # Collect lines until next ### or page boundary or EOF
            while i < len(lines):
                next_line = lines[i]
                if next_line.startswith("### "):
                    break
                if re.match(r"<!--\s*(END )?PAGE:", next_line):
                    break
                if next_line.startswith("---") and i > 0 and lines[i-1].strip() == "":
                    break
                block_lines.append(next_line)
                i += 1

            body = "\n".join(block_lines).strip()
            if not body or len(body) < 20:
                continue

            # Build deterministic ID from content hash
            block_id = hashlib.sha256(
                f"{still_name}:{current_page}:{body[:200]}".encode()
            ).hexdigest()[:16]

            block_type = _detect_block_type(header_line, body)
            title = header_line.lstrip("#").strip()
            classification = _extract_field(body, "Classification")
            confidence = _extract_confidence(body)

            blocks.append(ConceptBlock(
                block_id=block_id,
                block_type=block_type,
                title=title,
                body=body,
                source_page=current_page,
                source_still=still_name,
                classification=classification,
                confidence=confidence,
                rule_type=_detect_rule_type(header_line, body) if block_type == "rule" else "",
            ))
        else:
            i += 1

    return blocks


# ── Embedding ────────────────────────────────────────────────────────────────

_embedding_model = None


def _get_embedding_model():
    """Lazy-load the sentence-transformers model (768-dim).

    Forces local-only loading after first download — no network calls to HF.
    """
    global _embedding_model
    if _embedding_model is None:
        import os
        # Prevent any network calls to HF Hub (model is already cached locally)
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from sentence_transformers import SentenceTransformer
        _embedding_model = SentenceTransformer("all-mpnet-base-v2")
    return _embedding_model


def embed_blocks(blocks: list[ConceptBlock], embedding_batch_size: int = 64) -> None:
    """Embed all blocks in-place using all-mpnet-base-v2.

    Uses title + first 512 chars of body as embedding input for each block.
    Modifies blocks in-place (sets .embedding field).

    Args:
        blocks: List of ConceptBlock to embed.
        embedding_batch_size: Encoder mini-batch size (sentence-transformers encode()).
            The embedding-model throughput knob — NOT page-batching or Bedrock batch.
    """
    if not blocks:
        return

    model = _get_embedding_model()
    texts = []
    for b in blocks:
        # Use title + truncated body for embedding
        embed_text = f"{b.title}\n{b.body[:512]}"
        texts.append(embed_text)

    embeddings = model.encode(texts, batch_size=embedding_batch_size, show_progress_bar=False)

    for block, emb in zip(blocks, embeddings):
        block.embedding = np.array(emb, dtype=np.float32)


# ── Vector Store (SQLite) ────────────────────────────────────────────────────


class VectorStore:
    """SQLite-backed vector store with numpy BLOB columns.

    Stores embeddings as raw float32 bytes. KNN via cosine similarity
    computed in-memory (efficient for <50K vectors).
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self._create_tables()

    def _create_tables(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS vectors (
                block_id TEXT PRIMARY KEY,
                still_name TEXT NOT NULL,
                block_type TEXT NOT NULL,
                title TEXT NOT NULL,
                source_page TEXT NOT NULL,
                embedding BLOB NOT NULL,
                body_hash TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT ''
            )
        """)
        # Add body column if missing (migration for existing DBs)
        try:
            self.conn.execute("SELECT body FROM vectors LIMIT 1")
        except sqlite3.OperationalError:
            self.conn.execute("ALTER TABLE vectors ADD COLUMN body TEXT NOT NULL DEFAULT ''")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS unified_concepts (
                concept_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                block_type TEXT NOT NULL,
                confidence INTEGER NOT NULL DEFAULT 0,
                contributor_count INTEGER NOT NULL DEFAULT 1,
                source_stills TEXT NOT NULL DEFAULT '',
                source_pages TEXT NOT NULL DEFAULT '',
                body_raw TEXT NOT NULL,
                body_synthesized TEXT,
                contributor_ids TEXT NOT NULL DEFAULT ''
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vectors_still
            ON vectors(still_name)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vectors_type
            ON vectors(block_type)
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_uc_type
            ON unified_concepts(block_type)
        """)
        self.conn.commit()

    def upsert(self, block: ConceptBlock) -> None:
        """Insert or replace a block's vector."""
        if block.embedding is None:
            raise ValueError(f"Block {block.block_id} has no embedding")
        body_hash = hashlib.sha256(block.body.encode()).hexdigest()[:32]
        self.conn.execute(
            """INSERT OR REPLACE INTO vectors
               (block_id, still_name, block_type, title, source_page,
                embedding, body_hash, body)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                block.block_id,
                block.source_still,
                block.block_type,
                block.title,
                block.source_page,
                block.embedding.tobytes(),
                body_hash,
                block.body,
            ),
        )

    def upsert_batch(self, blocks: list[ConceptBlock]) -> int:
        """Batch insert/replace. Returns count inserted."""
        count = 0
        for b in blocks:
            if b.embedding is not None:
                self.upsert(b)
                count += 1
        self.conn.commit()
        return count

    def upsert_unified_concept(self, concept: "UnifiedConcept") -> None:
        """Store a unified concept with its full body_raw."""
        self.conn.execute(
            """INSERT OR REPLACE INTO unified_concepts
               (concept_id, title, block_type, confidence, contributor_count,
                source_stills, source_pages, body_raw, body_synthesized, contributor_ids)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                concept.concept_id,
                concept.title,
                concept.block_type,
                concept.confidence,
                concept.contributor_count,
                json.dumps(concept.source_stills),
                json.dumps(concept.source_pages),
                concept.body_raw,
                concept.body_synthesized,
                json.dumps(concept.contributor_ids),
            ),
        )

    def upsert_unified_concepts_batch(self, concepts: list["UnifiedConcept"]) -> int:
        """Batch store unified concepts. Returns count inserted."""
        count = 0
        for c in concepts:
            self.upsert_unified_concept(c)
            count += 1
        self.conn.commit()
        return count

    def get_unified_concept(self, concept_id: str) -> Optional["UnifiedConcept"]:
        """Retrieve a unified concept by ID."""
        row = self.conn.execute(
            "SELECT * FROM unified_concepts WHERE concept_id = ?",
            (concept_id,),
        ).fetchone()
        if not row:
            return None
        return UnifiedConcept(
            concept_id=row[0],
            title=row[1],
            block_type=row[2],
            confidence=row[3],
            contributor_ids=json.loads(row[9]),
            source_stills=json.loads(row[5]),
            source_pages=json.loads(row[6]),
            body_raw=row[7],
            body_synthesized=row[8],
        )

    def get_all_unified_concepts(self, still_filter: str = "") -> list["UnifiedConcept"]:
        """Retrieve all unified concepts, optionally filtered by source still."""
        if still_filter:
            rows = self.conn.execute(
                "SELECT * FROM unified_concepts WHERE source_stills LIKE ?",
                (f'%"{still_filter}"%',),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM unified_concepts").fetchall()
        
        concepts = []
        for row in rows:
            concepts.append(UnifiedConcept(
                concept_id=row[0],
                title=row[1],
                block_type=row[2],
                confidence=row[3],
                contributor_ids=json.loads(row[9]),
                source_stills=json.loads(row[5]),
                source_pages=json.loads(row[6]),
                body_raw=row[7],
                body_synthesized=row[8],
            ))
        return concepts

    def knn(
        self, query_embedding: np.ndarray, k: int = 10, still_filter: str = ""
    ) -> list[tuple[str, float]]:
        """Find k nearest neighbors by cosine similarity.

        Args:
            query_embedding: 768-dim float32 vector.
            k: Number of neighbors to return.
            still_filter: If set, only search within this still.

        Returns:
            List of (block_id, similarity_score) tuples, descending by score.
        """
        query = "SELECT block_id, embedding FROM vectors"
        params: list = []
        if still_filter:
            query += " WHERE still_name = ?"
            params.append(still_filter)

        rows = self.conn.execute(query, params).fetchall()
        if not rows:
            return []

        # Load all embeddings into a matrix
        ids = []
        matrix = []
        dim = len(query_embedding)
        for block_id, emb_blob in rows:
            vec = np.frombuffer(emb_blob, dtype=np.float32)
            if len(vec) == dim:
                ids.append(block_id)
                matrix.append(vec)

        if not matrix:
            return []

        mat = np.array(matrix)  # shape: (N, 768)
        # Normalize for cosine similarity
        query_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)
        mat_norms = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-10)
        similarities = mat_norms @ query_norm  # shape: (N,)

        # Top-k
        top_indices = np.argsort(similarities)[::-1][:k]
        results = [(ids[i], float(similarities[i])) for i in top_indices]
        return results

    def get_all_embeddings(self, still_filter: str = "") -> dict[str, np.ndarray]:
        """Load all embeddings as a dict of block_id → vector."""
        query = "SELECT block_id, embedding FROM vectors"
        params: list = []
        if still_filter:
            query += " WHERE still_name = ?"
            params.append(still_filter)

        result = {}
        for block_id, emb_blob in self.conn.execute(query, params).fetchall():
            result[block_id] = np.frombuffer(emb_blob, dtype=np.float32).copy()
        return result

    def close(self):
        self.conn.close()


# ── Knowledge Graph (NetworkX) ───────────────────────────────────────────────


class KnowledgeGraph:
    """NetworkX-based knowledge graph for concept storage and dedup.

    Nodes = concept blocks. Edges = relationships (same_as, references, etc.).
    Persisted as JSON (node-link format). Exportable to Neo4j/Neptune later.
    """

    def __init__(self, graph_path: str = ""):
        self.graph_path = graph_path
        if graph_path and Path(graph_path).exists():
            with open(graph_path, "r") as f:
                data = json.load(f)
            self.G = nx.node_link_graph(data)
        else:
            self.G = nx.DiGraph()

    def add_block(self, block: ConceptBlock) -> None:
        """Add a concept block as a node in the graph."""
        self.G.add_node(
            block.block_id,
            title=block.title,
            # The ONE shared concept key (concept_key.py). The node keeps its RAW `### ` header in
            # `title`; the formatted still keeps an id-stamped, rewritten title. Both reduce to
            # this string, so the concept<->code bridge resolves by equality, not by a ladder of
            # normalisations. Stored (not only derived) so the artifact is self-describing.
            canonical_key=ckey.concept_key(block.source_still, block.title),
            block_type=block.block_type,
            source_page=block.source_page,
            source_still=block.source_still,
            classification=block.classification,
            confidence=block.confidence,
            body_preview=block.body[:300],
            body_length=len(block.body),
        )

    def add_edge(
        self, source_id: str, target_id: str, edge_type: str, score: float = 0.0
    ) -> None:
        """Add a typed edge between two concept nodes."""
        self.G.add_edge(source_id, target_id, edge_type=edge_type, score=score)

    def get_node(self, block_id: str) -> Optional[dict]:
        """Get node attributes by block_id."""
        if self.G.has_node(block_id):
            return dict(self.G.nodes[block_id])
        return None

    def nodes_by_type(self, block_type: str, still: str = "") -> list[str]:
        """Get all node IDs of a given type, optionally filtered by still."""
        results = []
        for node_id, attrs in self.G.nodes(data=True):
            if attrs.get("block_type") == block_type:
                if not still or attrs.get("source_still") == still:
                    results.append(node_id)
        return results

    def nodes_by_still(self, still_name: str) -> list[str]:
        """Get all node IDs for a given still."""
        return [
            nid for nid, attrs in self.G.nodes(data=True)
            if attrs.get("source_still") == still_name
        ]

    def duplicate_groups(self) -> list[list[str]]:
        """Get groups of nodes connected by 'same_as' edges."""
        # Build undirected subgraph of same_as edges
        same_as_edges = [
            (u, v) for u, v, d in self.G.edges(data=True)
            if d.get("edge_type") == "same_as"
        ]
        ug = nx.Graph()
        ug.add_edges_from(same_as_edges)
        return [list(cc) for cc in nx.connected_components(ug) if len(cc) > 1]

    def add_unified_concept(self, concept: "UnifiedConcept") -> None:
        """Add a UnifiedConcept as a first-class node in the graph.

        Creates the unified_concept node and CONTRIBUTES_TO edges from
        all contributor observation blocks.
        """
        self.G.add_node(
            concept.concept_id,
            node_type="unified_concept",
            title=concept.title,
            # one canonical key per source still (a unified concept may span stills); see
            # concept_key.py. Keeps the bridge resolvable by equality for unified concepts too.
            canonical_keys=[ckey.concept_key(s, concept.title) for s in (concept.source_stills or [])]
            or [ckey.concept_key("", concept.title)],
            block_type=concept.block_type,
            confidence=concept.confidence,
            classification=concept.classification,
            contributor_count=concept.contributor_count,
            source_stills=concept.source_stills,
            source_pages=concept.source_pages,
            body_raw_length=len(concept.body_raw),
            body_synthesized_length=len(concept.body_synthesized) if concept.body_synthesized else 0,
            has_synthesis=concept.body_synthesized is not None,
        )
        # Create CONTRIBUTES_TO edges from observation blocks to this concept
        for contrib_id in concept.contributor_ids:
            if self.G.has_node(contrib_id):
                self.G.add_edge(contrib_id, concept.concept_id, edge_type="CONTRIBUTES_TO")

    def get_unified_concepts(self, still_filter: str = "") -> list[str]:
        """Get all unified_concept node IDs, optionally filtered by source still."""
        results = []
        for node_id, attrs in self.G.nodes(data=True):
            if attrs.get("node_type") == "unified_concept":
                if not still_filter or still_filter in attrs.get("source_stills", []):
                    results.append(node_id)
        return results

    def save(self, path: str = "") -> None:
        """Persist graph to JSON (node-link format)."""
        save_path = path or self.graph_path
        if not save_path:
            raise ValueError("No graph_path set")
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        data = nx.node_link_data(self.G)
        with open(save_path, "w") as f:
            json.dump(data, f, indent=2)

    @property
    def node_count(self) -> int:
        return self.G.number_of_nodes()

    @property
    def edge_count(self) -> int:
        return self.G.number_of_edges()


# ── Deduplication ────────────────────────────────────────────────────────────


def find_duplicates(
    blocks: list[ConceptBlock],
    vector_store: VectorStore,
    similarity_threshold: float = 0.85,
    k_neighbors: int = 20,
) -> list[tuple[str, str, float]]:
    """Find duplicate concept blocks via approximate nearest neighbors (hnswlib).

    Uses hnswlib (HNSW index) to find K nearest neighbors per block, then
    filters pairs above the cosine similarity threshold. This is O(N log N)
    instead of the O(N²) all-pairs matrix that segfaults at 31K+ blocks.

    Cosine similarity is computed via inner-product on L2-normalized vectors
    (hnswlib's 'ip' space with normalized inputs = cosine similarity).

    Args:
        blocks: Newly parsed blocks to check. MUST all be from the same still.
        vector_store: Existing vector store with prior blocks.
        similarity_threshold: Cosine similarity threshold for dedup.
        k_neighbors: Number of nearest neighbors to query per block (default 20).

    Returns:
        List of (block_id_a, block_id_b, similarity) tuples.

    Raises:
        ValueError: If blocks span multiple stills (dedup boundary violation).
    """
    if not blocks:
        return []

    # Enforce dedup boundary: all blocks must be from the same still
    stills_present = set(b.source_still for b in blocks)
    if len(stills_present) > 1:
        raise ValueError(
            f"Dedup boundary violation: find_duplicates received blocks from "
            f"{len(stills_present)} stills ({stills_present}). "
            f"Dedup must operate within a single still."
        )

    # Collect only blocks with embeddings from this still
    embedded_blocks = [b for b in blocks if b.embedding is not None]
    if len(embedded_blocks) < 2:
        return []

    import hnswlib

    n = len(embedded_blocks)
    dim = embedded_blocks[0].embedding.shape[0]  # 768

    # Build matrix and L2-normalize (inner product on unit vectors = cosine sim)
    ids = [b.block_id for b in embedded_blocks]
    matrix = np.array([b.embedding for b in embedded_blocks], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1e-10
    normed = matrix / norms

    # Build hnswlib index (inner product space on normalized vecs = cosine)
    # ef_construction and M control index quality vs build speed
    index = hnswlib.Index(space="ip", dim=dim)
    index.init_index(max_elements=n, ef_construction=200, M=32)
    index.add_items(normed, list(range(n)))

    # Set ef (search quality) — higher = more accurate, slower
    index.set_ef(max(50, k_neighbors * 2))

    # Query K nearest neighbors for every block
    # k+1 because each block is its own nearest neighbor (distance 0)
    effective_k = min(k_neighbors + 1, n)
    labels, distances = index.knn_query(normed, k=effective_k)
    # labels: (N, k) — integer indices into our array
    # distances: (N, k) — inner product distances (for normalized vecs: 1 - cosine)
    # Actually with 'ip' space: distance = 1 - inner_product for normalized vecs
    # So similarity = 1 - distance

    # Collect unique pairs above threshold
    seen_pairs: set[tuple[str, str]] = set()
    duplicates: list[tuple[str, str, float]] = []

    for i in range(n):
        for j_idx in range(effective_k):
            neighbor_idx = int(labels[i][j_idx])
            if neighbor_idx == i:
                continue  # skip self

            # hnswlib 'ip' space: distance = 1 - inner_product
            # For normalized vectors: inner_product = cosine_similarity
            # So: similarity = 1 - distance
            similarity = 1.0 - float(distances[i][j_idx])

            if similarity < similarity_threshold:
                continue

            # Canonical pair ordering to avoid duplicates (a, b) and (b, a)
            id_a, id_b = ids[i], ids[neighbor_idx]
            pair_key = (min(id_a, id_b), max(id_a, id_b))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            duplicates.append((id_a, id_b, similarity))

    return duplicates


def create_unified_concepts(
    blocks: list[ConceptBlock],
    graph: KnowledgeGraph,
) -> list[UnifiedConcept]:
    """Create UnifiedConcept nodes from dedup groups and standalone blocks.

    Every block becomes part of exactly one UnifiedConcept:
    - Blocks in a dedup group → merged into one UnifiedConcept with body_raw
      as the lossless concat of all contributors
    - Standalone blocks (not in any dedup group) → promoted to a UnifiedConcept
      with a single contributor

    Args:
        blocks: All parsed blocks for this still.
        graph: The knowledge graph (must already have same_as edges from dedup).

    Returns:
        List of created UnifiedConcept objects.
    """
    block_map = {b.block_id: b for b in blocks}
    dup_groups = graph.duplicate_groups()

    # Track which block_ids are in a group
    grouped_ids: set[str] = set()
    for group in dup_groups:
        grouped_ids.update(group)

    concepts: list[UnifiedConcept] = []

    # Multi-contributor unified concepts (from dedup groups)
    for group in dup_groups:
        # Get all contributing blocks
        contributors = [block_map[bid] for bid in group if bid in block_map]
        if not contributors:
            continue

        # Pick canonical title: longest title from highest-confidence contributor
        contributors.sort(key=lambda b: (-b.confidence, -len(b.title)))
        canonical_title = contributors[0].title

        # Dominant block_type: most common type among contributors
        type_counts: dict[str, int] = {}
        for c in contributors:
            type_counts[c.block_type] = type_counts.get(c.block_type, 0) + 1
        dominant_type = max(type_counts, key=type_counts.get)  # type: ignore

        # body_raw: lossless concat with clear separators
        body_parts = []
        for c in contributors:
            body_parts.append(f"<!-- contributor: {c.source_still}:{c.source_page} -->\n{c.body}")
        body_raw = "\n\n---\n\n".join(body_parts)

        # Deterministic concept_id from sorted contributor IDs
        sorted_ids = sorted(b.block_id for b in contributors)
        concept_id = hashlib.sha256(
            "|".join(sorted_ids).encode()
        ).hexdigest()[:16]
        concept_id = f"uc_{concept_id}"  # prefix to distinguish from block nodes

        concept = UnifiedConcept(
            concept_id=concept_id,
            title=canonical_title,
            block_type=dominant_type,
            body_raw=body_raw,
            body_synthesized=None,
            contributor_ids=[b.block_id for b in contributors],
            source_stills=list(set(b.source_still for b in contributors)),
            source_pages=list(set(b.source_page for b in contributors)),
            confidence=max(b.confidence for b in contributors),
            classification=contributors[0].classification,
        )
        concepts.append(concept)
        graph.add_unified_concept(concept)

    # Single-contributor unified concepts (standalone blocks)
    for block in blocks:
        if block.block_id in grouped_ids:
            continue  # Already handled in a group

        concept_id = f"uc_{block.block_id}"
        concept = UnifiedConcept(
            concept_id=concept_id,
            title=block.title,
            block_type=block.block_type,
            body_raw=block.body,
            body_synthesized=None,
            contributor_ids=[block.block_id],
            source_stills=[block.source_still],
            source_pages=[block.source_page],
            confidence=block.confidence,
            classification=block.classification,
        )
        concepts.append(concept)
        graph.add_unified_concept(concept)

    return concepts


def find_cross_still_links(
    blocks: list[ConceptBlock],
    vector_store: VectorStore,
    similarity_threshold: float = 0.75,
    k_neighbors: int = 10,
) -> list[tuple[str, str, float]]:
    """Find cross-still references via approximate nearest neighbors (hnswlib).

    Builds an hnswlib index from all blocks in OTHER stills (loaded from the
    vector store), then queries each new block's K nearest neighbors. Filters
    pairs above the similarity threshold and limits to 5 links per block.

    This replaces the O(N_new × N_other) dense matrix with O(N_new × log N_other)
    ANN queries, preventing segfaults on large codebases.

    Args:
        blocks: Blocks from a specific still.
        vector_store: Store containing blocks from ALL stills.
        similarity_threshold: Cosine similarity threshold for linking.
        k_neighbors: Number of nearest neighbors to query per block (default 10).

    Returns:
        List of (block_id_a, block_id_b, similarity) tuples where a and b
        are from different stills.
    """
    if not blocks:
        return []

    # Check if cross-still is possible
    rows = vector_store.conn.execute(
        "SELECT DISTINCT still_name FROM vectors"
    ).fetchall()
    available_stills = {r[0] for r in rows}
    if len(available_stills) < 2:
        return []

    # Get this still's name
    source_still = blocks[0].source_still

    # Load embeddings from OTHER stills only
    other_rows = vector_store.conn.execute(
        "SELECT block_id, embedding FROM vectors WHERE still_name != ?",
        (source_still,),
    ).fetchall()
    if not other_rows:
        return []

    # Build new-block matrix
    new_embedded = [b for b in blocks if b.embedding is not None]
    if not new_embedded:
        return []

    new_ids = [b.block_id for b in new_embedded]
    new_matrix = np.array([b.embedding for b in new_embedded], dtype=np.float32)
    dim = new_matrix.shape[1]

    # Build other-stills matrix
    other_ids = []
    other_vecs = []
    for block_id, emb_blob in other_rows:
        vec = np.frombuffer(emb_blob, dtype=np.float32)
        if len(vec) == dim:
            other_ids.append(block_id)
            other_vecs.append(vec)

    if not other_vecs:
        return []

    import hnswlib

    n_other = len(other_ids)
    other_matrix = np.array(other_vecs, dtype=np.float32)

    # Normalize both matrices for cosine similarity via inner product
    new_norms = np.linalg.norm(new_matrix, axis=1, keepdims=True)
    new_norms[new_norms == 0] = 1e-10
    new_normed = new_matrix / new_norms

    other_norms = np.linalg.norm(other_matrix, axis=1, keepdims=True)
    other_norms[other_norms == 0] = 1e-10
    other_normed = other_matrix / other_norms

    # Build hnswlib index from other-stills vectors
    index = hnswlib.Index(space="ip", dim=dim)
    index.init_index(max_elements=n_other, ef_construction=200, M=32)
    index.add_items(other_normed, list(range(n_other)))

    # Set ef for search quality
    index.set_ef(max(50, k_neighbors * 2))

    # Query K nearest neighbors for each new block
    effective_k = min(k_neighbors, n_other)
    labels, distances = index.knn_query(new_normed, k=effective_k)

    # Collect links (limit 5 per new block)
    max_links_per_block = 5
    links: list[tuple[str, str, float]] = []

    for i in range(len(new_ids)):
        block_links = 0
        for j_idx in range(effective_k):
            if block_links >= max_links_per_block:
                break
            neighbor_idx = int(labels[i][j_idx])
            # similarity = 1 - distance (for ip space with normalized vecs)
            similarity = 1.0 - float(distances[i][j_idx])
            if similarity >= similarity_threshold:
                links.append((new_ids[i], other_ids[neighbor_idx], similarity))
                block_links += 1

    return links


# ── Materialization (Graph → Spec Files) ─────────────────────────────────────


# Manifest file → block types mapping per still.
# Keys are "{still_name}/{filename}" or just "{filename}" for defaults.
# Value is a list of block_type strings, or:
#   "__ALL__"  → all blocks for this still
#   "__DEDUP__" → only blocks that are members of duplicate groups
#   "__UNIQUE__" → only blocks NOT in any duplicate group
STILL_MANIFEST_MAP: dict[str, list[str]] = {
    # ── data ──
    "data/persistent_objects.md": ["entity"],
    "data/business_invariants.md": ["invariant", "rule"],
    "data/data_obligations.md": ["obligation"],
    "data/data_boundaries.md": ["boundary"],
    # ── domain_language ──
    "domain_language/concept_registry.md": ["entity"],
    "domain_language/naming_conflicts.md": ["__DEDUP__"],
    "domain_language/excluded_terms.md": ["finding", "observation"],
    "domain_language/domain_map.md": ["rule", "invariant", "event", "state", "workflow", "command"],
    # ── interface ──
    "interface/contract_registry.md": ["interface"],
    "interface/obligation_assessment.md": ["obligation", "invariant"],
    "interface/capability_map.md": ["entity", "rule"],
    # ── rule ──
    "rule/business_rule_catalog.md": ["rule", "invariant"],
    "rule/rule_dependencies.md": ["entity"],
    "rule/gap_analysis.md": ["finding", "observation"],
    # ── state ──
    "state/lifecycle_registry.md": ["entity", "state"],
    "state/transition_specs.md": ["rule", "invariant"],
    "state/cross_entity_dependencies.md": ["event", "workflow"],
    # ── permission ──
    "permission/role_registry.md": ["entity", "permission"],
    "permission/capability_matrix.md": ["rule", "invariant"],
    "permission/boundary_spec.md": ["boundary", "obligation"],
    # ── exception ──
    "exception/exception_catalog.md": ["entity", "exception"],
    "exception/override_map.md": ["rule"],
    "exception/disposition.md": ["invariant", "obligation"],
    # ── workflow ──
    "workflow/workflow_registry.md": ["entity", "workflow"],
    "workflow/workflow_dependencies.md": ["rule", "event"],
    "workflow/actor_matrix.md": ["permission"],
    "workflow/gap_analysis.md": ["finding", "observation"],
    # ── test ──
    "test/protected_behaviors.md": ["entity", "invariant"],
    "test/coverage_gaps.md": ["finding", "observation"],
    "test/priority_map.md": ["rule"],
}


def _get_manifest_types(still_name: str, filename: str) -> Optional[list[str]]:
    """Look up the block types for a manifest file.

    Checks still-specific key first, then falls back to None (= all blocks).
    """
    key = f"{still_name}/{filename}"
    if key in STILL_MANIFEST_MAP:
        return STILL_MANIFEST_MAP[key]
    return None


def materialize_manifest_files(
    graph: KnowledgeGraph,
    blocks: list[ConceptBlock],
    still_name: str,
    manifest_path: str,
    output_dir: str,
) -> dict[str, int]:
    """Materialize per-manifest spec files from the knowledge graph.

    Reads the manifest to know which files to produce. For each file,
    queries the graph for blocks of the relevant type, deduplicates
    (picks highest-confidence from each same_as group), and writes
    the output with canonical formatting.

    Args:
        graph: The populated KnowledgeGraph.
        blocks: All blocks for this still (for body access).
        still_name: Name of the still.
        manifest_path: Path to the manifest file listing expected outputs.
        output_dir: Directory to write materialized files.

    Returns:
        Dict of filename → concept count written.
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Read manifest
    manifest_files = []
    if Path(manifest_path).exists():
        manifest_files = [
            line.strip() for line in Path(manifest_path).read_text().splitlines()
            if line.strip()
        ]
    else:
        # No manifest file — produce a single file with all blocks
        manifest_files = [f"{still_name}_all.md"]

    # Build block lookup by ID
    block_map = {b.block_id: b for b in blocks}

    # Get duplicate groups to pick canonical representatives
    dup_groups = graph.duplicate_groups()
    # Map: block_id → canonical block_id (highest confidence in group)
    canonical_map: dict[str, str] = {}
    for group in dup_groups:
        # Pick the one with highest confidence, breaking ties by body length
        best = max(
            group,
            key=lambda bid: (
                block_map[bid].confidence if bid in block_map else 0,
                block_map[bid].body.__len__() if bid in block_map else 0,
            ),
        )
        for bid in group:
            canonical_map[bid] = best

    stats: dict[str, int] = {}

    for filename in manifest_files:
        if filename == "README.md":
            # Generate index/summary
            _write_readme(graph, blocks, still_name, output_path / filename, stats)
            continue

        target_types = _get_manifest_types(still_name, filename)

        if target_types is None:
            # No mapping found — write ALL blocks for this still
            candidate_ids = graph.nodes_by_still(still_name)
        elif target_types == ["__DEDUP__"]:
            # Only blocks that are members of duplicate groups
            dup_block_ids = set()
            for group in dup_groups:
                dup_block_ids.update(group)
            candidate_ids = [bid for bid in graph.nodes_by_still(still_name) if bid in dup_block_ids]
        elif target_types == ["__UNIQUE__"]:
            # Only blocks NOT in any duplicate group
            dup_block_ids = set()
            for group in dup_groups:
                dup_block_ids.update(group)
            candidate_ids = [bid for bid in graph.nodes_by_still(still_name) if bid not in dup_block_ids]
        else:
            # Collect blocks of the right type(s) for this still
            candidate_ids = []
            for btype in target_types:
                candidate_ids.extend(graph.nodes_by_type(btype, still=still_name))

        # Deduplicate: skip non-canonical members of duplicate groups
        seen_canonical: set[str] = set()
        deduped_ids: list[str] = []
        for bid in candidate_ids:
            canon = canonical_map.get(bid, bid)
            if canon not in seen_canonical:
                seen_canonical.add(canon)
                deduped_ids.append(canon)

        # Sort by source page for stable output
        deduped_ids.sort(
            key=lambda bid: block_map[bid].source_page if bid in block_map else ""
        )

        # Write the file
        file_path = output_path / filename
        with open(file_path, "w", encoding="utf-8") as f:
            # Title is the subject only. Prefixing the still name ("# data — Business
            # Invariants") leaked an internal extraction-pass name into a heading the
            # customer reads, because the master blend demotes this H1 into a visible H3.
            # Which still produced a file is already recorded by its path on disk.
            f.write(f"# {filename.replace('.md', '').replace('_', ' ').title()}\n\n")
            f.write(f"<!-- still: {still_name} | concepts: {len(deduped_ids)} | "
                    f"generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} -->\n\n")
            f.write("---\n\n")

            for bid in deduped_ids:
                if bid in block_map:
                    f.write(block_map[bid].body)
                    f.write("\n\n")

        stats[filename] = len(deduped_ids)

    return stats


def _write_readme(
    graph: KnowledgeGraph,
    blocks: list[ConceptBlock],
    still_name: str,
    readme_path: Path,
    stats: dict[str, int],
) -> None:
    """Write a README.md summarizing the still output."""
    dup_groups = graph.duplicate_groups()
    still_nodes = graph.nodes_by_still(still_name)

    # Count by type
    type_counts: dict[str, int] = {}
    for nid in still_nodes:
        node = graph.get_node(nid)
        if node:
            btype = node.get("block_type", "unknown")
            type_counts[btype] = type_counts.get(btype, 0) + 1

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(f"# {still_name} Still — Knowledge Graph Output\n\n")
        f.write(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n\n")
        f.write("## Statistics\n\n")
        f.write(f"| Metric | Value |\n|--------|-------|\n")
        f.write(f"| Total concepts | {len(still_nodes)} |\n")
        f.write(f"| Duplicate groups | {len(dup_groups)} |\n")
        f.write(f"| Unique after dedup | {len(still_nodes) - sum(len(g) - 1 for g in dup_groups)} |\n")
        f.write(f"| Graph edges | {graph.edge_count} |\n\n")

        f.write("## Concepts by Type\n\n")
        f.write("| Type | Count |\n|------|-------|\n")
        for btype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            f.write(f"| {btype} | {count} |\n")
        f.write("\n")

        if stats:
            f.write("## Output Files\n\n")
            f.write("| File | Concepts |\n|------|----------|\n")
            for fname, count in stats.items():
                if fname != "README.md":
                    f.write(f"| `{fname}` | {count} |\n")
            f.write("\n")

        f.write("## Architecture\n\n")
        f.write("This output was produced by the knowledge graph layer:\n\n")
        f.write("1. **Parse**: Raw concat → individual concept blocks\n")
        f.write("2. **Embed**: all-mpnet-base-v2 (768-dim, local)\n")
        f.write("3. **Graph**: NetworkX directed graph (nodes=concepts, edges=relationships)\n")
        f.write("4. **Dedup**: KNN cosine similarity (threshold=0.85) → same_as edges\n")
        f.write("5. **Materialize**: Graph queries → per-manifest spec files\n")


# ── Orchestrator ─────────────────────────────────────────────────────────────


def process_still(
    concat_path: str,
    still_name: str,
    manifest_path: str,
    output_dir: str,
    graph_path: str = "",
    vector_db_path: str = "",
    dedup_threshold: float = 0.85,
    cross_still_threshold: float = 0.75,
) -> dict:
    """Full pipeline: parse → embed → graph → dedup → materialize.

    This is the main entry point for the knowledge layer.

    Args:
        concat_path: Path to {still}_raw_concat.md.
        still_name: Name of the still (e.g. "data").
        manifest_path: Path to manifest file listing expected outputs.
        output_dir: Directory for materialized spec files.
        graph_path: Path for the NetworkX graph JSON (created/appended).
        vector_db_path: Path for the SQLite vector store.
        dedup_threshold: Cosine similarity threshold for same_as.
        cross_still_threshold: Threshold for cross-still links.

    Returns:
        Dict with stats (blocks_parsed, blocks_embedded, duplicates_found, etc.)
    """
    # Default paths
    output_p = Path(output_dir)
    output_p.mkdir(parents=True, exist_ok=True)

    if not graph_path:
        graph_path = str(output_p / "knowledge_graph.json")
    if not vector_db_path:
        # Use a shared vector store at the stills level for cross-still linking.
        # Walk up from output_dir to find the "stills" directory.
        # Expected structure: .../stills/{still_name}/final_v2/ → shared at .../stills/
        candidate = output_p.parent
        while candidate != candidate.parent:
            if candidate.name == "stills":
                vector_db_path = str(candidate / "shared_vectors.db")
                break
            candidate = candidate.parent
        else:
            # Fallback: put it in the output dir
            vector_db_path = str(output_p / "vectors.db")

    print(f"[knowledge] Processing still: {still_name}")
    print(f"  concat: {concat_path}")
    print(f"  output: {output_dir}")

    # Step 1: Parse
    t0 = time.time()
    blocks = parse_raw_concat(concat_path, still_name)
    parse_time = time.time() - t0
    print(f"  Parsed {len(blocks)} concept blocks ({parse_time:.1f}s)")

    if not blocks:
        return {"error": "No blocks parsed from concat", "blocks_parsed": 0}

    # Step 2: Embed
    t0 = time.time()
    embed_blocks(blocks)
    embed_time = time.time() - t0
    print(f"  Embedded {len(blocks)} blocks ({embed_time:.1f}s)")

    # Step 3: Store in vector DB
    vector_store = VectorStore(vector_db_path)
    inserted = vector_store.upsert_batch(blocks)
    print(f"  Stored {inserted} vectors in {vector_db_path}")

    # Step 4: Build/append graph
    graph = KnowledgeGraph(graph_path)
    for b in blocks:
        graph.add_block(b)

    # Step 5: Dedup (within same still)
    t0 = time.time()
    duplicates = find_duplicates(blocks, vector_store, dedup_threshold)
    for bid_a, bid_b, score in duplicates:
        graph.add_edge(bid_a, bid_b, "same_as", score)
    dedup_time = time.time() - t0
    print(f"  Found {len(duplicates)} duplicate pairs ({dedup_time:.1f}s)")

    # Step 6: Create unified concepts (every block → exactly one unified_concept)
    t0 = time.time()
    unified_concepts = create_unified_concepts(blocks, graph)
    uc_time = time.time() - t0
    multi_contrib = sum(1 for uc in unified_concepts if uc.contributor_count > 1)
    print(f"  Created {len(unified_concepts)} unified concepts "
          f"({multi_contrib} merged, {len(unified_concepts) - multi_contrib} standalone) ({uc_time:.1f}s)")

    # Step 6b: Store unified concepts in vector store (full body_raw preserved)
    uc_stored = vector_store.upsert_unified_concepts_batch(unified_concepts)
    print(f"  Stored {uc_stored} unified concepts in vector store")

    # Step 7: Cross-still links (only if other stills exist in the store)
    cross_links = find_cross_still_links(blocks, vector_store, cross_still_threshold)
    for bid_a, bid_b, score in cross_links:
        graph.add_edge(bid_a, bid_b, "references", score)
    print(f"  Found {len(cross_links)} cross-still links")

    # Step 8: Save graph
    graph.save(graph_path)

    # Step 9: Materialize per-manifest files
    stats = materialize_manifest_files(graph, blocks, still_name, manifest_path, output_dir)
    print(f"  Materialized {len(stats)} files: {stats}")

    # Write completion marker for this still's knowledge phase
    marker = output_p / ".complete"
    marker.write_text(json.dumps({
        "still": still_name,
        "blocks_parsed": len(blocks),
        "unified_concepts": len(unified_concepts),
        "files_materialized": len(stats),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }))

    vector_store.close()

    return {
        "blocks_parsed": len(blocks),
        "blocks_embedded": inserted,
        "duplicates_found": len(duplicates),
        "unified_concepts": len(unified_concepts),
        "unified_multi_contributor": multi_contrib,
        "cross_still_links": len(cross_links),
        "graph_nodes": graph.node_count,
        "graph_edges": graph.edge_count,
        "files_materialized": stats,
        "graph_path": graph_path,
        "vector_db_path": vector_db_path,
    }


# ── CLI Entry Point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 4:
        print("Usage: python3 knowledge.py <concat_path> <still_name> <output_dir> [manifest_path] [graph_path]")
        print()
        print("Example:")
        print("  python3 knowledge.py \\")
        print("    output/stills/data/data_raw_concat.md \\")
        print("    data \\")
        print("    output/stills/data/final \\")
        print("    runtimes/pipeline/manifests/data.txt \\")
        print("    output/stills/knowledge_graph.json")
        sys.exit(1)

    concat = sys.argv[1]
    still = sys.argv[2]
    out_dir = sys.argv[3]
    manifest = sys.argv[4] if len(sys.argv) > 4 else ""
    graph = sys.argv[5] if len(sys.argv) > 5 else ""

    result = process_still(
        concat_path=concat,
        still_name=still,
        manifest_path=manifest,
        output_dir=out_dir,
        graph_path=graph,
    )
    print(f"\nResult: {json.dumps(result, indent=2)}")
