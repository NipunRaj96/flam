"""
GitHub profile and repository fetcher for candidate context.

Per Phase 2 specification:
- One-time pull via official public GitHub API (no scraping).
- Fetches top non-fork repositories sorted by recently pushed.
- Summarizes READMEs using Groq before storing to avoid storage/token bloat.
- Stored as a structured JSON string in context_versions so context/store.py
  can select the top-N relevant repos per job description.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx
from generator import llm

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
MAX_REPOS_TO_FETCH = 8


async def summarize_readme_with_groq(repo_name: str, readme_text: str, groq_api_key: Optional[str] = None) -> str:
    """One or two plain sentences on what a repo is and does."""
    if not llm.has_llm() or not readme_text.strip():
        return ""
    prompt = (
        f"Repository: {repo_name}\n"
        f"README (truncated):\n{readme_text[:3000]}\n\n"
        "In one or two plain sentences, say what this project does and what it is built with. "
        "Write so a non-specialist can follow. No marketing words. Output only the sentences."
    )
    try:
        return await llm.chat([{"role": "user", "content": prompt}], temperature=0.2, max_tokens=150)
    except Exception as e:
        logger.warning("README summary failed for %s: %s", repo_name, e)
        return ""


async def fetch_github_profile_data(
    username: str,
    groq_api_key: Optional[str] = None,
) -> list[dict]:
    """
    Fetch public repositories for a GitHub username, extract and summarize
    READMEs, and return the repository summaries.

    Returns:
        list of dicts with keys:
          name, description, language, url, stars, readme_summary
    """
    username = username.strip().lstrip("@")
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "flam-job-agent/1.0",
    }

    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        # 1. Fetch user info
        user_res = await client.get(f"{GITHUB_API_BASE}/users/{username}")
        if user_res.status_code == 404:
            raise ValueError(f"GitHub user '{username}' not found.")
        elif user_res.status_code != 200:
            raise RuntimeError(f"GitHub API error: {user_res.status_code} - {user_res.text}")

        # 2. Fetch public repos
        repos_res = await client.get(
            f"{GITHUB_API_BASE}/users/{username}/repos",
            params={"sort": "pushed", "direction": "desc", "per_page": 15},
        )
        if repos_res.status_code != 200:
            raise RuntimeError(f"Failed to fetch repositories: {repos_res.status_code}")

        originals = [r for r in repos_res.json() if not r.get("fork")][:MAX_REPOS_TO_FETCH]

        async def _one(repo: dict) -> dict:
            name = repo.get("name", "")
            branch = repo.get("default_branch", "main")
            readme_text = ""
            for ref in dict.fromkeys([branch, "main", "master"]):
                try:
                    res = await client.get(f"https://raw.githubusercontent.com/{username}/{name}/{ref}/README.md")
                except Exception as exc:
                    logger.debug("README fetch failed for %s: %s", name, exc)
                    continue
                if res.status_code == 200:
                    readme_text = res.text
                    break
            return {
                "name": name,
                "description": repo.get("description") or "",
                "language": repo.get("language") or "Unknown",
                "url": repo.get("html_url", ""),
                "stars": repo.get("stargazers_count", 0),
                "readme_summary": await summarize_readme_with_groq(name, readme_text),
            }

        repos_data = list(await asyncio.gather(*[_one(r) for r in originals]))

    return repos_data
