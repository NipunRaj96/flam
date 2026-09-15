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

import base64
import json
import logging
from typing import Optional

import httpx
from groq import AsyncGroq

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
MAX_REPOS_TO_FETCH = 8


async def summarize_readme_with_groq(
    repo_name: str,
    readme_text: str,
    groq_api_key: Optional[str],
    model: str = "qwen/qwen3.8-27b",
) -> str:
    """
    Summarize a GitHub repo README into 1-2 concise, high-impact bullet points.
    """
    if not groq_api_key or not readme_text.strip():
        return ""

    try:
        client = AsyncGroq(api_key=groq_api_key)
        prompt = f"""\
You are summarizing a candidate's GitHub project for their job application context.
Repository: {repo_name}
README Content (truncated):
{readme_text[:3000]}

Provide a 1-2 sentence technical summary highlighting:
1. What it does / architecture / protocol implemented
2. Key tools/languages/benchmarks or features
Do not use marketing fluff. Be direct and technical.
"""
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=150,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.warning("Failed to summarize README for %s with Groq: %s", repo_name, e)
        return ""


async def fetch_github_profile_data(
    username: str,
    groq_api_key: Optional[str] = None,
) -> str:
    """
    Fetch public repositories for a GitHub username, extract and summarize
    READMEs, and return a JSON string of repository summaries.

    Returns:
        JSON string: list of dicts with keys:
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

        repos_raw = repos_res.json()
        repos_data = []

        for repo in repos_raw:
            if repo.get("fork"):
                continue  # focus on original projects

            name = repo.get("name", "")
            description = repo.get("description") or ""
            language = repo.get("language") or "Unknown"
            url = repo.get("html_url", "")
            stars = repo.get("stargazers_count", 0)
            default_branch = repo.get("default_branch", "main")

            # Try to fetch README
            readme_text = ""
            try:
                # GitHub raw content
                readme_res = await client.get(
                    f"https://raw.githubusercontent.com/{username}/{name}/{default_branch}/README.md"
                )
                if readme_res.status_code == 200:
                    readme_text = readme_res.text
                else:
                    # Fallback branch master
                    readme_res2 = await client.get(
                        f"https://raw.githubusercontent.com/{username}/{name}/master/README.md"
                    )
                    if readme_res2.status_code == 200:
                        readme_text = readme_res2.text
            except Exception as exc:
                logger.debug("Could not fetch README for %s: %s", name, exc)

            readme_summary = ""
            if readme_text and groq_api_key:
                readme_summary = await summarize_readme_with_groq(
                    repo_name=name,
                    readme_text=readme_text,
                    groq_api_key=groq_api_key,
                )

            repos_data.append({
                "name": name,
                "description": description,
                "language": language,
                "url": url,
                "stars": stars,
                "readme_summary": readme_summary,
            })

            if len(repos_data) >= MAX_REPOS_TO_FETCH:
                break

    return json.dumps(repos_data, indent=2)
