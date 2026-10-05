# flam — AI Job Application Agent

> [!NOTE]
> **Active development.** Features and platform support are still evolving.

**flam** is a Telegram bot that applies to jobs for you. Send it anything that says how to apply and it drafts the answers in your own voice, shows you a preview, and submits only after you approve.

## What you can send it

- A **form or career-page link** (Google Forms, MS Forms, Typeform, Notion Forms, Greenhouse/Lever-style pages)
- A post that says **"mail me at …"**: paste it, forward it, or send a **screenshot** (it reads the image, finds the email, and follows any instructions in the post such as the exact subject line)
- A **job PDF**

For forms and career pages it fills the form and shows a numbered preview. For emails it writes the subject and body and gives you a one-tap **Open in Gmail** link, since it does not send mail by itself (see Limits).

## How answers are written

- Plain, specific, first person. No buzzwords, no "I am excited to…", no markdown, no em dashes.
- Technical work is explained in everyday words unless the post itself uses the technical term.
- **Only facts from your profile.** Plain questions (name, college, phone, notice period…) are copied from your saved facts. Anything it doesn't know is flagged for you, never guessed. Visa status, salary, gender and similar questions are never answered for you.
- Respects limits like "max 300 characters" or "in 100 words".
- You control the tone with `/template`, and add facts a resume doesn't say with `/fact`.

## Quick start

```bash
git clone https://github.com/NipunRaj96/Flam.git && cd Flam
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp secrets/.env.example secrets/.env      # then fill it in
python -m bot.main
```

`secrets/.env`:

```env
TELEGRAM_BOT_TOKEN=...            # from @BotFather
GROQ_API_KEY=...
ALLOWED_TELEGRAM_IDS=123456789    # your Telegram user id. If empty, ANYONE can use your bot.
MAX_DAILY_SUBMISSIONS=20
# GROQ_MODEL=qwen/qwen3.8-27b     # default; the same model reads screenshots
```

## Commands

| Command | What it does |
|---|---|
| `/upload` | Save your resume (PDF or text). The PDF is kept so file-upload fields can use it |
| `/update_github <user>` | Pull and summarise your public repos |
| `/linkedin` | Paste your profile text |
| `/fact <key> <value>` | Add facts like `notice_period`, `current_ctc`, `expected_ctc`, `location`, `relocate`, `work_authorization` |
| `/template` | Set how your answers should sound |
| `/edit <n> <answer>` | Change answer *n* in the preview (`/edit subject …` / `/edit body …` for email) |
| `/approve` · `/cancel` | Submit or discard the pending application |
| `/status` · `/history` · `/logs` | What it knows · past applications · telemetry |

## Layout

```
bot/            Telegram handlers, in-memory pending state, entry point
intake/         post.py: reads a post once (role, company, contact, what they ask for)
classifier/     link.py: form link vs email vs career page; handles "name [at] x [dot] com"
generator/      answer_generator.py (facts first, model second), prompts.py, humanize.py, llm.py
executor/       form_executor.py (known platforms, driven by form_knowledge_base/*.json), confirm.py
custom_page/    executor.py: DOM scan of any career page, resume upload, submit check
email_service/  router.py: draft + Gmail/mailto links + receipt
context/        resume, GitHub and LinkedIn store, structured facts
idempotency.py  duplicate guard, rate limit, application lifecycle
tests/          python -m tests.test_offline  (no network needed)
```

## Safety

- **Preview first.** Nothing is submitted without `/approve`. If answers are still empty, `/approve` warns once before submitting.
- **No duplicates.** A finished application for the same job and target is blocked (screenshots and re-typed posts of the same job are recognised). Failed or cancelled attempts can be retried.
- **Submit is verified**, not assumed. If the click can't be confirmed you are told, and it is kept as applied so you don't double-apply.
- **Consent boxes are never ticked** for you; personal questions are never answered for you.
- Your data stays local (SQLite + `data/`). Resume text is sent to Groq to write answers.

## Limits (honest list)

- **Email is not sent automatically.** You tap the Gmail link, attach your resume and press send; `/approve` then logs it. Automatic sending needs a Gmail OAuth app (`GOOGLE_CLIENT_ID`, `OAUTH_REDIRECT_URI`) that is not wired up.
- Google Forms is the most tested platform. MS Forms, Typeform and Notion selectors are unverified against live forms.
- Career pages: single-page forms only. Multi-step portals (Workday-style), CAPTCHAs and custom dropdown widgets are flagged, not handled. Forms behind a login can't be opened.
- File uploads other than your resume are not supported.
- `/edit` can't change answers on Typeform (the live step has already moved on).
- Outcome tracking (phase 4) is not built.

## License

MIT
