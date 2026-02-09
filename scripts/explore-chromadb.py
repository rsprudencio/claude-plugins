#!/usr/bin/env python3
"""Interactive ChromaDB Explorer for Jarvis.

Provides a simple CLI to browse collections, view documents, and search.
Safe to use while Claude Code is running (read-only operations).
"""
import json
import os
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

try:
    import chromadb
    from chromadb.config import Settings
except ImportError:
    print("Error: chromadb not installed. Run: pip install chromadb")
    sys.exit(1)


@contextmanager
def auto_pager(enabled=True):
    """Pipe stdout through a pager (like less) when output goes to a terminal.

    Mimics git's pager behavior: auto-pages on TTY, passthrough when piped.
    Respects $PAGER env var; defaults to 'less -RFX'.
    """
    use_pager = enabled and sys.stdout.isatty()
    if not use_pager:
        yield
        return

    pager_cmd = os.environ.get("PAGER", "less -RFX")
    process = None
    old_stdout = sys.stdout
    old_sigpipe = None

    try:
        process = subprocess.Popen(
            pager_cmd,
            shell=True,
            stdin=subprocess.PIPE,
            text=True,
        )
        # Ignore SIGPIPE so we don't crash when user quits pager early
        old_sigpipe = signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        sys.stdout = process.stdin
        yield
    except BrokenPipeError:
        # User quit pager before all output was written — that's fine
        pass
    finally:
        sys.stdout = old_stdout
        if old_sigpipe is not None:
            signal.signal(signal.SIGPIPE, old_sigpipe)
        if process:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            process.wait()


