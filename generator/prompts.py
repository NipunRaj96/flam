"""
Prompt templates and context slice construction for Phase 2 answer generation.

Implements doc 04 cross-referencing & human-formatted output:
- Human form-filling tone (short paragraphs, bullet points, line breaks).
- Strict factual grounding in candidate context.
- Discrete choice selection for dropdown, radio, and checkbox fields.
- Cites used context sources for traceability in preview and DB.
"""
from __future__ import annotations

from typing import Optional

SYSTEM_PROMPT = """\
You are an expert, honest job application assistant helping a candidate fill out questions on a job application form.

Core Rules:
1. GROUNDED IN CONTEXT ONLY: Use ONLY the candidate's provided resume, GitHub projects, and profile data. Never invent past companies, fake metrics, unverified degrees, or imaginary experiences.
2. HUMAN FORM-FILLING STYLE:
   - Write like a human filling out this form, not a document or essay.
   - Use short paragraphs or bullet points where it improves readability (especially for project descriptions, skills, or experience overviews).
   - Avoid dense, single-block paragraphs.
   - Keep formatting appropriate to the field type:
     * Short-answer field: 1–2 direct lines.
     * Paragraph field: 2–4 short paragraphs or simple dashed/bulleted points with clean line breaks.
   - PLAIN TEXT ONLY: A form field is NOT markdown. Do NOT use markdown bolding (e.g. **Heading**), markdown links ([link](url)), or code backticks (`code`). Use clean plain text.
3. TAILORED & SPECIFIC: Connect specific projects, tools, and metrics from the candidate's background to the exact requirements in the job description.
4. AUTHENTIC VOICE: Write in natural first person ("I built", "I worked on"). Sound like a real person, not an AI or cover letter. Avoid corporate buzzwords and robotic filler.
5. If the candidate's context genuinely lacks any relevant information to answer the question, respond with exactly:
NEEDS_MANUAL_REVIEW

{user_template_section}
"""


def system_with_template(user_template: Optional[str]) -> str:
    """Inject the user's editable style template into the system prompt."""
    if user_template and user_template.strip():
        section = f"STYLE & TONE INSTRUCTIONS FROM CANDIDATE:\n{user_template.strip()}"
    else:
        section = "STYLE: Direct, concise, technical, impact-oriented, human-written formatting."
    return SYSTEM_PROMPT.format(user_template_section=section)


def build_user_prompt(
    question: str,
    field_type: str,
    jd_text: str,
    context: dict[str, str],
) -> str:
    """
    Construct the tailored user prompt for answering a text or paragraph question.
    Extracts relevant slices from resume, github, linkedin, and portfolio.
    """
    context_blocks = []

    # 1. Resume slice
    if "resume" in context and context["resume"]:
        context_blocks.append(f"--- CANDIDATE RESUME ---\n{context['resume'][:3500]}")

    # 2. Filtered GitHub repos
    if "github" in context and context["github"]:
        context_blocks.append(f"--- CANDIDATE GITHUB PROJECTS ---\n{context['github']}")

    # 3. LinkedIn context
    if "linkedin" in context and context["linkedin"]:
        context_blocks.append(f"--- CANDIDATE LINKEDIN PROFILE ---\n{context['linkedin'][:1500]}")

    # 4. Portfolio / Additional notes
    if "portfolio" in context and context["portfolio"]:
        context_blocks.append(f"--- CANDIDATE PORTFOLIO / NOTES ---\n{context['portfolio'][:1000]}")

    # Fallback to any remaining top-level keys
    other_keys = [k for k in context if k not in ("resume", "github", "linkedin", "portfolio", "structured_facts") and context[k]]
    if other_keys:
        other_lines = [f"{k}: {context[k]}" for k in other_keys[:10]]
        context_blocks.append("--- CANDIDATE FACTS ---\n" + "\n".join(other_lines))

    formatted_context = "\n\n".join(context_blocks) if context_blocks else "No detailed context provided."

    return f"""\
JOB DESCRIPTION / POSTING:
\"\"\"
{jd_text.strip()[:2500] if jd_text.strip() else "General job application."}
\"\"\"

CANDIDATE BACKGROUND & CONTEXT:
\"\"\"
{formatted_context}
\"\"\"

FORM QUESTION:
{question}

FIELD TYPE:
{field_type}

Instructions:
- Write like a human filling out this form, not a document. Use short paragraphs or bullet points where it improves readability. Avoid dense single-block paragraphs.
- Keep formatting appropriate to the field type (short-answer field = 1-2 lines, paragraph field = can use line breaks/bullets).
- Output the exact text to fill into this field. Do NOT include preamble, meta-commentary, or surrounding quotes.
"""


def build_choice_prompt(
    question: str,
    field_type: str,
    choices: list[str],
    jd_text: str,
    context: dict[str, str],
) -> str:
    """
    Construct prompt for multiple-choice, radio, or dropdown fields.
    Forces the LLM to choose from the provided list of choices.
    """
    choices_formatted = "\n".join(f"- {c}" for c in choices)

    context_summary = []
    if "resume" in context and context["resume"]:
        context_summary.append(context["resume"][:2500])
    if "github" in context and context["github"]:
        context_summary.append(context["github"][:1500])
    if "linkedin" in context and context["linkedin"]:
        context_summary.append(context["linkedin"][:1000])

    ctx_text = "\n\n".join(context_summary) if context_summary else "No context provided."

    return f"""\
CANDIDATE BACKGROUND:
\"\"\"
{ctx_text}
\"\"\"

JOB DESCRIPTION CONTEXT:
\"\"\"
{jd_text.strip()[:1500] if jd_text.strip() else "General job application."}
\"\"\"

QUESTION:
{question}

FIELD TYPE:
{field_type} (Discrete choice selection)

AVAILABLE CHOICES (Select from this exact list):
{choices_formatted}

Instructions:
- Based on the candidate's background, select the best matching option from the AVAILABLE CHOICES above.
- For radio or dropdown (single select): Return ONLY the exact string of the single chosen option, nothing else.
- For checkbox (multi select): Return the chosen options separated by a pipe character '|' (e.g. "Python | Go | Docker").
- The output MUST exactly match one (or more) of the provided choices verbatim. Do NOT add explanation, numbering, or quotes.
"""
