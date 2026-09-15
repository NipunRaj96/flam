"""
Static test profile — Phase 1 only.

In Phase 2 this entire module is replaced by a context-store lookup that
reads from context_versions in the DB (resume, GitHub, LinkedIn, portfolio).

The dict keys must match the profile_key values in executor/google_forms.py's
_FIELD_KEYWORDS mapping.
"""

STATIC_PROFILE: dict[str, str] = {
    "name": "Nipun Test",
    "email": "nipun.test@example.com",
    "phone": "+91 99999 00000",
    "linkedin": "https://linkedin.com/in/nipun-test",
    "github": (
        "1. raft-kv (https://github.com/nipun-test/raft-kv) — distributed key-value store "
        "implementing the Raft consensus algorithm in Go. Handles leader election, log replication, "
        "and snapshotting. Benchmarked at ~50k ops/s on a 3-node cluster.\n"
        "2. llm-eval (https://github.com/nipun-test/llm-eval) — lightweight framework for evaluating "
        "LLM outputs across factuality, coherence, and task-specific rubrics.\n"
        "3. stream-index (https://github.com/nipun-test/stream-index) — real-time inverted index "
        "over Kafka streams, used for low-latency search on event data."
    ),
    "portfolio": "https://nipun-test.dev",
    "college": "Indian Institute of Technology, Delhi",
    "degree": "B.Tech Computer Science",
    "graduation_year": "2025",
    "cgpa": "8.5 / 10",
    "about": (
        "Computer science student with a focus on systems and applied ML. "
        "Built a distributed key-value store and an LLM evaluation framework. "
        "Interned at a fintech startup where I reduced DB query latency by 40%."
    ),
    # Open-ended answers — Phase 2 replaces these with JD-specific LLM output (doc 04).
    "why_interested": (
        "I'm interested in this AI Engineer role because it sits at the intersection of systems "
        "engineering and applied ML — exactly where I've been building. My LLM evaluation framework "
        "(llm-eval on GitHub) came out of frustration with opaque model outputs; I want to work on "
        "production AI systems where correctness and reliability matter as much as accuracy metrics."
    ),
    "why_hire": (
        "I build things that work at the system level, not just in notebooks. My raft-kv project "
        "demonstrates I can implement complex distributed protocols from scratch. I'm a fast learner "
        "who documents and tests as I go, and I'm looking for a role where I can own hard problems "
        "end-to-end from day one."
    ),
    "experience": (
        "Internship: Fintech startup (6 months) — optimised PostgreSQL query plans, reducing p99 "
        "latency from 800ms to 120ms using partial indexes and query rewrites.\n"
        "Projects: raft-kv (distributed systems), llm-eval (LLM evaluation), stream-index "
        "(real-time search over Kafka). All on GitHub. Full details in resume."
    ),
}
