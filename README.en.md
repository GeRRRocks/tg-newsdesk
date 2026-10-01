# tg-newsdesk

[Русский](README.md) | **English**

A Telegram bot that auto-posts news to a group with human moderation. It
collects news from sources, rewrites each item into a short post with an LLM
(Claude, GPT, Gemini or DeepSeek — your choice) and sends the draft to the
admins in a private chat. Nothing reaches the group until an admin presses
“Publish”.

It ships configured for car news, but the subject is just the text of the
prompt: sources are topic-neutral, so the bot fits a channel on any subject.

> The bot's interface is available in Russian (default) and English — set
> `BOT_LANGUAGE=en` in `.env`, see [Bot language](#bot-language). Button names
> below are given in Russian with the English label in brackets.

## How it works

1. On schedule (or when “⚡ Сгенерировать” / Generate is pressed) the bot polls
   the enabled sources and takes the newest item it has not seen yet.
2. The LLM rewrites it into a post following the system prompt.
3. Every admin receives the draft with buttons in a private chat: publish it,
   reject it, ask for another version or replace the text with their own.
4. Once approved, the post goes to the target group — into a specific topic if
   one is set. The buttons disappear from the other admins' copies.
5. A draft nobody answers within a day closes on its own.

The bot is fully private: it answers only the admins listed in
`ADMIN_CHAT_IDS`; messages and button presses from anyone else are silently
ignored.

## Server requirements

| | Minimum | Recommended |
|---|---|---|
| Memory | 2 GB | 4 GB |
| CPU | 1 core | 2 cores |
| Disk | 10 GB | 20 GB |
| System | Linux x86-64 with Docker Engine and the compose v2 plugin | Ubuntu 24.04 LTS |

- **Memory.** The containers may use up to 1.3 GB (bot 768 MB, database
  384 MB, backups 128 MB), and the system and the image build need memory
  too. On a 2 GB server `make deploy` creates a 2 GB swap file by itself.
  Going below 2 GB is not recommended: collecting news from several sources
  peaks at 350–400 MB in the bot alone.
- **Disk.** Images and the build cache take about 2.5 GB, the swap file 2 GB;
  the database and backups grow slowly (tens of megabytes).
- **Network.** Outbound internet access is required: to `api.telegram.org`, to
  the API of the chosen LLM and to the source sites. No inbound ports or
  public IP are needed — the bot polls Telegram itself.

Tested on Ubuntu 24.04 (x86-64), Docker 29 and compose 2.40. Other
distributions and ARM have not been tested.

## Quick start (Docker)

All you need is Docker with the compose plugin. The bot, PostgreSQL and backups
run in three containers; the database publishes no ports and lives in an
isolated network with no internet access; data is kept in the named volume
`pgdata`.

```bash
git clone https://github.com/GeRRRocks/tg-newsdesk.git
cd tg-newsdesk
cp .env.example .env             # fill in the values, see below
sudo make deploy                 # build the images and start
```

Before starting, `make deploy`, `make up` and `make update` check the server's
memory: with less than 4 GB of RAM and no swap they create a 2 GB swap file
`/swapfile` (`scripts/setup-swap.sh`) — without it the system simply kills
processes when memory runs out. This step needs root; without it the command
tells you what to run and carries on with the deployment. The size is set with
`SWAP_SIZE_MB`. Running `docker compose up -d --build` directly does not create
swap.

What to put into `.env` and where to get it is covered in
[Telegram setup](#telegram-setup), [LLM provider](#llm-provider) and
[Environment variables](#environment-variables). Don't invent the database
password, generate it: `openssl rand -hex 24`.

Day-to-day management goes through `make` (run `make` with no arguments for the
list):

```bash
make status      # container status
make logs        # live bot logs
make restart     # restart the bot, including after editing .env
make deploy      # rebuild the image and restart the bot after a code change
make update      # pull fresh code from git and deploy
make backup      # dump the database now (to backups/ and to S3 if configured)
make restore FILE=backups/<file>   # restore the database from a dump
make psql        # PostgreSQL console
make down        # stop everything (data in the volume is kept)
```

These are thin wrappers around `docker compose`, which you can also use
directly. After editing `.env` run `make restart` (or `docker compose up -d`):
`docker compose restart` does not re-read the variables.

## Telegram setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and put the token
   into `BOT_TOKEN`.
2. Find the chat_id of every admin (for example with
   [@userinfobot](https://t.me/userinfobot)) and list them comma-separated in
   `ADMIN_CHAT_IDS`. Each admin has to send `/start` to the bot in a private
   chat themselves — otherwise the bot cannot message them.
3. Enable topics in the target group: **Settings → Topics**.
4. Add the bot to the group and make it an admin with **Post Messages** and
   **Manage Topics**. Without the latter, posting into a closed topic fails
   with Telegram's `TOPIC_CLOSED` error.
5. Temporarily put your own chat_id into `TARGET_GROUP_CHAT_ID` (the value is
   required, the bot will not start without it) and start the bot.
6. Send `/get_topic_id` right inside the target topic of the group (not in
   General and not in a private chat) — the bot replies with ready-made
   `TARGET_GROUP_CHAT_ID` and `TARGET_TOPIC_ID` lines.
7. Put them into `.env` and apply: `make restart`.

If an admin posts in the group anonymously or as a business account rather
than from their personal account, `ADMIN_CHAT_IDS` needs the id of the account
the messages actually come from — otherwise the bot ignores them. Check
`from_user` in the bot logs when in doubt.

## Environment variables

All secrets and identifiers live only in `.env` (template: `.env.example`);
nothing is hard-coded. `.env` is not tracked by git.

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | Telegram bot token |
| `AI_PROVIDER` | default LLM: `anthropic`, `openai`, `gemini` or `deepseek`; `anthropic` if unset |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY` | API keys; only the key of the `AI_PROVIDER` LLM is required |
| `AI_MODEL` | model for the `AI_PROVIDER` LLM; optional |
| `ADMIN_CHAT_IDS` | comma-separated admin chat_ids — drafts go to all of them |
| `TARGET_GROUP_CHAT_ID` | chat_id of the target group |
| `TARGET_TOPIC_ID` | id of the topic to post into; optional |
| `TIMEZONE` | schedule time zone (IANA), `Europe/Moscow` by default |
| `DRAFT_INTERVAL_MINUTES` | initial auto-generation interval, 60 by default |
| `DRAFT_EXPIRE_HOURS` | hours after which an unanswered draft closes on its own; 24 by default, `0` — never |
| `BOT_TOPIC` | subject for the default prompt; if unset — “car news” (or its Russian equivalent) |
| `BOT_LANGUAGE` | bot language: `ru` (default) or `en`, see [Bot language](#bot-language) |
| `QA_ENABLED`, `QA_TOPIC_ID`, `QA_USER_DAILY_LIMIT`, `QA_GLOBAL_DAILY_LIMIT` | answers to group members' questions and their limits, see [Questions to the bot in the group](#questions-to-the-bot-in-the-group); off, 3 and 50 by default |
| `BACKUP_TIME`, `BACKUP_KEEP_DAYS` | time of the daily dump (`03:30` by default) and how many days local copies are kept (14) |
| `S3_ENDPOINT`, `S3_REGION`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_PREFIX` | storage for database copies, see [Backups](#backups); optional |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | credentials of the PostgreSQL container (Docker only) |
| `DATABASE_URL` | PostgreSQL connection string (non-Docker runs only) |

In Docker, `DATABASE_URL` from `.env` is ignored: compose builds it from
`POSTGRES_*` and the address of the `db` service.

`DRAFT_INTERVAL_MINUTES` and `BOT_TOPIC` are starting values only. After the
first change made through the menu, the settings stored in the database apply.

## Bot language

`BOT_LANGUAGE` in `.env` sets the language of everything the bot shows to
people: menus and buttons, messages to admins, failure alerts (including those
from the backup container) and the default prompt.

```bash
BOT_LANGUAGE=en                  # ru (default) or en
BOT_TOPIC=car news               # subject for the English prompt
```

There is one language per installation, changed with a restart:
`make restart`. Things to know:

- **The prompt decides the language of the posts.** With `en` the default
  prompt is in English and asks for posts in English. A prompt already changed
  through the “🏷 Prompt” menu stays as it is — switching the language does not
  touch it; the reset button in the same menu brings the default prompt back.
- **`BOT_TOPIC` is inserted into the prompt as is**, so for an English bot
  write the subject in English or leave it out — “car news” is used then.
- **Logs stay in Russian.** The installation and swap scripts
  (`scripts/install.sh`, `scripts/setup-swap.sh`) speak the selected language.

## LLM provider

Posts can be written by one of four LLMs. Keys are set in `.env`, and you can
switch between them right from the bot menu.

| `AI_PROVIDER` | LLM | Key variable | Where to get a key | Default model |
|---|---|---|---|---|
| `anthropic` | Claude | `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) | `claude-opus-5` |
| `openai` | GPT | `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com) | `gpt-5.4-mini` |
| `gemini` | Gemini | `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com) | `gemini-3.8-flash` |
| `deepseek` | DeepSeek | `DEEPSEEK_API_KEY` | [platform.deepseek.com](https://platform.deepseek.com) | `deepseek-flash` |

**Configuration in `.env`.** The minimum is one LLM: its name in `AI_PROVIDER`
and its key. Without that key the bot does not start and the logs name the
missing variable. The other keys may stay empty, or you can fill in several —
those LLMs then become available in the menu.

```bash
AI_PROVIDER=deepseek
DEEPSEEK_API_KEY=...             # key of the default LLM — required
ANTHROPIC_API_KEY=...            # optional: a second LLM for the menu
```

**Switching in the bot.** The “🤖 Нейросеть” (LLM) button shows which LLM and
model write the posts now, and lists all four. The current one is marked ✅,
LLMs without a key — 🔒. Switching takes effect immediately, without a
restart: the next draft is written by the selected LLM. A key cannot be entered
through the bot — to unlock an LLM, add its key to `.env` and run
`make restart`.

The menu choice is stored in the database and takes precedence over
`AI_PROVIDER`. The `.env` value is used until something is chosen in the menu,
and when the key of the chosen LLM has been removed from `.env`.

**Model.** `AI_MODEL` overrides the default model only for the `AI_PROVIDER`
LLM; the others use their model from the table. Providers rename models: if a
default model stops working, the logs contain the provider's error — set a
current model in `AI_MODEL`.

The system prompt is shared by all LLMs. The active LLM and model are logged
when the bot starts.

## Operating the bot

Everything is done with buttons; `/start` opens the main menu:

| Button | What it does |
|---|---|
| 📋 Источники (Sources) | list of sources and draft statistics; a tap opens a card with Disable/Enable and Delete |
| ➕ Добавить (Add) | wizard: address of a site, feed or Telegram channel → the bot detects the type → a name or “Skip” |
| ⚡ Сгенерировать (Generate) | collect a news item and generate a draft now, without waiting for the schedule |
| ⏱ Расписание (Schedule) | auto-generation mode and parameters, see below |
| 🏷 Промт (Prompt) | view, reset or replace the LLM system prompt: enter it manually or generate it from a description |
| 🤖 Нейросеть (LLM) | which LLM writes the posts; switch between those with keys in `.env`, see [LLM provider](#llm-provider) |
| 🚫 Фильтр (Filter) | stop words and required words for news, see [Word filter](#word-filter) |
| 🏓 Пинг (Ping) | check that the bot is alive |

The only command that has no button is `/get_topic_id`.

### Draft

Every draft has four buttons:

| Button | What it does |
|---|---|
| ✅ Опубликовать (Publish) | sends the post to the group |
| ❌ Отклонить (Reject) | closes the draft; this news item is not offered again |
| 🔄 Другой вариант (Another version) | the LLM rewrites the text of the same item |
| ✏️ Править (Edit) | the bot asks for your own text, which replaces the generated one |

After “Another version” and “Edit”, the old cards are closed with a note for
all admins and everyone receives a new one — that is the one to publish. Your
own text is published as is, without markup.

An unanswered draft closes after `DRAFT_EXPIRE_HOURS` hours (24 by default):
the buttons disappear and a note appears under the card. In the statistics
such drafts are counted separately as “expired”, not “rejected”.

### Schedule

Two modes, switched in the “⏱ Расписание” menu and applied immediately,
without a restart:

- **⏱ Interval** — every N minutes: presets or your own number.
- **📅 Days and time** — selected weekdays and one or more times of day (for
  example 09:00 and 18:00) shared by all selected days. Times are in the
  `TIMEZONE` zone. Until at least one day and one time are chosen, the
  interval keeps working.

### Sources

The bot detects the source type from the address you send. You can send a
site's main page: the bot checks whether the address itself is a feed, looks
for an RSS feed declared on the page, then tries common feed paths (`/rss`,
`/feed`, `/rss.xml` and the like) and, if it finds one, saves the source with
the feed address. If there is no RSS but the page has links to articles, the
source is added as HTML. If nothing is found, the bot warns you and offers to
choose the type manually.

Types:

- **RSS** — the address of an RSS/Atom feed.
- **HTML** — a regular page with a list of news, for sites without RSS. The
  bot finds article-like links on it and takes the title, description and
  image from each article's Open Graph tags. The method is generic and not
  tunable per site, so quality depends on the site's markup.
- **Telegram channel** — a public channel. Send `@name` or a `t.me/name` link
  in the wizard and the type is detected. The bot reads the channel's web
  preview (`t.me/s/name`) — roughly the 20 latest posts, without logging in to
  Telegram and without adding the bot to the channel. The first line of a post
  becomes the title; the draft links to the post itself. Private channels and
  channels with the web preview disabled cannot be read this way — such a
  source ends up in a failure alert. The bot cannot tell a channel's ads from
  news: they are handled by the word filter or the “Reject” button.

The source list shows the overall draft totals, a source card shows its own:
how many were published, rejected, are pending and expired. These numbers show
which source is worth disabling.

The bot only fetches public addresses (`http`/`https`) and does not read
responses larger than 3 MB — such sources are skipped with a warning in the
logs.

### Word filter

The “🚫 Фильтр” button sets two lists of words or phrases shared by all
sources:

- **Stop words** — an item containing any of them is skipped.
- **Required words** — if the list is not empty, only items containing at
  least one of them are taken.

Words are sent in one message, comma-separated, and replace the previous list.
Matching is a case-insensitive substring search in the title and summary:
“crossover” also catches “crossovers”. The filter runs before the LLM is
called, so filtered-out items cost nothing.

### Failure alerts

The bot messages the admins itself when something breaks:

- the LLM failed to write a scheduled draft — one message per failure streak
  and another when generation recovers;
- a source fails to open or returns no news three runs in a row;
- an unexpected error happened in the bot — on a button press or in a
  scheduled job. Identical errors are reported at most once every 10 minutes;
  details are in `make logs`;
- a database backup failed or could not be uploaded to S3.

Failure counters reset when the bot restarts. If the bot itself or the server
is down, there will be no messages — that has to be monitored separately.

### Subject and prompt

The subject is defined only by the LLM system prompt:

- **`BOT_TOPIC` in `.env`** — inserted into the default prompt;
- **the “🏷 Промт” button** — replaces the whole prompt text on the fly. That
  is also where you set post length, tone, format and anything else.

There are two ways to replace the prompt:

- **“✏️ Задать вручную” (Enter manually)** — send a ready prompt text;
- **“✨ Сгенерировать по описанию” (Generate from a description)** — describe
  in your own words what the posts should be like (up to 1000 characters) and
  the selected LLM writes the prompt itself. The bot shows the result with the
  buttons “✅ Сохранить” (Save), “🔄 Другой вариант” (Another version) and
  “❌ Отмена” (Cancel): the active prompt changes only after Save. Each
  generation is one request to the LLM.

To move the bot to another subject, change the prompt and the set of sources —
no code changes needed. The prompt does not depend on the selected LLM.

### Questions to the bot in the group

A group member can mention the bot and ask a question — the bot replies
through the selected LLM. The question must start with the word “Question:”
(or “Вопрос:”), otherwise the bot does not notice it — this keeps it out of
conversations between members who merely mention it:

```
@bot_username Question: how does a steering rack work?
```

Case does not matter, and the mention may come before the marker or after the
question (`Question: how does a steering rack work? @bot_username`). Both
words are accepted whatever the bot language is.

The feature is off by default because every answer is a paid LLM request.
Enable it in `.env`; it takes effect after `make restart`:

```env
QA_ENABLED=true
QA_TOPIC_ID=               # topic for questions; empty — any topic of the group
QA_USER_DAILY_LIMIT=3      # questions per member
QA_GLOBAL_DAILY_LIMIT=50   # answers for all members together
```

- **Subject.** The bot answers questions on the channel's subject
  (`BOT_TOPIC`) in the broad sense — not only about news, but also about how
  things work, terms, choosing and maintenance. It politely declines
  off-topic questions; such a refusal also spends an attempt, because the LLM
  writes it.
- **Limits.** Both are counted over 24 hours from the first question: once
  the window has passed, the counter is reset and a new window starts with
  the next question. Counters are stored in the database and survive a
  restart. If the LLM did not respond, the attempt is not counted.
- **When a limit is used up,** the bot says once when asking will be possible
  again and silently skips further questions until then.
- **Admins** from `ADMIN_CHAT_IDS` are not limited.
- **Where it works.** Only in the group from `TARGET_GROUP_CHAT_ID`; in other
  chats the bot does not react to mentions. If `QA_TOPIC_ID` is set, the bot
  answers only in that topic and silently skips mentions in the others; send
  `/get_topic_id` inside the topic to learn its id. Without `QA_TOPIC_ID` — in
  any topic. A question
  is up to 500 characters. A message without a mention of the bot or without the
  “Question:” marker is not treated as a question, even when it is a reply to
  the bot's message.

The LLM has no internet access: it can be wrong about prices, dates and
recent events. Answers are posted to the group right away, without
moderation. The question text is sent to the provider of the selected LLM;
in the database the bot stores only the member's Telegram id and their
counter for the current day.

## Backups

The `backup` container dumps the database every day at `BACKUP_TIME` into the
`backups/` folder next to the project (a compressed file
`tg_news_bot_<date>_<time>.sql.gz`). Local copies older than
`BACKUP_KEEP_DAYS` days are deleted.

**Upload to S3.** To also keep copies off the server, put the parameters of an
S3-compatible storage into `.env` and run `make up`:

```bash
S3_ENDPOINT=https://storage.yandexcloud.net   # storage address, without the bucket name
S3_REGION=ru-central1
S3_BUCKET=my-bot-backups
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_PREFIX=tg-news-bot/                        # optional
```

The key only needs permission to write objects to that bucket. To check the
setup run `make backup` — the output must contain the line “отправлено в S3”
(uploaded to S3). The script never deletes anything from S3: retention there
is set with a lifecycle rule in the bucket settings. If a copy fails or is not
uploaded, the `backup` container reports it to the admins in Telegram; its log
is `make logs-backup`.

**Restore.**

```bash
make restore FILE=backups/tg_news_bot_2026-10-01_033000.sql.gz
```

The command asks for confirmation, stops the bot, saves the current database
as a separate dump, replaces it with the contents of the file and starts the
bot. To only look inside a dump without touching the live database, restore it
into a separate one:

```bash
RESTORE_DB=check make restore FILE=backups/<file>
```

**Rolling back the code.** Drafts with the “expired” status are understood
only by code starting from commit `496bec1`. Before rolling back to an earlier
version, turn them into “rejected”: in `make psql` run
`UPDATE posted_news SET status = 'REJECTED' WHERE status = 'EXPIRED';`

## Installing without Docker

### Automatically (Ubuntu, systemd)

The script first asks for the language — Russian or English: all further
questions use it, and it also becomes the bot language (`BOT_LANGUAGE`). It
then installs Python and PostgreSQL, creates a swap file on low-memory
servers, creates a venv, the database and `.env` (it asks for the LLM, its key
and the tokens interactively) and registers a systemd service:

```bash
git clone https://github.com/GeRRRocks/tg-newsdesk.git
cd tg-newsdesk
sudo bash scripts/install.sh
```

```bash
systemctl status tg-news-bot     # status
journalctl -u tg-news-bot -f     # live logs
systemctl restart tg-news-bot    # restart, including after editing .env
```

After installation, do steps 6–7 of [Telegram setup](#telegram-setup) and
restart the service.

### Manually

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -c constraints.txt
cp .env.example .env             # fill in the values, including DATABASE_URL
python main.py
```

A running PostgreSQL is required; tables are created automatically on first
start.

### Moving to Docker

To move an existing installation (a `python main.py` process and PostgreSQL on
the host) into Docker, stop the old bot process and run:

```bash
bash scripts/docker-migrate.sh
```

The script dumps the old database, restores it into the container, compares
row counts per table and only then starts the bot. The old database is not
modified.

## Project layout

Stack: Python 3.12, [aiogram 3](https://docs.aiogram.dev/), SQLAlchemy 2 +
asyncpg (PostgreSQL), APScheduler, httpx, feedparser, BeautifulSoup4, the
Anthropic Python SDK (the other LLMs are called over HTTP with httpx).

```
main.py                 # entry point
bot/
  config.py             # settings from .env (Pydantic Settings)
  i18n.py               # picks a text in the bot language
  locales/              # bot texts: ru.py and en.py
  filters/
    admin.py            # IsAdmin — lets only admins through
  handlers/
    base.py             # /start, main menu, /get_topic_id
    sources.py          # listing, adding, toggling and deleting sources
    generate.py         # the “Generate” button
    schedule.py         # schedule menu
    prompt.py           # prompt menu
    qa.py               # answers to group members' questions
    ai.py               # LLM selection menu
    news_filter.py      # word filter menu
    moderation.py       # draft card: publishing, editing, auto-expiry
    errors.py           # catch-all for unexpected errors
  services/
    news.py             # fetching sources, parsing RSS, picking the newest item
    scraper.py          # collecting news from HTML pages
    source_detect.py    # detecting the source type from an address, RSS discovery
    telegram_channel.py # collecting posts from public Telegram channels
    ai.py               # rewriting an item with the selected LLM
    news_filter.py      # word filter for news
    qa.py               # limits for questions to the bot in the group
    alerts.py           # failure messages to admins
    scheduler.py        # schedule and the collect → generate → send cycle
  db/
    models.py           # Source, PostedNews, Draft, DraftNotification, BotSetting, Topic, QaUsage
    session.py          # async engine and sessions
Dockerfile              # bot image
Dockerfile.backup       # backup image
docker-compose.yml      # bot + PostgreSQL + backups
Makefile                # short management commands (make help)
requirements.txt        # direct dependencies
constraints.txt         # exact versions of all packages; the image is built from them
scripts/
  install.sh            # non-Docker installation (systemd)
  docker-migrate.sh     # moving an existing installation into Docker
  backup.sh             # database dump and S3 upload (runs in the backup container)
  restore.sh            # restoring the database from a dump
  setup-swap.sh         # swap file for low-memory servers
```

Tables are created when the bot starts; there are no migrations: schema changes
go into `bot/db/models.py`, and new columns of existing tables also into
`init_models()` (`bot/db/session.py`).

## Branches and versions

- **`main`** — the stable version. It is what `git clone` gets and what the
  bot should be deployed from. `make update` only brings published versions
  into it.
- **`beta`** — ongoing development: new features appear here and move to
  `main` after testing. To try it: `git checkout beta`, then `make deploy`.

Every publication to `main` gets a version number and a git tag (`v1.0.0`,
`v1.1.0`, …); the list of changes is in [CHANGELOG.md](CHANGELOG.md) (in
Russian). To install a specific version: `git checkout v1.0.0 && make deploy`.

The bot adds new database columns on start but never removes them. Going back
from `beta` to `main`, or to an older version, on the same database may
therefore need manual steps — they are described in the changelog entry of the
version that changed the schema.

## License

[MIT](LICENSE) — the code may be freely used, modified and distributed,
provided the license text and the author attribution are kept.
