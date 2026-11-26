import re
from typing import Any, Protocol

from bookscout.core.contracts import ToolContext, ToolError, ToolResult


def _isbn13_check_digit(first_twelve: str) -> str:
    total = sum(int(ch) * (1 if i % 2 == 0 else 3)
                for i, ch in enumerate(first_twelve))
    return str((10 - total % 10) % 10)


def _clean_isbn(isbn: str) -> str:
    return re.sub(r"[-\s]", "", isbn).upper()


class ValidateIsbnTool:
    name = "validate_isbn"
    version = "1.0"
    input_schema = {
        "type": "object", "required": ["isbn"],
        "properties": {"isbn": {"type": "string", "maxLength": 17}},
        "additionalProperties": False,
    }

    def execute(self, input_data: dict[str, Any], context: ToolContext):
        isbn = _clean_isbn(input_data["isbn"])
        if len(isbn) == 10:
            if not re.fullmatch(r"\d{9}[\dX]", isbn):
                return ToolResult(error=ToolError("ISBN_FORMAT_INVALID",
                    "Некорректный формат ISBN-10", field="isbn"))
            checksum = sum((10 - i) * (10 if ch == "X" else int(ch))
                           for i, ch in enumerate(isbn))
            if checksum % 11:
                return ToolResult(error=ToolError("ISBN_CHECKSUM_INVALID",
                    "Контрольная сумма ISBN-10 неверна", field="isbn"))
            prefix = "978" + isbn[:9]
            return ToolResult(value={"isbn13": prefix + _isbn13_check_digit(prefix),
                                     "was_converted_from_isbn10": True})
        if len(isbn) != 13 or not isbn.isdigit() or not isbn.startswith(("978", "979")):
            return ToolResult(error=ToolError("ISBN_FORMAT_INVALID",
                "Ожидался ISBN-10 или ISBN-13", field="isbn"))
        if _isbn13_check_digit(isbn[:12]) != isbn[-1]:
            return ToolResult(error=ToolError("ISBN_CHECKSUM_INVALID",
                "Контрольная сумма ISBN-13 неверна", field="isbn"))
        return ToolResult(value={"isbn13": isbn, "was_converted_from_isbn10": False})


class ResolveBookTool:
    name = "resolve_book"
    version = "1.0"
    input_schema = {
        "type": "object", "required": ["isbn13"],
        "properties": {"isbn13": {"type": "string", "maxLength": 13}},
        "additionalProperties": False,
    }

    def execute(self, input_data: dict[str, Any], context: ToolContext):
        isbn13 = input_data["isbn13"]
        if len(isbn13) != 13 or not isbn13.isdigit():
            return ToolResult(error=ToolError("BOOK_QUERY_INVALID",
                "Нужен корректный ISBN-13", field="isbn13"))
        # Deterministic placeholder until the bibliographic API adapter is added.
        return ToolResult(value={"edition": {
            "edition_id": f"isbn:{isbn13}", "title": "Демонстрационная книга",
            "isbn13": isbn13,
        }})


class StoreAdapter(Protocol):
    source_id: str
    def search(self, edition: dict[str, Any], context: ToolContext) -> list[dict[str, Any]]: ...


class InMemoryStoreAdapter:
    source_id = "demo_store"

    def search(self, edition: dict[str, Any], context: ToolContext):
        return [{
            "source_id": self.source_id, "title": edition["title"],
            "isbn13": edition["isbn13"], "price_minor": 79900,
            "currency": "RUB", "delivery_minor": None,
            "availability": "in_stock",
            "product_url": "https://example.org/books/demo",
        }]


class SearchStoreOffersTool:
    name = "search_store_offers"
    MAX_OFFERS_PER_JOB = 1000
    version = "1.0"
    input_schema = {
        "type": "object", "required": ["edition"],
        "properties": {"edition": {"type": "object"}},
        "additionalProperties": False,
    }

    def __init__(self, adapters: list[StoreAdapter] | None = None):
        self.adapters = adapters or [InMemoryStoreAdapter()]

    def execute(self, input_data: dict[str, Any], context: ToolContext):
        edition = input_data["edition"]
        if not edition.get("edition_id") or not edition.get("isbn13"):
            return ToolResult(error=ToolError("EDITION_REQUIRED",
                "Не передано подтверждённое издание", field="edition"))
        offers, source_results = [], []
        for adapter in self.adapters:
            if context.cancellation_token.is_set():
                return ToolResult(error=ToolError("JOB_CANCELLED", "Поиск отменён"))
            try:
                raw = adapter.search(edition, context)
                bad_items = []
                truncated = False
                for index, offer in enumerate(raw):
                    if self._valid_offer(offer, edition):
                        if len(offers) < self.MAX_OFFERS_PER_JOB:
                            offers.append(offer)
                        else:
                            truncated = True
                    else:
                        bad_items.append(index)
                if bad_items:
                    source_results.append({"source_id": adapter.source_id,
                        "status": "error", "error_code": "STORE_RESPONSE_INVALID",
                        "invalid_item_indexes": bad_items, "truncated": truncated})
                else:
                    source_results.append({"source_id": adapter.source_id,
                        "status": "success", "error_code": None, "truncated": truncated})
            except TimeoutError:
                source_results.append({"source_id": adapter.source_id,
                    "status": "timeout", "error_code": "STORE_TIMEOUT"})
            except Exception:
                source_results.append({"source_id": adapter.source_id,
                    "status": "error", "error_code": "STORE_UNAVAILABLE"})
        return ToolResult(value={"offers": offers, "source_results": source_results})

    @staticmethod
    def _valid_offer(offer, edition):
        if not isinstance(offer, dict):
            return False
        price = offer.get("price_minor")
        url = offer.get("product_url", "")
        return (
            offer.get("isbn13") == edition["isbn13"]
            and isinstance(price, int) and not isinstance(price, bool)
            and 0 <= price <= 100_000_000_000
            and offer.get("currency") == "RUB"
            and isinstance(url, str) and url.startswith("https://")
            and bool(offer.get("source_id"))
        )


class NormalizeOffersTool:
    name = "normalize_offers"
    version = "1.0"
    input_schema = {
        "type": "object", "required": ["offers", "source_results"],
        "properties": {"offers": {"type": "array"},
                       "source_results": {"type": "array"}},
        "additionalProperties": False,
    }

    def execute(self, input_data: dict[str, Any], context: ToolContext):
        offers = sorted(input_data["offers"], key=lambda item: item["price_minor"])
        sources = input_data["source_results"]
        has_errors = any(item["status"] != "success" for item in sources)
        if offers and has_errors:
            outcome = "PARTIAL"
        elif offers:
            outcome = "SUCCEEDED"
        elif sources and all(item["status"] == "success" for item in sources):
            outcome = "NO_RESULTS"
        elif sources and all(item.get("error_code") == "STORE_TIMEOUT" for item in sources):
            outcome = "TIMED_OUT"
        else:
            outcome = "FAILED"
        value = {"offers": offers, "source_results": sources, "job_outcome": outcome}
        if outcome == "FAILED":
            value["failure"] = {
                "code": "STORE_SOURCES_FAILED",
                "message": "Источники не вернули пригодных предложений",
            }
        return ToolResult(value=value)
