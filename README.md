# flam — AI Job Application Agent

> [!NOTE]
> **Active Development & Iteration Phase**: This project is currently in active development and iteration. Architecture, features, and platform capabilities are actively evolving.

**flam** is an autonomous AI agent operated via Telegram that parses, drafts, and submits job applications across forms, custom career portals, and email applications on your behalf.

Paste a job post or application link → the agent cross-references the Job Description against your real candidate profile (Resume PDF, GitHub repos, LinkedIn) → generates grounded answers in a natural human voice → you review and approve → Playwright submits the application and sends you a screenshot confirmation receipt.

---

## ✨ Features

- **Multi-Platform Form Engine**: On-demand JSON knowledge base supporting **Google Forms**, **Microsoft Forms**, **Typeform**, and **Notion Forms** with multi-page pagination and multi-field handling (radio, checkbox, dropdown, short/long text).
- **Custom Career Page Automation**: Self-hosted, open-source browser agent (`browser-use` OSS + local Playwright + Groq) that navigates unknown application portals with field confidence scoring.
- **Email Application Flow**: Auto-detects job postings with recruiter emails, generates tailored subject lines and cover notes, and prepares 1-tap OAuth / `mailto:` dispatch with zero stored passwords.
- **Context Grounding & Style Matching**:
  - Upload PDF resumes (extracted via `pdfplumber`).
  - Sync public GitHub repositories with automated Groq README summarization.
  - Paste LinkedIn profile summaries.
  - User-customizable tone & style templates (`/template edit <prompt>`).
  - Strict human-sounding formatting: clean bullet points, natural line breaks, and zero markdown formatting artifacts (`**bold**` or `[links]()`) injected into web forms.
- **Universal Idempotency & Rate Limiting**: DB-enforced `UNIQUE(user_id, jd_hash, form_id)` constraint prevents accidental double-submits, coupled with a configurable daily submission limit.

---

## 🏗️ Architecture

```
flam/
├── bot/                     # Telegram interface & command dispatch
│   ├── main.py              #   Bot entry point & handler registration
│   ├── handlers.py          #   Command handlers (/upload, /update_github, /approve, etc.)
│   └── state.py             #   In-memory pending-application state machine
│
├── form_knowledge_base/     # Deterministic platform DOM matching rules
│   ├── google_forms.json    #   Google Forms selector definitions & confirmation rules
│   ├── ms_forms.json        #   Microsoft Forms DOM structure
│   ├── notion_forms.json    #   Notion Forms DOM structure
│   └── typeform.json        #   Typeform conversational navigation rules
│
├── classifier/              # URL pattern matching & platform router
│   └── link.py              #   Classifies into known form, email, or custom page
│
├── executor/                # Playwright automation layer
│   ├── form_executor.py     #   Unified executor driving Playwright via platform configs
│   └── knowledge_base.py    #   On-demand config loader
│
├── custom_page/             # Self-hosted browser agent for career portals
│   └── executor.py          #   browser-use OSS + Groq vision/LLM fallback
│
├── email_service/           # Email job application router
│   └── router.py            #   OAuth & mailto cover note generator
│
├── context/                 # Candidate context store & parsers
│   ├── store.py             #   Versioned context store & JD-aware repo filtering
│   ├── github.py            #   GitHub API pull + Groq README summarization
│   └── resume.py            #   PDF resume extraction & fact parsing
│
├── generator/               # Answer synthesis & prompt engineering
│   ├── answer_generator.py  #   Core AnswerGenerator class
│   └── prompts.py           #   Grounded prompt templates & human formatting rules
│
├── db/                      # Persistence layer
│   ├── models.py            #   SQLAlchemy models (Users, Applications, Answers, Context)
│   └── session.py           #   Async SQLite session management
│
├── telemetry/               # Logging and monitoring
│   └── logger.py            #   Structured JSON telemetry & event logging
│
├── idempotency.py           # Rate limiting & deduplication guards
├── requirements.txt         # Project dependencies
└── README.md
```

---

## 🚀 Quick Start

### 1. Clone & Setup Environment

```bash
git clone https://github.com/NipunRaj96/Flam.git
cd Flam

# Create and activate virtual environment (Python 3.10+)
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure Environment

Copy the example environment file:
```bash
cp secrets/.env.example secrets/.env
```

Edit `secrets/.env`:
```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_from_botfather
GROQ_API_KEY=your_groq_api_key
MAX_DAILY_SUBMISSIONS=20
VISION_CONFIDENCE_THRESHOLD=0.8
```

### 3. Run the Bot

```bash
python -m bot.main
```

---

## 📱 Telegram Commands

| Command | Description |
|---|---|
| `/start` | Welcome overview and usage guide |
| `/upload` | Upload resume as a **PDF document** or paste text |
| `/update_github <username>` | Sync public repositories & summarize READMEs via Groq |
| `/linkedin` | Paste LinkedIn profile summary & work history |
| `/template` | View or customize tone/style instructions (`/template edit <prompt>`) |
| `/status` | View active candidate context (resume version, GitHub repos, template) |
| `/logs` | View recent telemetry events and activity summary |
| `/approve` | Submit the pending application |
| `/cancel` | Cancel the pending application draft |

---

## 🛡️ Security & Privacy

- **Zero Stored Passwords**: Email workflows use OAuth device tokens or native `mailto:` links; no plain-text passwords or credentials are ever stored.
- **Local Storage**: All context versions, candidate data, and receipts are stored locally in your SQLite database and filesystem.
- **Strict Deduplication**: Applications cannot be submitted twice for the same job description and form URL.

---

## 📄 License

MIT License.