class ChromaDBExplorer:
    """Interactive ChromaDB explorer with read-only operations."""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path).expanduser()
        if not self.db_path.exists():
            print(f"Error: Database not found at {self.db_path}")
            sys.exit(1)

        self.client = chromadb.PersistentClient(
            path=str(self.db_path),
            settings=Settings(anonymized_telemetry=False)
        )

    def list_collections(self):
        """List all collections with document counts."""
        collections = self.client.list_collections()

        print(f"\n{'='*60}")
        print(f"ChromaDB Database: {self.db_path}")
        print(f"{'='*60}\n")

        if not collections:
            print("No collections found.")
            return

        print(f"Collections: {len(collections)}\n")

        for coll in collections:
            count = coll.count()
            print(f"  📦 {coll.name}")
            print(f"     Documents: {count}")
            print(f"     Metadata: {coll.metadata}")
            print()

    # Sort key extractors: each returns a comparable value for a result row.
    # Numeric fields use _safe_float to handle missing/string values like "N/A".
    @staticmethod
    def _safe_float(val, default=0.0):
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    SORT_KEYS = {
        "importance": lambda r: ChromaDBExplorer._safe_float(r["metadata"].get("importance_score")),
        "size":       lambda r: len(r["document"]) if r["document"] else 0,
        "type":       lambda r: str(r["metadata"].get("type", "")),
        "tier":       lambda r: str(r["metadata"].get("tier", "")),
        "created":    lambda r: str(r["metadata"].get("created_at", "")),
        "id":         lambda r: r["id"],
        "relevance":  lambda r: ChromaDBExplorer._safe_float(r.get("relevance")),
    }

    @staticmethod
    def _to_rows(ids, metadatas, documents, distances=None):
        """Unpack ChromaDB parallel arrays into a list of row dicts."""
        rows = []
        for i, doc_id in enumerate(ids):
            row = {
                "id": doc_id,
                "metadata": metadatas[i] if metadatas else {},
                "document": documents[i] if documents else "",
            }
            if distances is not None:
                row["relevance"] = (1 - distances[i]) if distances[i] is not None else None
            rows.append(row)
        return rows

    @classmethod
    def _sort_rows(cls, rows, sort_key: Optional[str], reverse: bool):
        """Sort rows in-place if a sort key is given."""
        if not sort_key:
            return rows
        key_fn = cls.SORT_KEYS.get(sort_key)
        if not key_fn:
            valid = ", ".join(cls.SORT_KEYS)
            print(f"Warning: unknown sort key '{sort_key}'. Valid keys: {valid}")
            return rows
        rows.sort(key=key_fn, reverse=reverse)
        return rows

    @staticmethod
    def _oneline(index: int, row: dict):
        """Format a single document as a compact one-liner.

        Layout: IDX  [REL]  ID  [type|tier]  size  "preview..."
        The preview fills whatever terminal width remains.
        """
        term_width = shutil.get_terminal_size((120, 24)).columns
        metadata = row["metadata"]
        document = row["document"]

        # Build the fixed-width prefix
        idx = f"{index:>3}"
        rel = f" {row['relevance']:.3f}" if row.get("relevance") is not None else ""
        doc_type = metadata.get("type", "?")
        tier = metadata.get("tier", "?")
        tag = f"[{doc_type}|{tier}]"
        imp = metadata.get("importance_score")
        imp_str = f" i:{imp}" if imp is not None and imp != "N/A" else ""
        chars = f" {len(document)}c" if document else ""

        prefix = f"{idx}{rel}  {row['id']}  {tag}{imp_str}{chars}  "

        # Fill remaining width with a content preview
        remaining = term_width - len(prefix) - 2  # 2 for quotes
        if remaining > 10 and document:
            # Collapse whitespace for a clean single line
            flat = " ".join(document.split())
            if len(flat) > remaining:
                preview = flat[:remaining - 1] + "\u2026"
            else:
                preview = flat
            print(f"{prefix}\"{preview}\"")
        else:
            print(prefix.rstrip())

    @staticmethod
    def _verbose(index: int, row: dict, full: bool = False):
        """Format a single document in the multi-line verbose view."""
        metadata = row["metadata"]
        document = row["document"]

        doc_type = metadata.get("type", "unknown")
        namespace = metadata.get("namespace", "unknown")
        importance = metadata.get("importance_score", "N/A")
        topics = metadata.get("topics", "")
        created_at = metadata.get("created_at", "")
        tier = metadata.get("tier", "unknown")

        print(f"{index}. ID: {row['id']}")

        if row.get("relevance") is not None:
            print(f"   Relevance: {row['relevance']:.3f}")

        print(f"   Type: {doc_type} | Namespace: {namespace} | Tier: {tier}")

        if importance != "N/A":
            print(f"   Importance: {importance}")
        if topics:
            print(f"   Topics: {topics}")
        if created_at:
            print(f"   Created: {created_at}")

        if document:
            if full:
                print(f"   Content: {document}")
            else:
                preview = document[:150] + "..." if len(document) > 150 else document
                print(f"   Content: {preview}")

        print()

    def show_collection(self, name: str = "jarvis", limit: int = 20,
                        full: bool = False, oneline: bool = False,
                        sort_key: Optional[str] = None, reverse: bool = False):
        """Show documents in a collection."""
        try:
            coll = self.client.get_collection(name)
        except Exception as e:
            print(f"Error: Collection '{name}' not found. {e}")
            return

        count = coll.count()

        if not oneline:
            print(f"\n{'='*60}")
            print(f"Collection: {name} ({count} documents)")
            print(f"{'='*60}\n")

        if count == 0:
            print("No documents in collection.")
            return

        results = coll.get(limit=limit, include=["documents", "metadatas"])
        rows = self._to_rows(results["ids"], results["metadatas"], results["documents"])
        self._sort_rows(rows, sort_key, reverse)

        for i, row in enumerate(rows, 1):
            if oneline:
                self._oneline(i, row)
            else:
                self._verbose(i, row, full=full)

        if count > limit:
            print(f"Showing {limit} of {count} documents. Use --limit to see more.")

    def show_document(self, doc_id: str, collection: str = "jarvis"):
        """Show full details of a single document."""
        try:
            coll = self.client.get_collection(collection)
        except Exception as e:
            print(f"Error: Collection '{collection}' not found. {e}")
            return

        results = coll.get(ids=[doc_id], include=["documents", "metadatas", "embeddings"])

        if not results["ids"]:
            print(f"Document '{doc_id}' not found in collection '{collection}'")
            return

        print(f"\n{'='*60}")
        print(f"Document: {doc_id}")
        print(f"{'='*60}\n")

        metadata = results["metadatas"][0] if results["metadatas"] else {}
        document = results["documents"][0] if results["documents"] else ""

        print("Metadata:")
        for key, value in metadata.items():
            print(f"  {key}: {value}")

        print(f"\nDocument ({len(document)} chars):")
        print("-" * 60)
        print(document)
        print("-" * 60)

    def search(self, query: str, collection: str = "jarvis", n_results: int = 5,
               oneline: bool = False, sort_key: Optional[str] = None,
               reverse: bool = False):
        """Semantic search in a collection."""
        try:
            coll = self.client.get_collection(collection)
        except Exception as e:
            print(f"Error: Collection '{collection}' not found. {e}")
            return

        results = coll.query(
            query_texts=[query],
            n_results=n_results,
            include=["documents", "metadatas", "distances"]
        )

        if not oneline:
            print(f"\n{'='*60}")
            print(f"Search: '{query}' (top {n_results} results)")
            print(f"{'='*60}\n")

        if not results["ids"] or not results["ids"][0]:
            print("No results found.")
            return

        rows = self._to_rows(
            results["ids"][0], results["metadatas"][0],
            results["documents"][0], results["distances"][0],
        )
        self._sort_rows(rows, sort_key, reverse)

        for i, row in enumerate(rows, 1):
            if oneline:
                self._oneline(i, row)
            else:
                self._verbose(i, row)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Interactive ChromaDB Explorer")
    parser.add_argument(
        "--db-path",
        default="~/.jarvis/memory_db",
        help="Path to ChromaDB database (default: ~/.jarvis/memory_db)"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all collections"
    )
    parser.add_argument(
        "--show",
        metavar="COLLECTION",
        help="Show documents in a collection (default: jarvis)"
    )
    parser.add_argument(
        "--doc",
        metavar="DOC_ID",
        help="Show full document by ID"
    )
    parser.add_argument(
        "--search",
        metavar="QUERY",
        help="Semantic search in collection"
    )
    parser.add_argument(
        "--collection",
        default="jarvis",
        help="Collection name (default: jarvis)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Limit results (default: 20)"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Show full document content (no truncation)"
    )
    parser.add_argument(
        "--oneline",
        action="store_true",
        help="Compact one-line-per-document view"
    )
    parser.add_argument(
        "--sort",
        metavar="KEY",
        choices=list(ChromaDBExplorer.SORT_KEYS),
        help="Sort results by: importance, size, type, tier, created, id, relevance"
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Reverse sort order (descending)"
    )
    parser.add_argument(
        "--no-pager",
        action="store_true",
        help="Disable pager (less) for output"
    )

    args = parser.parse_args()

    explorer = ChromaDBExplorer(args.db_path)

    with auto_pager(enabled=not args.no_pager):
        if args.list:
            explorer.list_collections()
        elif args.show:
            explorer.show_collection(args.show, limit=args.limit, full=args.full,
                                     oneline=args.oneline,
                                     sort_key=args.sort, reverse=args.reverse)
        elif args.doc:
            explorer.show_document(args.doc, collection=args.collection)
        elif args.search:
            explorer.search(args.search, collection=args.collection,
                            n_results=args.limit, oneline=args.oneline,
                            sort_key=args.sort, reverse=args.reverse)
        else:
            # Default: show jarvis collection
            explorer.show_collection("jarvis", limit=args.limit, full=args.full,
                                     oneline=args.oneline,
                                     sort_key=args.sort, reverse=args.reverse)


if __name__ == "__main__":
    main()
