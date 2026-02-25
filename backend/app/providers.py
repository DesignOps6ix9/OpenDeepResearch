from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx


LOW_QUALITY_DOMAINS = {
    "reddit.com",
    "quora.com",
    "pinterest.com",
    "tiktok.com",
}

OFFICIAL_DOMAINS = {
    "who.int",
    "oecd.org",
    "worldbank.org",
    "imf.org",
    "europa.eu",
    "sec.gov",
    "nih.gov",
    "cdc.gov",
    "gov.cn",
}

NEWS_DOMAINS = {
    "reuters.com",
    "bloomberg.com",
    "ft.com",
    "wsj.com",
    "economist.com",
    "apnews.com",
    "bbc.com",
    "nytimes.com",
    "chinanews.com",
    "news.cn",
    "people.com.cn",
}

INDUSTRY_DOMAINS = {
    "mckinsey.com",
    "bcg.com",
    "deloitte.com",
    "pwc.com",
    "kpmg.com",
    "gartner.com",
    "forrester.com",
    "statista.com",
    "36kr.com",
    "huxiu.com",
    "iyiou.com",
}

ACADEMIC_DOMAINS = {
    "doi.org",
    "arxiv.org",
    "nature.com",
    "science.org",
    "acm.org",
    "ieee.org",
    "nber.org",
    "pubmed.ncbi.nlm.nih.gov",
    "pmc.ncbi.nlm.nih.gov",
    "scholar.google.com",
}

ACADEMIC_HINTS = (
    "doi",
    "arxiv",
    "pubmed",
    "meta-analysis",
    "systematic review",
    "clinical trial",
    "randomized",
    "peer reviewed",
    "期刊",
    "论文",
    "文献",
    "综述",
    "临床",
    "随机对照",
)


@dataclass
class SearchResult:
    provider: str
    query: str
    title: str
    url: str
    snippet: str
    domain: str
    score: float
    quality_score: float
    rerank_score: float = 0.0


@dataclass
class SourceDocument:
    provider: str
    url: str
    domain: str
    title: str
    quality_score: float
    content: str
    excerpt: str


def extract_domain(url: str) -> str:
    try:
        domain = urlparse(url).netloc.lower().strip()
        if domain.startswith("www."):
            return domain[4:]
        return domain
    except Exception:
        return ""


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        clean_query = urlencode([(k, v) for k, v in parse_qsl(parsed.query) if not k.startswith("utm_")])
        normalized = parsed._replace(query=clean_query, fragment="")
        return urlunparse(normalized)
    except Exception:
        return url


def _domain_match(domain: str, rule: str) -> bool:
    rule = rule.lower().strip()
    if not rule:
        return False
    if domain == rule:
        return True
    if domain.endswith(f".{rule}"):
        return True
    if rule in {"gov", "edu"} and domain.endswith(f".{rule}"):
        return True
    return False


def source_quality_score(
    domain: str,
    provider_score: float,
    trusted_domains: list[str],
    required_domains: list[str],
    blocked_domains: list[str],
) -> float:
    quality = provider_score

    if any(_domain_match(domain, item) for item in blocked_domains):
        return -999.0

    if any(_domain_match(domain, item) for item in required_domains):
        quality += 4.0

    trusted_matched = any(_domain_match(domain, item) for item in trusted_domains)
    if trusted_matched:
        # Keep trust boost, but avoid over-amplifying academic domains.
        if domain.endswith(".edu") or domain in ACADEMIC_DOMAINS:
            quality += 0.8
        else:
            quality += 1.6

    if domain.endswith(".gov") or domain.endswith(".int"):
        quality += 1.2
    elif domain.endswith(".edu"):
        quality += 0.35

    if domain in LOW_QUALITY_DOMAINS:
        quality -= 3.0

    return quality


def is_academic_query(query: str) -> bool:
    lowered = (query or "").lower()
    return any(hint in lowered for hint in ACADEMIC_HINTS)


