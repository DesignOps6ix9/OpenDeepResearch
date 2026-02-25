from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # noqa: BLE001
    load_dotenv = None

def _fallback_load_env(path: Path, override: bool = False) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return

    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        env_key = key.strip()
        if not env_key:
            continue
        env_value = value.strip().strip('"').strip("'")
        if override or env_key not in os.environ:
            os.environ[env_key] = env_value


repo_env = Path(__file__).resolve().parents[2] / ".env"

if load_dotenv is not None:
    load_dotenv()
    if repo_env.exists():
        # Prefer repository .env for deterministic local runs.
        load_dotenv(repo_env, override=True)
else:
    _fallback_load_env(Path.cwd() / ".env", override=False)
    _fallback_load_env(repo_env, override=True)


def _parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    parts = [part.strip() for part in value.replace(";", ",").split(",")]
    return [part for part in parts if part]


DEFAULT_TRUSTED_DOMAINS = [
    # Public institutions and standards.
    "nih.gov",
    "cdc.gov",
    "who.int",
    "oecd.org",
    "worldbank.org",
    "imf.org",
    "europa.eu",
    "sec.gov",
    # Industry and business analysis.
    "mckinsey.com",
    "bcg.com",
    "deloitte.com",
    "pwc.com",
    "kpmg.com",
    "gartner.com",
    "forrester.com",
    # Credible media / market reporting.
    "reuters.com",
    "bloomberg.com",
    "ft.com",
    "wsj.com",
    "economist.com",
    # Product / engineering official docs.
    "github.com",
    "learn.microsoft.com",
    "cloud.google.com",
    "openai.com",
    "deepmind.google",
    "anthropic.com",
    # Academic is still allowed, but no longer the default dominant signal.
    "arxiv.org",
    "nature.com",
    "science.org",
    "acm.org",
    "ieee.org",
    "nber.org",
    "gov",
    "edu",
]

DEFAULT_BLOCKED_DOMAINS = [
    "reddit.com",
    "quora.com",
    "pinterest.com",
]


@dataclass
class Settings:
    llm_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    llm_api_key: str = ""
    llm_model_supervisor: str = "doubao-seed-1-6-flash-250828"
    llm_model_planner: str = "doubao-seed-1-6-flash-250828"
    llm_model_researcher: str = "doubao-seed-1-6-flash-250828"
    research_model_temperature: float = 0.0

    tavily_api_key: str = ""
    serper_api_key: str = ""
    firecrawl_api_key: str = ""
    cohere_api_key: str = ""
    cohere_rerank_model: str = "rerank-v3.5"
    crossref_base_url: str = "https://api.crossref.org"
    jina_reader_api_key: str = ""

    default_allowed_domains: list[str] = field(default_factory=list)
    default_blocked_domains: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_DOMAINS))
    trusted_domains: list[str] = field(default_factory=lambda: list(DEFAULT_TRUSTED_DOMAINS))

    search_max_results_per_query: int = 5
    max_sources_per_step: int = 6
    max_read_sources_per_step: int = 4
    crossref_max_results_per_query: int = 4

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            llm_base_url=os.getenv("LLM_BASE_URL") or os.getenv("ARK_BASE_URL") or cls.llm_base_url,
            llm_api_key=os.getenv("LLM_API_KEY") or os.getenv("ARK_API_KEY") or "",
            llm_model_supervisor=os.getenv("LLM_MODEL_SUPERVISOR") or os.getenv("ARK_MODEL") or cls.llm_model_supervisor,
            llm_model_planner=os.getenv("LLM_MODEL_PLANNER") or os.getenv("ARK_MODEL") or cls.llm_model_planner,
            llm_model_researcher=os.getenv("LLM_MODEL_RESEARCHER") or os.getenv("ARK_MODEL") or cls.llm_model_researcher,
            research_model_temperature=float(os.getenv("RESEARCH_MODEL_TEMPERATURE", "0")),
            tavily_api_key=os.getenv("TAVILY_API_KEY", ""),
            serper_api_key=os.getenv("SERPER_API_KEY", ""),
            firecrawl_api_key=os.getenv("FIRECRAWL_API_KEY", ""),
            cohere_api_key=os.getenv("COHERE_API_KEY", ""),
            cohere_rerank_model=os.getenv("COHERE_RERANK_MODEL", "rerank-v3.5"),
            crossref_base_url=os.getenv("CROSSREF_BASE_URL", "https://api.crossref.org"),
            jina_reader_api_key=os.getenv("JINA_READER_API_KEY", "") or os.getenv("JINA_API_KEY", ""),
            default_allowed_domains=_parse_csv(os.getenv("DEFAULT_ALLOWED_DOMAINS")),
            default_blocked_domains=_parse_csv(os.getenv("DEFAULT_BLOCKED_DOMAINS")) or list(DEFAULT_BLOCKED_DOMAINS),
            trusted_domains=_parse_csv(os.getenv("TRUSTED_DOMAINS")) or list(DEFAULT_TRUSTED_DOMAINS),
            search_max_results_per_query=int(os.getenv("SEARCH_MAX_RESULTS_PER_QUERY", "5")),
            max_sources_per_step=int(os.getenv("MAX_SOURCES_PER_STEP", "6")),
            max_read_sources_per_step=int(os.getenv("MAX_READ_SOURCES_PER_STEP", "4")),
            crossref_max_results_per_query=int(os.getenv("CROSSREF_MAX_RESULTS_PER_QUERY", "4")),
        )
