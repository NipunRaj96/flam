"""
Offline checks (no network, no Telegram, no Groq). Run:  python -m tests.test_offline
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.pop("GROQ_API_KEY", None)  # force the no-LLM paths

from bot.handlers import _email_blocks, _plain, _preview_blocks, esc
from classifier.link import extract_emails, find_application_channel, pick_apply_email
from generator.answer_generator import AnswerGenerator, match_fact_key
from generator.humanize import clean_answer, limit_words, parse_limit
from generator.llm import parse_json, strip_reasoning
from intake.post import PostInfo


def test_classifier():
    post = ('Looking for AI engineers in my team at Amazon. Mail me at rohit.mehta [at] amazon [dot] com '
            'with subject "AI Engineer - Name".')
    assert find_application_channel(post) == ("email_application", "rohit.mehta@amazon.com")
    assert find_application_channel("Send CV to jobs@acme.io.")[1] == "jobs@acme.io"      # trailing dot
    assert find_application_channel("ping me  priya @ startup.in")[1] == "priya@startup.in"  # OCR spacing
    assert find_application_channel("Apply https://forms.gle/abc123 or hr@x.com")[0] == "google_forms"
    assert find_application_channel("see https://www.linkedin.com/jobs/view/1")[0] is None   # login-walled
    assert find_application_channel("Join https://acme.com/careers/42")[0] == "custom_career_page"
    assert find_application_channel("nothing here")[0] is None
    e = ["noreply@a.com", "hello@a.com", "careers@a.com"]
    assert pick_apply_email("Questions: noreply@a.com hello@a.com. To apply write to careers@a.com", e) == "careers@a.com"
    assert extract_emails("a@b.com A@B.com") == ["a@b.com"]


def test_humanize():
    raw = '"**Built** a search tool — it utilized caching and leveraged [docs](https://x.io)."'
    out = clean_answer(raw)
    assert "**" not in out and "—" not in out and "utiliz" not in out and "leverag" not in out.lower()
    assert out.startswith("Built") and not out.startswith('"')
    assert clean_answer("<think>hmm</think>Hello", "short_text") == "Hello"
    assert strip_reasoning("<think>unclosed reasoning") == ""
    assert clean_answer("line one\nline two", "short_text") == "line one line two"
    assert len(clean_answer("Sentence one. Sentence two is longer. Sentence three.", "paragraph", 40)) <= 40
    assert parse_limit("Why us? (max 500 characters)") == (500, None)
    assert parse_limit("Describe yourself in 100 words") == (None, 100)
    assert len(limit_words("a b c d e f g h i j", 4).split()) <= 4
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": 2} done') == {"a": 2}


def test_fact_routing():
    k = lambda q, t="short_text": match_fact_key(q, t)
    assert k("Full Name *") == "name" and k("First name") == "first_name"
    assert k("Company name") is None                      # must NOT return the candidate's name
    assert k("LinkedIn profile URL") == "linkedin" and k("GitHub link") == "github"
    assert k("What is your notice period?") == "notice_period"
    assert k("Describe your GitHub projects") is None      # open question
    assert k("Why do you want this role?") is None
    assert k("Full name", "paragraph") is None


def test_generator_facts_and_no_guessing():
    g = AnswerGenerator()
    ctx = {
        "resume": "Jane Doe\njane@x.com +91-9310104862 linkedin.com/in/jane github.com/jane",
        "linkedin": "LONG pasted LinkedIn profile text",
        "github": "• repoA (Py): desc",
        "structured_facts": json.dumps({"name": "Jane Doe", "college": "NIT Trichy"}),
        "user_facts": json.dumps({"notice_period": "30 days"}),
    }
    run = lambda *a, **kw: asyncio.run(g.generate(*a, **kw))
    assert run("Full Name", "short_text", "", ctx).value == "Jane Doe"
    assert run("College / University", "short_text", "", ctx).value == "NIT Trichy"
    assert run("LinkedIn profile URL", "short_text", "", ctx).value == "linkedin.com/in/jane"   # not the pasted blob
    assert run("GitHub profile link", "short_text", "", ctx).value == "github.com/jane"
    assert run("Notice period", "short_text", "", ctx).value == "30 days"
    miss = run("Expected CTC", "short_text", "", ctx)
    assert miss.value is None and miss.flagged and "/fact" in miss.note
    assert run("Company name", "short_text", "", ctx).value is None                           # no placeholder data
    r = run("Need visa sponsorship?", "radio", "", ctx, choices=["Yes", "No"])
    assert r.value is None and r.flagged                                                      # never defaults to "Yes"
    r = run("Gender", "radio", "", ctx, choices=["Male", "Female", "Prefer not to say"])
    assert r.value == "Prefer not to say" and r.flagged
    r = run("Ethnicity", "radio", "", ctx, choices=["A", "B"])
    assert r.value is None


def test_dates():
    from datetime import date
    from generator.answer_generator import join_date
    assert join_date("30 days", date(2026, 10, 5)) == date(2026, 11, 4)
    assert join_date("2 months", date(2026, 10, 5)) == date(2026, 12, 4)
    assert join_date("Immediate", date(2026, 10, 5)) == date(2026, 10, 5)
    assert join_date("negotiable") is None
    g = AnswerGenerator()
    ctx = {"user_facts": json.dumps({"notice_period": "1 week"})}
    r = asyncio.run(g.generate("Latest date you can join", "date", "", ctx))
    assert r.value and len(r.value) == 10 and r.source == "profile"
    r = asyncio.run(g.generate("Date of graduation", "date", "", {}))
    assert r.value is None and r.flagged


def test_previews_never_break_telegram():
    post = PostInfo(role="AI Engineer", company="Amazon <Alexa>")
    fields = [
        {"question": "Why us? (a.b-c!)", "value": "I built <x> & y.\nSecond line.", "field_type": "paragraph",
         "flagged": False, "source": "llm", "context_keys_used": ["context:resume"], "note": ""},
        {"question": "Notice period", "value": None, "field_type": "short_text", "flagged": True,
         "source": "manual_required", "context_keys_used": [], "note": "Add with /fact notice_period 30 days"},
    ]
    blocks = _preview_blocks(post, "Google Forms", "https://x.com/a?b=1&c=2", fields, ["note"])
    assert all(len(b) < 3800 for b in blocks)
    text = "\n".join(blocks)
    assert "&lt;x&gt;" in text and "<x>" not in text and "Amazon &lt;Alexa&gt;" in text
    draft = {"to_email": "a@b.com", "subject": "Hi & bye", "body": "Hello,\n<b>x</b>", "name_missing": True,
             "missing_info": ["notice period"], "gmail_url": "https://mail.google.com/?a=1&b=2",
             "mailto_url": "mailto:a@b.com?subject=Hi%20%26"}
    mail = "\n".join(_email_blocks(draft, post))
    assert "&lt;b&gt;" in mail and "&amp;b=2" in mail
    assert "<b>x</b>" in _plain(mail) and "&lt;" not in _plain(mail)  # plain fallback shows the text, not markup
    assert esc("a<b>&") == "a&lt;b&gt;&amp;"


def test_db_dedup_and_retry():
    import idempotency as idem
    from db.session import create_tables, init_db

    async def go():
        with tempfile.TemporaryDirectory() as d:
            init_db(f"sqlite+aiosqlite:///{d}/t.db")
            await create_tables()
            u = await idem.get_or_create_user(1)
            h = idem.stable_jd_hash("Amazon", "AI Engineer", "ocr text A")
            assert h == idem.stable_jd_hash("amazon", "ai engineer", "totally different OCR text")  # stable across screenshots
            a = await idem.create_application(u.id, h, "x@y.com", "email_application", "AI Engineer", "Amazon")
            await idem.mark_failed(a.id, "boom")
            assert await idem.check_duplicate(u.id, h, "x@y.com") is None         # failed does not block
            a2 = await idem.create_application(u.id, h, "x@y.com", "email_application")
            assert a2 is not None and a2.id == a.id                                # row reused, constraint respected
            await idem.mark_pending_approval(a2.id)
            assert await idem.cancel_stale_applications() == 1                     # restart cleanup
            a3 = await idem.create_application(u.id, h, "x@y.com", "email_application")
            await idem.mark_submitted(a3.id, "r.txt", confirmed=False)
            assert await idem.check_duplicate(u.id, h, "x@y.com") is not None     # unconfirmed still blocks
            assert await idem.create_application(u.id, h, "x@y.com", "email_application") is None
            # same form URL, different post text: blocked for forms, allowed for email inboxes
            b = await idem.create_application(u.id, "h1", "https://forms/x", "google_forms")
            await idem.mark_submitted(b.id, "r.png")
            assert await idem.check_duplicate(u.id, "h2", "https://forms/x", by_form_only=True) is not None
            assert await idem.check_duplicate(u.id, "h2", "https://forms/x", by_form_only=False) is None
            assert idem._strip_url("https://a.com/jobs/?gh_jid=77&utm_source=x#f") == "https://a.com/jobs?gh_jid=77"

    asyncio.run(go())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("ALL PASSED")
