# Promotional Slot Management Bot

A Discord bot that manages promotional channels ("slots") for members of your server.
Every slot is a public channel that members can read but not post in. Only the slot owner
can publish, and only within a mention allowance set by the plan.

Every command works two ways: as a slash command (`/myslot`) and as a dot command (`.myslot`).

---

## 1. Requirements

- Python 3.11 or newer
- A Discord application with a bot user
- A server where you have the Administrator permission

## 2. Discord Developer Portal setup

1. Open https://discord.com/developers/applications and click **New Application**.
2. Go to **Bot**. Click **Reset Token** and copy it. Store it only in `.env`.
3. On the same page, under **Privileged Gateway Intents**, enable all three:
   - **Presence Intent** (reads custom status for free-slot verification)
   - **Server Members Intent** (finds members and tracks departures)
   - **Message Content Intent** (required for dot commands and mention monitoring)
4. Save changes.

Privileged intents are limited to bots in fewer than 100 servers until you apply for verification.

## 3. Invite the bot

Go to **OAuth2 → URL Generator**:

- Scopes: `bot`, `applications.commands`
- Bot permissions:
  - View Channels
  - Send Messages
  - Manage Channels
  - Manage Messages
  - Read Message History
  - Embed Links
  - Mention Everyone
  - Add Reactions

Integer value for the permissions: **224336**. Use this URL, replacing `CLIENT_ID`:

```
https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=224336
```

The bot's role must sit **above** any role it needs to manage, and it must be able to see the
category where slots are created.

## 4. Local installation

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
```

Edit `.env`:

| Variable | Meaning |
|---|---|
| `DISCORD_TOKEN` | Bot token from the Developer Portal |
| `GUILD_ID` | Your server ID (enable Developer Mode, right-click the server, Copy ID) |
| `OWNER_ID` | Your user ID. The owner always has administrator rights in the bot |
| `ADMIN_ROLE_IDS` | Optional. Comma-separated role IDs that count as administrators |
| `COMMAND_PREFIX` | Dot-command prefix. Default `.` |
| `DATABASE_PATH` | SQLite file. Default `data/slots.db` |
| `KEY_PEPPER` | Secret of 32+ characters used to hash recovery keys. **Never change it after launch** or existing keys stop working |

Generate a pepper:

```bash
python -c "import secrets;print(secrets.token_urlsafe(48))"
```

## 5. Starting the bot

```bash
python main.py
```

On first start the bot creates the database, applies the schema, and syncs slash commands to your
server. Slash commands can take up to a minute to appear the first time.

## 6. First-time configuration (inside Discord)

Run these in any channel as an administrator:

1. `/setlogchannel #admin-logs`
2. `/setcategory #Promo Slots` (default category for new slots)
3. `/setcategory #Archive scope:archive` (optional, for archived slots)
4. `/createplan Free "Free" 30 10 free:yes` (free plan: 30 days, 10 slots)
5. `/createplan Gold "$10" 30 5 here_per_day:2 everyone_per_week:1` (paid plan)
6. `/setclaimpanel #claim-here`
7. `/setup` to check the checklist

Plan names with spaces take underscores: `/createplan Gold_Plus "$25/mo" 30 3`.

## 7. Free-slot verification

Discord does not expose the About Me bio to bots, so verification uses **custom status** only.

1. `/setrequirement PROMOTING IN EXAMPLE SERVER`
2. `/setrequirementstatus on`

Members put the phrase in their custom status and press **Claim Slot**. If the bot can't see it,
an administrator can approve the request with `/verify-user @member`. Nobody is approved
automatically when verification fails.

## 8. Mention allowances and penalties

Each plan sets its allowance:

- `here_per_day`: uses of `@here` per rolling 24 hours (default 2)
- `everyone_per_week`: uses of `@everyone` per rolling 7 days (default 1)

Members publish through `/post-promo` (or `.post-promo`). The bot posts the message itself,
so it can enforce the allowance. If a member mentions `@everyone` or `@here` directly in
their channel and exceeds the allowance, the message is deleted and the slot is removed.

Each removal counts toward an automatic blacklist. The default is 3 removals (`/setthreshold`).

## 9. Recovery keys

- Each slot receives a unique key such as `SLOT-X7K9-P2M8-Q4L1`, sent by DM once.
- Only a keyed HMAC of the key is stored. The key itself is never saved or logged.
- `/recover-slot <key>` moves a slot to a new account, but only when the original owner has
  left the server. This prevents taking another active member's slot.
- `/regenerate-key` replaces the key and invalidates the old one.
- `/admin-recover <key> @member` lets an administrator move a slot regardless of owner status.

## 10. Command reference

**Members** (slash and dot):
`/claim-slot`, `/available-slots`, `/slot-rules`, `/slot-info`, `/myslot`, `/renew-slot`,
`/recover-slot`, `/regenerate-key`, `/find <text>`, `/post-promo <none|here|everyone> <text>`

