from __future__ import annotations

__all__ = ["SearchIndex"]

from collections.abc import Iterable, Iterator
from typing import Any

from elasticsearch import Elasticsearch, helpers

from markchain.errors import RequestError
from markchain.requests import digest
from markchain.scope import SearchScope


class SearchIndex:
    def __init__(self, url: str, vault_identity: str) -> None:
        self.name = "markchain-" + digest(vault_identity.encode())[:24] + "-v1"
        self.client = Elasticsearch(url, request_timeout=20, max_retries=0)

    def close(self) -> None:
        self.client.close()

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
                    "title": {"type": "text", "analyzer": "document"},
                    "content": {"type": "text", "analyzer": "document"},
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
            if item["found"]
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

    # cost: time O(k+s), heap O(k+s), stack O(1), io 1
    # vars: k = result metadata bytes, s = scope bytes; server cost depends on postings
    # basis: estimate
    def find(
        self, query: str, limit: int, *, folders: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        scope = SearchScope(folders)
        filters: list[dict[str, Any]] = [{"term": {"kind": "document"}}]
        if scope.folders:
            filters.append(
                {
                    "bool": {
                        "should": [
                            {"prefix": {"path": folder + "/"}}
                            for folder in scope.folders
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )
        response = self.client.search(
            index=self.name,
            size=limit,
            query={
                "bool": {
                    "filter": filters,
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": ["title^3", "content"],
                                "fuzziness": "AUTO",
                            }
                        }
                    ],
                }
            },
        )
        if response.get("timed_out") or response.get("_shards", {}).get("failed", 0):
            raise RequestError("search did not evaluate the complete requested scope")
        return [
            dict(hit["_source"], score=hit["_score"])
            for hit in response["hits"]["hits"]
        ]

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


def _key(path: str) -> str:
    return digest(path.encode())
