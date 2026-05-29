"""EDGAR RAG provider — the reference implementation onboarded onto MCP-Host.

This is the canonical template the SDK generalizes from the local edgar-rag project. The real
deployment reads from a LanceDB artifact (the `vectors` artifact) and an `edgar.*` Postgres
schema; here we ship a tiny embedded corpus + naive keyword ranking so the provider runs and
is testable without external data. Swapping in LanceDB means changing only the bodies below —
the manifest, transport, auth, billing and metering are all the host's.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mcp_host.sdk import ErrorCode, Provider, ToolError, tool

# Tiny embedded corpus (stand-in for the LanceDB `vectors` artifact).
_CORPUS = [
    {"accession": "0000320193-23-000106", "company": "APPLE INC", "cik": "320193",
     "filing_type": "10-K", "filing_date": "2023-11-03", "section": "Item 1A - Risk Factors",
     "text": "The Company's business is subject to global supply chain and component risks."},
    {"accession": "0000320193-23-000106", "company": "APPLE INC", "cik": "320193",
     "filing_type": "10-K", "filing_date": "2023-11-03", "section": "Item 7 - MD&A",
     "text": "Net sales increased driven by Services revenue and iPhone demand."},
    {"accession": "0001045810-24-000029", "company": "NVIDIA CORP", "cik": "1045810",
     "filing_type": "10-K", "filing_date": "2024-02-21", "section": "Item 1A - Risk Factors",
     "text": "Demand for data center GPUs may fluctuate with AI infrastructure spending."},
    {"accession": "0001318605-24-000012", "company": "TESLA INC", "cik": "1318605",
     "filing_type": "8-K", "filing_date": "2024-01-24", "section": "Results",
     "text": "Vehicle deliveries and energy storage deployments grew year over year."},
]


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=20)
    company: str | None = Field(default=None, max_length=200)
    filing_type: str | None = Field(default=None, max_length=10)


class GetFilingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accession_number: str = Field(min_length=1, max_length=40)


class EdgarRagProvider(Provider):
    manifest_path = "provider.json"

    @tool("search_filings", input_model=SearchInput)
    def search_filings(self, ctx, query: str, top_k: int = 5, company: str | None = None,
                       filing_type: str | None = None):
        terms = {w for w in query.lower().split() if len(w) > 2}
        rows = _CORPUS
        if company:
            rows = [r for r in rows if company.upper() in r["company"]]
        if filing_type:
            rows = [r for r in rows if r["filing_type"] == filing_type]
        scored = []
        for r in rows:
            hay = r["text"].lower()
            score = sum(1 for t in terms if t in hay)
            if score:
                scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        hits = [{"score": s, **r} for s, r in scored[:top_k]]
        return ctx.json_text({"query": query, "results": hits, "count": len(hits)})

    @tool("list_companies")
    def list_companies(self, ctx):
        agg: dict[str, dict] = {}
        for r in _CORPUS:
            a = agg.setdefault(r["cik"], {"company": r["company"], "cik": r["cik"], "passages": 0})
            a["passages"] += 1
        return ctx.json_text({"companies": sorted(agg.values(), key=lambda x: x["company"])})

    @tool("get_filing", input_model=GetFilingInput)
    def get_filing(self, ctx, accession_number: str):
        passages = [r for r in _CORPUS if r["accession"] == accession_number]
        if not passages:
            raise ToolError(ErrorCode.TOOL_NOT_FOUND, f"No filing {accession_number}")
        return ctx.json_text({"accession_number": accession_number, "passages": passages})

    @tool("get_data_catalog")
    def get_data_catalog(self, ctx):
        return ctx.json_text(self.catalog(ctx))

    def catalog(self, ctx):
        companies = sorted({r["company"] for r in _CORPUS})
        types = sorted({r["filing_type"] for r in _CORPUS})
        dates = sorted({r["filing_date"] for r in _CORPUS})
        return {
            "provider": self.id, "status": "available",
            "total_passages": len(_CORPUS), "companies": companies,
            "filing_types": types, "date_range": [dates[0], dates[-1]] if dates else [],
        }

    def health(self, ctx):
        return {"status": "ok", "provider": self.id, "passages_indexed": len(_CORPUS)}
