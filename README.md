# Telegram Ban-Word Bot

Auto-deletes group messages containing banned words / spam commands (`/masuk`, `/join`, …).
Ban list is stored per group in Supabase. Hosted on Railway.

## 1. Telegram (BotFather)
1. Open @BotFather → `/newbot` → copy the **token**.
2. Add the bot to your group and make it **admin** with **Delete messages** permission.

## 2. Supabase
1. Create a project → **SQL Editor** → paste `schema.sql` → Run.
2. **Project Settings → API**: copy the **Project URL** and the **service_role** (or *secret*) key.
   Never put this key in the code or GitHub — only in Railway variables.

## 3. GitHub
Upload all files in this folder to your new repo (or):
```bash
git init && git add . && git commit -m "ban-word bot"
git branch -M main
git remote add origin https://github.com/YOURNAME/YOURREPO.git
git push -u origin main
```

## 4. Railway
1. **New Project → Deploy from GitHub repo** → pick the repo.
2. **Variables** tab, add:
   | Name | Value |
   |---|---|
   | `BOT_TOKEN` | from BotFather |
   | `SUPABASE_URL` | from Supabase |
   | `SUPABASE_KEY` | service_role / secret key |
   | `DEFAULT_BANS` | `/masuk,/join` (optional, applies to all groups) |
   | `EXEMPT_ADMINS` | `true` (admins are never deleted) |
3. Deploy. Logs should say `Bot starting.` No domain/port needed (it uses polling).
   Run only **1 replica** — two copies with the same token will conflict.

## Commands (admins, inside the group)
- `/addban /masuk /join promo t.me/` – add words (space or comma separated)
- `/delban promo` – remove
- `/listban` – show list
- `/status` – uptime, database, delete permission, banned-word count, deleted today/total

## How matching works
- Words starting with `/` → deleted when used as a command anywhere in the message, including `/join@SomeBot`.
- Normal words → whole-word match, case-insensitive (`promo` hits "PROMO!" but not "promotion").
- Anything with symbols (e.g. `t.me/`, `bit.ly`) → substring match.
- Checks text and photo/video captions.
