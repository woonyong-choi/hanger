from __future__ import annotations

__all__ = ["INDEX_PREFIX", "SearchIndex", "index_name"]

from collections.abc import Iterable, Iterator
from itertools import batched
from typing import Any

from elasticsearch import Elasticsearch, helpers

from hanger.errors import RequestError
from hanger.requests import digest
from hanger.scope import SearchScope

INDEX_PREFIX = "hanger-"
_FOLDER_BATCH_SIZE = 256


def index_name(closet_identity: str) -> str:
    """The index of a closet, named after its path."""
    return INDEX_PREFIX + digest(closet_identity.encode())[:24] + "-v3"


class SearchIndex:
    """The search index of one closet, named after the closet's path."""

    def __init__(self, url: str, closet_identity: str) -> None:
        self.name = index_name(closet_identity)
        self.client = Elasticsearch(url, request_timeout=20, max_retries=0)

    def close(self) -> None:
        self.client.close()

    def delete(self) -> None:
        self.client.options(ignore_status=404).indices.delete(index=self.name)

    # cost: time O(1), heap O(1), stack O(1), io O(1)
    # basis: estimate
    def ensure(self) -> None:
        if self.client.indices.exists(index=self.name):
            return
        self.client.indices.create(
            index=self.name,
            settings={
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "analysis": {
                    "analyzer": {
                        "document": {
                            "type": "custom",
                            "tokenizer": "nori_tokenizer",
                            "filter": [
                                "nori_part_of_speech",
                                "lowercase",
                                "nori_readingform",
                            ],
                        }
                    }
                },
            },
            mappings={
                "dynamic": "strict",
                "_source": {"excludes": ["content"]},
                "properties": {
                    "kind": {"type": "keyword"},
                    "path": {"type": "keyword"},
                    "hash": {"type": "keyword"},
                    "signature": {"type": "keyword"},
                    **{
                        field: {
                            "type": "text",
                            "analyzer": "document",
                            "fields": {
                                "surface": {"type": "text", "analyzer": "standard"}
                            },
                        }
                        for field in ("title", "content", "names")
                    },
                    "cursor": {"type": "keyword"},
                },
            },
        )

    # cost: time O(n), heap O(n), stack O(1), io 1
    # vars: n = requested path metadata bytes
    # basis: estimate
    def metadata(self, paths: list[str]) -> dict[str, dict[str, Any]]:
        if not paths:
            return {}
        response = self.client.mget(index=self.name, ids=[_key(p) for p in paths])
        return {
            item["_source"]["path"]: item["_source"]
            for item in response["docs"]
            if item.get("found")
        }

    # cost: time O(n), heap O(b), stack O(1), io O(n/b)
    # vars: n = indexed metadata bytes, b = server scroll batch bytes
    # basis: estimate
    def entries(self) -> Iterator[dict[str, Any]]:
        for hit in helpers.scan(
            self.client,
            index=self.name,
            query={"query": {"term": {"kind": "document"}}},
            size=256,
        ):
            yield hit["_source"]

    # cost: time O(n), heap O(b), stack O(1), io O(n/b)
    # vars: n = indexed payload bytes, b = bulk chunk bytes
    # basis: estimate
    def write(self, entries: Iterable[dict[str, Any]]) -> None:
        def actions() -> Iterator[dict[str, Any]]:
            for entry in entries:
                action: dict[str, Any] = {
                    "_index": self.name,
                    "_id": _key(entry["path"]),
                    "_op_type": "index" if "hash" in entry else "delete",
                }
                if "hash" in entry:
                    action["_source"] = entry
                yield action

        helpers.bulk(
            self.client,
            actions(),
            chunk_size=256,
            max_chunk_bytes=4 * 1024 * 1024,
            ignore_status=(404,),
        )

    # cost: time O(1), heap O(1), stack O(1), io 1
    # basis: estimate
    def refresh(self) -> None:
        self.client.indices.refresh(index=self.name)

    def find(
        self, query: str, limit: int, *, folders: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        scope = SearchScope(folders)
        if len(scope.folders) <= _FOLDER_BATCH_SIZE:
            return self._find(query, limit, scope.folders)
        pit = {
            "id": self.client.open_point_in_time(index=self.name, keep_alive="1m")[
                "id"
            ],
            "keep_alive": "1m",
        }
        try:
            results: list[dict[str, Any]] = []
            for batch in batched(scope.folders, _FOLDER_BATCH_SIZE):
                results.extend(self._find(query, limit, batch, pit=pit))
                results.sort(key=lambda hit: (-hit["score"], hit["path"]))
                del results[limit:]
            return results
        finally:
            self.client.close_point_in_time(id=pit["id"])

    # cost: time O(q), heap O(q), stack O(1), io 2
    # vars: q = query bytes
    # basis: estimate
    def analyze(self, text: str) -> list[str]:
        """Return the query terms the index matches, longest first."""
        tokens: set[str] = set()
        for analyzer in ("document", "standard"):
            response = self.client.indices.analyze(
                index=self.name, analyzer=analyzer, text=text
            )
            tokens.update(item["token"] for item in response["tokens"])
        return sorted((t for t in tokens if t.strip()), key=len, reverse=True)

    # cost: time O(k), heap O(k), stack O(1), io O(1)
    # vars: k = audit batch metadata bytes
    # basis: estimate
    def audit_entries(self, limit: int) -> list[dict[str, Any]]:
        state = self.client.options(ignore_status=404).get(index=self.name, id="audit")
        cursor = state.get("_source", {}).get("cursor", "")
        response = self.client.search(
            index=self.name,
            size=limit,
            sort=[{"path": "asc"}],
            query={
                "bool": {
                    "filter": [
                        {"term": {"kind": "document"}},
                        {"range": {"path": {"gt": cursor}}},
                    ]
                }
            },
        )
        return [hit["_source"] for hit in response["hits"]["hits"]]

    # cost: time O(1), heap O(1), stack O(1), io 1
    # basis: estimate
    def save_audit_cursor(self, path: str) -> None:
        self.client.index(
            index=self.name, id="audit", document={"kind": "state", "cursor": path}
        )

    def _find(
        self,
        query: str,
        limit: int,
        folders: tuple[str, ...],
        *,
        pit: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        filters: list[dict[str, Any]] = [{"term": {"kind": "document"}}]
        if folders:
            filters.append(
                {
                    "bool": {
                        "should": [
                            {"prefix": {"path": folder + "/"}} for folder in folders
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )
        target: dict[str, Any] = {"index": self.name} if pit is None else {"pit": pit}
        response = self.client.search(
            **target,
            size=limit,
            sort=[{"_score": "desc"}, {"path": "asc"}],
            query={
                "bool": {
                    "filter": filters,
                    "should": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["title^3", "content"],
                                "fuzziness": "AUTO",
                                "_name": "text",
                            }
                        },
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["title.surface^3", "content.surface"],
                                "_name": "surface_text",
                            }
                        },
                        # Keyword tree names: other names and the model's name
                        # for a note, weighed lightly so text matches lead.
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["names", "names.surface"],
                                "boost": 0.5,
                                "_name": "names",
                            }
                        },
                    ],
                    "minimum_should_match": 1,
                }
            },
        )
        if pit is not None and "pit_id" in response:
            pit["id"] = response["pit_id"]
        if response.get("timed_out") or response.get("_shards", {}).get("failed", 0):
            raise RequestError(
                "The search server could not search the whole requested scope."
            )
        return [
            dict(
                hit["_source"],
                score=hit["_score"],
                matched_by=hit.get("matched_queries", []),
            )
            for hit in response["hits"]["hits"]
        ]


def _key(path: str) -> str:
    return digest(path.encode())
