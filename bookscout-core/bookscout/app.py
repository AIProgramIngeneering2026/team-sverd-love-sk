from bookscout.core.dispatcher import Dispatcher
from bookscout.core.registry import PipelineStep, ToolRegistry
from bookscout.core.job import JobState
from bookscout.core.repository import InMemoryJobRepository
from bookscout.tools.builtin import (
    NormalizeOffersTool, ResolveBookTool, SearchStoreOffersTool, ValidateIsbnTool,
)


def build_dispatcher():
    registry = ToolRegistry()
    for tool in (ValidateIsbnTool(), ResolveBookTool(),
                 SearchStoreOffersTool(), NormalizeOffersTool()):
        registry.register(tool)
    # Workflow is configuration; Dispatcher has no concrete Tool names.
    registry.register_workflow("books.search", [
        PipelineStep("validate_isbn", JobState.VALIDATING,
                     lambda d: {"isbn": d.get("isbn")}, JobState.RESOLVING_BOOK),
        PipelineStep("resolve_book", JobState.RESOLVING_BOOK,
                     lambda d: {"isbn13": d["validate_isbn"]["isbn13"]},
                     JobState.FETCHING_OFFERS),
        PipelineStep("search_store_offers", JobState.FETCHING_OFFERS,
                     lambda d: {"edition": d["resolve_book"]["edition"]},
                     JobState.NORMALIZING, failure_state=JobState.NORMALIZING,
                     timeout_state=JobState.TIMED_OUT),
        PipelineStep("normalize_offers", JobState.NORMALIZING,
                     lambda d: {
                         "offers": d["search_store_offers"]["offers"],
                         "source_results": d["search_store_offers"]["source_results"]},
                     success_state=None),
    ])
    return Dispatcher(registry, InMemoryJobRepository())


def main():
    dispatcher = build_dispatcher()
    job = dispatcher.dispatch("books.search", {"isbn": "9785170900008"})
    print({"job_id": job.id, "state": job.state.value,
           "result": job.result, "error": job.error_code})


if __name__ == "__main__":
    main()