def has_chinese_chars(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


class ResearchToolkit:
    def __init__(
        self,
        tavily_api_key: str,
        serper_api_key: str,
        firecrawl_api_key: str,
        cohere_api_key: str,
        cohere_rerank_model: str,
        crossref_base_url: str,
        crossref_max_results_per_query: int,
        jina_reader_api_key: str,
        trusted_domains: list[str],
    ) -> None:
        self.tavily_api_key = tavily_api_key
        self.serper_api_key = serper_api_key
        self.firecrawl_api_key = firecrawl_api_key
        self.cohere_api_key = cohere_api_key
        self.cohere_rerank_model = cohere_rerank_model
        self.crossref_base_url = crossref_base_url.rstrip("/")
        self.crossref_max_results_per_query = max(1, crossref_max_results_per_query)
        self.jina_reader_api_key = jina_reader_api_key
        self.trusted_domains = trusted_domains

    async def search_many(
        self,
        queries: list[str],
        required_domains: list[str],
        blocked_domains: list[str],
        max_results_per_query: int,
        max_total_results: int,
    ) -> list[SearchResult]:
        tasks = [
            self._search_single_query(
                query=query,
                required_domains=required_domains,
                blocked_domains=blocked_domains,
                max_results=max_results_per_query,
            )
            for query in queries
        ]
        batches = await asyncio.gather(*tasks)

        merged: dict[str, SearchResult] = {}
        for batch in batches:
            for item in batch:
                key = normalize_url(item.url)
                existing = merged.get(key)
                if existing is None or item.quality_score > existing.quality_score:
                    merged[key] = item

        ranked = sorted(merged.values(), key=lambda x: x.quality_score, reverse=True)
        reranked = await self._rerank_with_cohere(
            query=" ; ".join(queries[:2]),
            results=ranked,
        )
        if reranked:
            ranked = reranked

        diversified = self._diversify_by_domain(
            results=ranked,
            limit=max_total_results,
            max_per_domain=2,
        )
        return diversified

    def _classify_source_family(self, item: SearchResult) -> str:
        domain = (item.domain or "").lower()
        provider = (item.provider or "").lower()

        if provider == "crossref":
            return "academic"
        if domain in LOW_QUALITY_DOMAINS:
            return "community"
        if domain in ACADEMIC_DOMAINS:
            return "academic"
        if domain.endswith(".gov") or domain.endswith(".int") or domain in OFFICIAL_DOMAINS:
            return "official"
        if domain.endswith(".edu"):
            return "academic"
        if domain in NEWS_DOMAINS:
            return "practice"
        if domain in INDUSTRY_DOMAINS:
            return "industry"
        return "practice"

    async def _search_single_query(
        self,
        query: str,
        required_domains: list[str],
        blocked_domains: list[str],
        max_results: int,
    ) -> list[SearchResult]:
        tasks: list[asyncio.Task[list[SearchResult]]] = []

        if self.tavily_api_key:
            tasks.append(
                asyncio.create_task(
                    self._search_tavily(
                        query=query,
                        max_results=max_results,
                        required_domains=required_domains,
                        blocked_domains=blocked_domains,
                    )
                )
            )

        if self.serper_api_key:
            tasks.append(
                asyncio.create_task(
                    self._search_serper(
                        query=query,
                        max_results=max_results,
                        required_domains=required_domains,
                        blocked_domains=blocked_domains,
                    )
                )
            )

        if self.crossref_base_url and is_academic_query(query):
            tasks.append(
                asyncio.create_task(
                    self._search_crossref(
                        query=query,
                        max_results=min(max_results, self.crossref_max_results_per_query),
                        required_domains=required_domains,
                        blocked_domains=blocked_domains,
                    )
                )
            )

        if not tasks:
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)
        merged: list[SearchResult] = []
        for item in results:
            if isinstance(item, Exception):
                continue
            merged.extend(item)
        return merged

    def _diversify_by_domain(
        self,
        results: list[SearchResult],
        limit: int,
        max_per_domain: int = 2,
    ) -> list[SearchResult]:
        if not results:
            return []
        if max_per_domain <= 0:
            return results[:limit]

        selected: list[SearchResult] = []
        per_domain: dict[str, int] = {}
        skipped: list[SearchResult] = []

        for item in results:
            domain = item.domain or "unknown"
            count = per_domain.get(domain, 0)
            if count >= max_per_domain:
                skipped.append(item)
                continue
            selected.append(item)
            per_domain[domain] = count + 1
            if len(selected) >= limit:
                return selected

        if len(selected) < limit and skipped:
            for item in skipped:
                selected.append(item)
                if len(selected) >= limit:
                    break

        return selected[:limit]

    async def _search_tavily(
        self,
        query: str,
        max_results: int,
        required_domains: list[str],
        blocked_domains: list[str],
    ) -> list[SearchResult]:
        payload = {
            "api_key": self.tavily_api_key,
            "query": query,
            "search_depth": "advanced",
            "max_results": max_results,
            "include_raw_content": False,
            "include_answer": False,
        }

        try:
            async with httpx.AsyncClient(timeout=35.0) as client:
                response = await client.post("https://api.tavily.com/search", json=payload)
            response.raise_for_status()
            data = response.json()
        except Exception:
            return []

        items: list[SearchResult] = []
        for result in data.get("results", [])[:max_results]:
            url = str(result.get("url", "")).strip()
            if not url:
                continue
            domain = extract_domain(url)
            quality = source_quality_score(
                domain=domain,
                provider_score=float(result.get("score", 0.0) or 0.0),
                trusted_domains=self.trusted_domains,
                required_domains=required_domains,
                blocked_domains=blocked_domains,
            )
            if quality <= -900:
                continue
            items.append(
                SearchResult(
                    provider="tavily",
                    query=query,
                    title=str(result.get("title", "")).strip(),
                    url=url,
                    snippet=str(result.get("content", "")).strip(),
                    domain=domain,
                    score=float(result.get("score", 0.0) or 0.0),
                    quality_score=quality,
                )
            )

        return items

    async def _search_serper(
        self,
        query: str,
        max_results: int,
        required_domains: list[str],
        blocked_domains: list[str],
    ) -> list[SearchResult]:
        headers = {
            "X-API-KEY": self.serper_api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "q": query,
            "num": max_results,
            "gl": "cn" if has_chinese_chars(query) else "us",
            "hl": "zh-cn" if has_chinese_chars(query) else "en",
        }

        try:
            async with httpx.AsyncClient(timeout=35.0) as client:
                response = await client.post("https://google.serper.dev/search", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        except Exception:
            return []

        items: list[SearchResult] = []
        for result in data.get("organic", [])[:max_results]:
            url = str(result.get("link", "")).strip()
            if not url:
                continue
            domain = extract_domain(url)
            quality = source_quality_score(
                domain=domain,
                provider_score=1.0,
                trusted_domains=self.trusted_domains,
                required_domains=required_domains,
                blocked_domains=blocked_domains,
            )
            if quality <= -900:
                continue
            items.append(
                SearchResult(
                    provider="serper",
                    query=query,
                    title=str(result.get("title", "")).strip(),
                    url=url,
                    snippet=str(result.get("snippet", "")).strip(),
                    domain=domain,
                    score=1.0,
                    quality_score=quality,
                )
            )

        return items

    async def _search_crossref(
        self,
        query: str,
        max_results: int,
        required_domains: list[str],
        blocked_domains: list[str],
    ) -> list[SearchResult]:
        params = {
            "query.bibliographic": query,
            "rows": str(max_results),
            "sort": "relevance",
            "order": "desc",
        }
        url = f"{self.crossref_base_url}/works"

        try:
            async with httpx.AsyncClient(timeout=35.0) as client:
                response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        except Exception:
            return []

        message = data.get("message") if isinstance(data, dict) else None
        items_raw = message.get("items") if isinstance(message, dict) else None
        if not isinstance(items_raw, list):
            return []

        items: list[SearchResult] = []
        for result in items_raw[:max_results]:
            if not isinstance(result, dict):
                continue

            title_raw = result.get("title", [])
            title = ""
            if isinstance(title_raw, list) and title_raw:
                title = str(title_raw[0]).strip()
            elif isinstance(title_raw, str):
                title = title_raw.strip()

            doi = str(result.get("DOI", "")).strip()
            url_val = str(result.get("URL", "")).strip()
            if not url_val and doi:
                url_val = f"https://doi.org/{doi}"
            if not url_val:
                continue

            domain = extract_domain(url_val)
            abstract = str(result.get("abstract", "")).strip()
            snippet = self._clean_crossref_abstract(abstract)
            if not snippet:
                container = result.get("container-title", [])
                if isinstance(container, list) and container:
                    snippet = f"Published in {container[0]}"
                else:
                    snippet = "Crossref scholarly record"

            quality = source_quality_score(
                domain=domain,
                provider_score=0.6,
                trusted_domains=self.trusted_domains,
                required_domains=required_domains,
                blocked_domains=blocked_domains,
            )
            if quality <= -900:
                continue

            items.append(
                SearchResult(
                    provider="crossref",
                    query=query,
                    title=title or f"DOI {doi}",
                    url=url_val,
                    snippet=snippet[:700],
                    domain=domain,
                    score=0.6,
                    quality_score=quality,
                )
            )

        return items

    async def _rerank_with_cohere(self, query: str, results: list[SearchResult]) -> list[SearchResult]:
        if not self.cohere_api_key or not query.strip() or not results:
            return results

        docs = [
            f"{item.title}\n{item.snippet}\n{item.url}"
            for item in results[:50]
        ]
        headers = {
            "Authorization": f"Bearer {self.cohere_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.cohere_rerank_model,
            "query": query,
            "documents": docs,
            "top_n": min(len(docs), 20),
        }

        try:
            async with httpx.AsyncClient(timeout=35.0) as client:
                response = await client.post("https://api.cohere.com/v2/rerank", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        except Exception:
            return results

        rerank_items = data.get("results") if isinstance(data, dict) else None
        if not isinstance(rerank_items, list):
            return results

        indexed = list(results)
        for row in rerank_items:
            if not isinstance(row, dict):
                continue
            index = row.get("index")
            relevance = row.get("relevance_score")
            if not isinstance(index, int) or not isinstance(relevance, (int, float)):
                continue
            if index < 0 or index >= len(indexed):
                continue
            item = indexed[index]
            item.rerank_score = float(relevance)
            # Blend retrieval score + domain quality + rerank relevance.
            item.quality_score = item.quality_score + 2.5 * float(relevance)

        indexed.sort(key=lambda x: x.quality_score, reverse=True)
        return indexed

    def _clean_crossref_abstract(self, text: str) -> str:
        if not text:
            return ""
        cleaned = re.sub(r"<[^>]+>", " ", text)
        cleaned = html.unescape(cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    async def read_sources(self, items: list[SearchResult], max_read: int) -> list[SourceDocument]:
        top_items = self._diversify_by_domain(
            results=items,
            limit=max_read,
            max_per_domain=1 if max_read <= 8 else 2,
        )
        tasks = [asyncio.create_task(self._read_single(item)) for item in top_items]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        docs: list[SourceDocument] = []
        for result in results:
            if isinstance(result, Exception) or result is None:
                continue
            docs.append(result)
        return docs

    async def _read_single(self, item: SearchResult) -> SourceDocument | None:
        content = ""

        if self.firecrawl_api_key:
            content = await self._read_with_firecrawl(item.url)

        if not content and self.jina_reader_api_key:
            content = await self._read_with_jina(item.url)

        if not content:
            content = await self._read_with_http(item.url)

        if not content:
            return None

        compact = re.sub(r"\s+", " ", content).strip()
        excerpt = compact[:700]

        return SourceDocument(
            provider=item.provider,
            url=item.url,
            domain=item.domain,
            title=item.title,
            quality_score=item.quality_score,
            content=compact[:20000],
            excerpt=excerpt,
        )

    async def _read_with_firecrawl(self, url: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.firecrawl_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": True,
        }

        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.post(
                    "https://api.firecrawl.dev/v1/scrape",
                    headers=headers,
                    json=payload,
                )
            response.raise_for_status()
            data = response.json()
        except Exception:
            return ""

        if not isinstance(data, dict):
            return ""
        body = data.get("data") or {}
        markdown = body.get("markdown")
        if isinstance(markdown, str):
            return markdown
        return ""

    async def _read_with_jina(self, url: str) -> str:
        if url.startswith("http://") or url.startswith("https://"):
            reader_url = f"https://r.jina.ai/{url}"
        else:
            reader_url = f"https://r.jina.ai/http://{url}"

        headers = {
            "Authorization": f"Bearer {self.jina_reader_api_key}",
            "X-API-Key": self.jina_reader_api_key,
        }

        try:
            async with httpx.AsyncClient(timeout=35.0, follow_redirects=True) as client:
                response = await client.get(reader_url, headers=headers)
            response.raise_for_status()
            text = response.text
        except Exception:
            return ""

        cleaned = re.sub(r"\s+", " ", text).strip()
        return cleaned[:20000]

    async def _read_with_http(self, url: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(url)
            response.raise_for_status()
            text = response.text
        except Exception:
            return ""

        cleaned = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"<style[\s\S]*?</style>", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = html.unescape(cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned[:20000]
