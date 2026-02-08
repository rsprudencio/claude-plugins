#!/usr/bin/env python3
"""Interactive ChromaDB Explorer for Jarvis.

Provides a simple CLI to browse collections, view documents, and search.
Safe to use while Claude Code is running (read-only operations).
"""
import json
import sys
from pathlib import Path
from typing import Optional

try:
    import chromadb
    from chromadb.config import Settings
except ImportError:
    print("Error: chromadb not installed. Run: pip install chromadb")
    sys.exit(1)


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

    def show_collection(self, name: str = "jarvis", limit: int = 20):
        """Show documents in a collection."""
        try:
            coll = self.client.get_collection(name)
        except Exception as e:
            print(f"Error: Collection '{name}' not found. {e}")
            return

        count = coll.count()
        print(f"\n{'='*60}")
        print(f"Collection: {name} ({count} documents)")
        print(f"{'='*60}\n")

        if count == 0:
            print("No documents in collection.")
            return

        # Get all documents (or limited subset)
        results = coll.get(limit=limit, include=["documents", "metadatas"])

        for i, doc_id in enumerate(results["ids"], 1):
            metadata = results["metadatas"][i-1] if results["metadatas"] else {}
            document = results["documents"][i-1] if results["documents"] else ""

            # Extract key metadata
            doc_type = metadata.get("type", "unknown")
            namespace = metadata.get("namespace", "unknown")
            importance = metadata.get("importance_score", "N/A")
            topics = metadata.get("topics", "")
            created_at = metadata.get("created_at", "")
            tier = metadata.get("tier", "unknown")

            print(f"{i}. ID: {doc_id}")
            print(f"   Type: {doc_type} | Namespace: {namespace} | Tier: {tier}")

            if importance != "N/A":
                print(f"   Importance: {importance}")
            if topics:
                print(f"   Topics: {topics}")
            if created_at:
                print(f"   Created: {created_at}")

            # Show document preview (first 150 chars)
            if document:
                preview = document[:150] + "..." if len(document) > 150 else document
                print(f"   Content: {preview}")

            print()

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

    def search(self, query: str, collection: str = "jarvis", n_results: int = 5):
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

        print(f"\n{'='*60}")
        print(f"Search: '{query}' (top {n_results} results)")
        print(f"{'='*60}\n")

        if not results["ids"] or not results["ids"][0]:
            print("No results found.")
            return

        for i, doc_id in enumerate(results["ids"][0], 1):
            distance = results["distances"][0][i-1] if results["distances"] else None
            metadata = results["metadatas"][0][i-1] if results["metadatas"] else {}
            document = results["documents"][0][i-1] if results["documents"] else ""

            doc_type = metadata.get("type", "unknown")
            importance = metadata.get("importance_score", "N/A")

            print(f"{i}. ID: {doc_id}")
            print(f"   Relevance: {1 - distance:.3f}" if distance is not None else "   Relevance: N/A")
            print(f"   Type: {doc_type} | Importance: {importance}")

            # Show preview
            preview = document[:200] + "..." if len(document) > 200 else document
            print(f"   {preview}")
            print()


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

    args = parser.parse_args()

    explorer = ChromaDBExplorer(args.db_path)

    if args.list:
        explorer.list_collections()
    elif args.show:
        explorer.show_collection(args.show, limit=args.limit)
    elif args.doc:
        explorer.show_document(args.doc, collection=args.collection)
    elif args.search:
        explorer.search(args.search, collection=args.collection, n_results=args.limit)
    else:
        # Default: show jarvis collection
        explorer.show_collection("jarvis", limit=args.limit)


if __name__ == "__main__":
    main()
