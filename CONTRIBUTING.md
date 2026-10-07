# Contributing to AI Counseling Bot

Thanks for your interest in improving the bot. This project mixes conversation design,
clinical screening logic and a matching engine — so changes are reviewed carefully.

## Where to start

- **Good first issues** — documentation, translations of helper text, test coverage.
- **Engine work** (`internal_ai_engine.py`) — please include before/after examples of a
  recommendation list so the ranking change can be judged.
- **Conversation flow** (`main.py`) — keep the user-facing strings in Persian and
  RTL-friendly; code, comments and docs stay in English.

## Development setup

```bash
git clone https://github.com/Mohammad-Hasan-Kaman/ai-counseling-bot.git
cd ai-counseling-bot
pip install -r requirements.txt
cp .env.example .env    # add a test bot token and your own Bale ID as admin
python main.py
```

Use a **test bot token**, never your production one, while developing.

## Pull request checklist

- [ ] `python -m py_compile *.py` passes (no syntax errors)
- [ ] No secrets, tokens, `.env`, databases or Excel data committed
- [ ] New user-facing strings are Persian and render correctly right-to-left
- [ ] README / docs updated if behaviour changed
- [ ] PR description explains *why* the change is needed

## Reporting bugs

Open an issue with:

1. What you did, what you expected, what happened
2. Python version and OS
3. Steps to reproduce — **remove tokens, phone numbers and user IDs first**

## Safety rules

This tool touches mental-health screening. Never:

- change GHQ-28 wording or scoring without citing the source
- claim the bot gives a medical diagnosis
- commit real user data, even anonymised-looking data

By contributing you agree your work is released under the [MIT License](LICENSE).
