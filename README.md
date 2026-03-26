# coldshot

Terminal-based agentic CRM for B2B cold outreach. AI discovers prospects, researches their pain points, finds the right person — you write one email at a time.

Most B2B outreach tools optimize for volume. coldshot optimizes for **precision**. It runs an AI-powered pipeline that finds companies matching your ICP, qualifies them with web research, walks their org chart to find the exact right decision-maker, and deep-dives into their specific pain points — then opens your `$EDITOR` and gets out of the way.

## The pipeline

```
Discover → Qualify → Find contact → Research → Draft → Send
```

1. **Discover** — Searches [Sumble](https://sumble.com) for companies matching your tech stack and size filters
2. **Qualify** — Claude + web search verifies each company is a real B2B prospect for your product
3. **Find contact** — Walks the org chart level by level (CXO → VP → Director → ...), evaluating each person with AI to find the right decision-maker
4. **Research** — Opus deep-dives into the company's pain points using web search
5. **Draft** — Generates a subject line, opens your `$EDITOR` with full context as comments
6. **Send** — Fires via Gmail API

The pipeline runs continuously in the background — while you're writing an email to one prospect, it's already discovering and researching the next.

## The database is your dataset

Every step of the pipeline is recorded to a local SQLite database — every API call, every LLM qualification, every person evaluation, every email you write and send. This isn't just logging.

coldshot records everything to `data/cold_sales.db`:

- **`discovered_orgs`** — every company seen, with qualification verdict and reasoning
- **`llm_calls`** — every prompt and response (qualification, person evaluation, pain points, subject lines)
- **`targets`** — every person selected, with why they were chosen
- **`outreach`** — every email sent (recipient, subject, body, timestamp)
- **`api_calls`** — every Sumble API call with request/response

With enough outreach cycles, this becomes a fine-tuning dataset for building your own autonomous email agent — one that learns your targeting criteria, your writing style, and what kind of prospects convert.

## Quick start

```bash
git clone https://github.com/drPod/coldshot.git
cd coldshot
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Configure
cp .env.example .env                     # Add API keys
cp coldshot.example.toml coldshot.toml    # Customize your outreach strategy

# Run
python cli.py
```

## Configuration

### `.env` — API keys

```
SUMBLE_API_KEY=your-key
ANTHROPIC_API_KEY=your-key
ANTHROPIC_BASE_URL=              # optional — for proxies / LLM gateways
```

### `coldshot.toml` — outreach strategy

This file defines **what** you're selling and **who** you're targeting. It's gitignored so your playbook stays private.

```toml
[sender]
name = "Your Name"

[product]
name = "Acme"
pitch = "a developer tool that does X for Y teams"
qualifier = "Only say YES if they likely need X."

[targeting]
scoring = [
    "Target the most senior technical person.",
    "Find who actually builds, not who manages.",
]

[research]
focus = [
    "Challenges with monitoring in production",
    "Cost optimization concerns",
]

[discovery]
technologies = ["React", "Node.js"]
min_employees = 50
max_employees = 500
```

See [`coldshot.example.toml`](coldshot.example.toml) for the full template with comments.

### Gmail OAuth

coldshot sends emails through the Gmail API. To set this up:

1. Create a project in [Google Cloud Console](https://console.cloud.google.com)
2. Enable the Gmail API
3. Create OAuth 2.0 credentials (Desktop app)
4. Download the `client_secret_*.json` file to the project root
5. On first run, a browser window opens to authorize — the token is cached in `token.json`

Both `client_secret*.json` and `token.json` are gitignored.

## Architecture

```
cli.py              Main loop — Rich live display + interactive email flow
config.py           Loads coldshot.toml

pipeline/
  discovery.py      Find and qualify companies via Sumble + Claude
  contacts.py       Walk org chart, evaluate people with Claude
  prompts.py        Build prompts from coldshot.toml config
  models.py         Pipeline data models

sumble/
  client.py         Typed Sumble v5 API client
  models.py         Pydantic response models

mailer/
  send.py           Gmail API sender

recorder/
  db.py             SQLite recorder — logs everything
```

## Requirements

- Python 3.12+
- [Sumble](https://sumble.com) API key — company and people data
- [Anthropic](https://anthropic.com) API key — AI qualification, research, and drafting
- Gmail OAuth credentials — for sending

## License

MIT
