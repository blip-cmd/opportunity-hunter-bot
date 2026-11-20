# opportunity-hunter-bot

Scheduled job and scholarship hunters. This repo contains code only. Every
profile, config, tracker and log lives in a separate private data repo that the
workflows check out at run time with a repo-scoped deploy key.

- `jobhunt/`: every 2 days. Job boards, ATS feeds and opportunity RSS, scored by a free LLM, emailed as a ranked digest.
- `scholarhunt/`: weekly. Scholarship RSS, watched lab pages and programme deadline alerts.

## Secrets (Settings → Secrets and variables → Actions)

| Secret | What |
|---|---|
| `PRIVATE_REPO` | `owner/name` of the private data repo |
| `DATA_REPO_KEY` | Private half of an SSH deploy key with write access, added to the data repo only. No expiry. |
| `GMAIL_ADDRESS` | Sender Gmail (a dedicated account) |
| `GMAIL_APP_PASSWORD` | 16-character app password for it |
| `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY` | Free LLM keys, at least one. Each may hold several comma-separated keys, used in turn so a rate limit or used-up quota on one key does not stall the run. |

## Rules for this public repo

- Never add `pull_request_target` or any trigger that runs untrusted code with secrets.
- Never print hunt output in a workflow. Full logs go to `search/logs/` in the private repo.
- Change dependencies only by regenerating `requirements.lock` with `pip-compile --generate-hashes` (Python 3.13).
- Actions are pinned by commit SHA. Update the SHA and the version comment together.