**Administrators** (slash and dot):
- Setup: `/setup`, `/setlogchannel`, `/setcategory`, `/setexpiryaction`, `/setthreshold`,
  `/setgltext`, `/setclaimpanel`
- Verification: `/setrequirement`, `/setrequirementstatus`, `/remove-requirement`, `/verify-user`
- Plans: `/createplan`, `/editplan`, `/deleteplan`, `/listplans`, `/setplanlimit`
- Payments: `/approvepayment`, `/rejectpayment`
- Slots: `/createslot`, `/giveslot`, `/admin-recover`, `/removeslot`, `/deleteslot`,
  `/ban-slot`, `/unban-slot`, `/extend-slot`, `/setexpiry`, `/allslots`, `/searchslot`, `/slotstats`
- Members: `/blacklist`, `/unblacklist`, `/resetuser`

Dot examples: `.find gl`, `.claim-slot`, `.editplan Gold price $12`, `.extend-slot 4 14`.

Editing a plan field uses a field name: `name`, `price`, `duration`, `description`, `benefits`,
`here`, `everyone`, `category`, `active`, `free`, `permissions`. For permissions, use the
format `attach_files=yes,embed_links=no`.

## 11. Expiry

- The bot checks every 15 minutes.
- Reminders go out at 7 days, 3 days and 24 hours before expiry. Each reminder is sent once,
  and the record survives restarts.
- Expired slots are handled per `/setexpiryaction`: `archive`, `disable` or `delete`.
- A channel is deleted only after the bot confirms a matching slot record exists.

## 12. Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The test suite covers key generation and hashing, secret scrubbing, configuration validation,
the one-open-slot rule, transaction rollback, and persistence across restarts. It does not connect
to Discord. Live behavior should be checked in a test server before you go public.

## 13. Project layout

```
main.py              entry point, error handling, startup
bot/config.py        environment configuration and colors
bot/db.py            SQLite schema, transactions, settings
bot/security.py      key generation and keyed hashing
bot/services.py      all business rules and channel operations
bot/views.py         embeds and interactive panel
bot/ui.py            shared embeds, pagination, confirmation
bot/cogs/member.py   member commands
bot/cogs/admin.py    administrator commands
bot/cogs/monitor.py  expiry loop and channel/member listeners
tests/test_core.py   automated tests
```

## 14. Moving to PostgreSQL later

All database access goes through `bot/db.py`, and business logic uses plain SQL with `?`
placeholders. Porting means replacing the `Database` class with an asyncpg-backed version that
keeps the same `one`, `all`, `write`, `tx` and `setting` methods. The schema is standard SQL apart
from `AUTOINCREMENT`, which becomes `GENERATED ALWAYS AS IDENTITY`.

---

## 15. Hosting on Railway

The bot runs as a background worker. It does not need a public URL or a port.

### A. Put the code on GitHub

1. Create a **private** GitHub repository.
2. Upload the contents of the `slot_bot` folder to it (not the zip file). Do not upload `.env`.

### B. Create the Railway service

1. In Railway, click **New Project → Deploy from GitHub repo** and pick the repository.
2. Railway detects the `Dockerfile` and `railway.json` and builds automatically.

### C. Add the environment variables

Open the service, go to **Variables**, and add these. Never put them in the code.

| Variable | Value |
|---|---|
| `DISCORD_TOKEN` | Your bot token from the Developer Portal |
| `GUILD_ID` | Your server ID |
| `OWNER_ID` | Your Discord user ID |
| `ADMIN_ROLE_IDS` | Optional. Comma-separated role IDs |
| `COMMAND_PREFIX` | Optional. Default `.` |
| `KEY_PEPPER` | A 32+ character random secret. Generate one locally with `python -c "import secrets;print(secrets.token_urlsafe(48))"` |

Do not set `DATABASE_PATH`. The Dockerfile already sets it to `/data/slots.db`.

### D. Add a persistent volume (required)

Without a volume, every redeploy wipes the database, including every slot and recovery key.

1. In the service, go to **Settings → Volumes → Add Volume**.
2. Set the mount path to **`/data`**.
3. Redeploy.

### E. Check the deploy

Open **Deploy Logs**. You should see `Synced N slash commands to guild ...` and `Logged in as ...`. If the bot is online in Discord and the slash commands appear, setup is complete.

### F. Common problems

- **"Missing required environment variable"**: a required variable is missing or empty in Variables.
- **"KEY_PEPPER must be at least 32 characters"**: the value is too short.
- **Slash commands don't appear**: wait a minute, then check that `GUILD_ID` is correct and the bot was invited with the `applications.commands` scope.
- **Dot commands or mention monitoring don't respond**: check that the Message Content Intent is enabled in the Developer Portal.
- **Bot keeps restarting**: read the error at the bottom of Deploy Logs. Railway retries up to 10 times, then stops.

### G. Changing settings later

Change values in the Variables tab and Railway redeploys automatically. Never change `KEY_PEPPER` after launch.
