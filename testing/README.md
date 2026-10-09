# End-to-end testing through Telegram

Method, safety rules and how delivery was measured: [docs/testing.md](../docs/testing.md).

## Setup (once)

1. Create a Telegram API ID and hash for your own account at my.telegram.org.
2. Install Telethon in a virtualenv: `python3 -m venv ~/.venvs/canary && ~/.venvs/canary/bin/pip install telethon`.
3. Write `~/.config/research-agent/telegram-canary.env` (mode 0600; never commit it):
   ```
   TELEGRAM_API_ID=123456
   TELEGRAM_API_HASH=<your api hash>
   TELEGRAM_CANARY_USER_ID=<your numeric user id; must be allowed by the bot>
   TELEGRAM_CANARY_PEER=@your_research_bot
   TELEGRAM_CANARY_PEER_ID=<the bot's numeric id>
   TELEGRAM_CANARY_SESSION=~/.config/research-agent/telegram-canary/user
   ```
4. Create the Telethon session interactively once (it asks for the login code), then `chmod 600` the `.session` file.

## Run

Run as a user that can read Hermes' `gateway_state.json`, but not as root:

```sh
CANARY_PYTHON=~/.venvs/canary/bin/python ./tg_canary.sh smoke
CANARY_PYTHON=~/.venvs/canary/bin/python ./tg_canary.sh product-20
```

Results go to `results/telegram-<suite>-<UTC timestamp>/` (`summary.json`, plus per task `prompt-sent.md`,
`response.md`, `telegram-messages.json` and `result.json`). `results/` is git-ignored: replies can quote your
private context.

## Suites

- `suites/smoke.json`: exact-reply smoke ("CANARY-OK"), a plain chat question and two short cited research
  questions.
- `suites/product-20.json`: the 20 product prompts behind the published delivery numbers. `n01`-`n10` are named
  three-way comparisons and `o01`-`o10` are open-ended.

## Task options

Each suite task may also set:

- `"footer": "none"` sends the prompt without the read-only footer (needed for travel requests, which must write
  their manifest);
- `"followup": {"after_seconds": 120, "text": "..."}` sends a second message while the first turn may still be
  running, to test busy-input handling;
- `"approval_followup": "yes" | "more" | "publish it"` replies once after the first answer settles. This is the
  only way a canary approves anything: use it only where publishing test pages is acceptable.

After the last task the canary sends `/new`, so your next real message starts in a clean session.

