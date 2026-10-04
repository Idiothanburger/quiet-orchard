# quiet-orchard

Posts new releases from a list of artists to a Discord channel. The source is the web player at `music.amazon.com.au`, used as a guest with no login.

It runs on GitHub Actions in a public repo (public repos get unlimited free Actions minutes), started every 30 minutes by cron-job.org. It's pure Python standard library, so there's nothing to install.

The web player's API is unofficial and undocumented. It can change or stop working without notice.

## How a run works

1. **Session:** start a guest session (`config.json`).
2. **Scan:** read the newest-first release list (20 per page) of every artist in `artists.txt`. Requests are paced at 2 per second with 2 workers sharing one limiter, so 309 artists take about 3 minutes.
3. **New artists:** an artist seen for the first time is **baselined**: their listed releases are recorded silently, with no extra requests. Adding an artist never floods the channel.
4. **Known artists:** an unseen release gets one album-page request for its date, track count, length, explicit flag and copyright. If it's dated within the last 7 days (NZ date) or later, it's alerted. Older ones are recorded silently.
5. **Posting:** a release is marked seen **only after Discord confirms delivery**, so a failed post is retried next run.
6. **Saving:** state is written once, to `seen_releases.json`, and the workflow commits it back to `main`.

Other behavior:
- **Release type:** Amazon labels everything "Album", so the type is guessed. 1–3 tracks under 30 minutes is a Single, up to 6 tracks under 30 minutes is an EP, and anything else is an Album. A title ending in "EP" counts as an EP.
- **Explicit and clean versions:** these are separate releases on Amazon. Same title, date and track count are sent as one alert, and both are marked seen.
- **Releases with two tracked artists:** a release credited to two tracked artists alerts once. A collab with a newly added artist still alerts, because known artists are processed before new ones are baselined.
- **Bursts:** when every release on a known artist's first page is unseen, it reads page 2 as well.
- **Blocking:** a 429, 403 or non-JSON reply (such as a CAPTCHA page) stops all requests for the rest of the run. Amazon sends no `Retry-After`. Unchecked artists are retried next run.
- **Unavailable artists:** an artist page that answers "Service error" counts as a failed check, never as "no releases".
- **Failure warning:** if more than 10% of artists fail in a run, or the session can't start, one warning goes to Discord, at most once every 6 hours.
- **Missing date:** a release whose album page has no date is alerted rather than skipped.
- **Stuck requests:** each response must arrive within 60 seconds, and scanning stops after 10 minutes (a normal run takes about 3). Artists still unchecked then are retried next run. State is still saved, and the process exits without waiting for a stuck connection.
- **Featured appearances:** an artist's release list on Amazon only has releases where they're a main artist, so a release where a tracked artist is only featured (for example "Song (feat. Artist)" by someone else) isn't found.

## Setup

### 1. Discord webhook
Open the channel's **Settings**, then **Integrations → Webhooks → New Webhook → Copy Webhook URL**.

### 2. Artist list
`artists.txt` has one Amazon artist ID per line (such as `B07L2WLZHN`), or a `music.amazon.com.au/artists/...` link. Anything after `#` is a comment.

`tools/match_artists.py` builds this list from the Apple Music tool's artists. It searches Amazon for each name and confirms matches by comparing release titles. If a name search fails, it searches the artist's release titles instead. It won't overwrite a hand-edited `artists.txt` without `--overwrite`.

### 3. Test locally
```bash
cp .env.example .env
```

Put the webhook URL in `.env`, then send a test message:

```bash
python notifier.py --test-discord
```

Then do a full scan without posting or saving anything:

```bash
python notifier.py --dry-run
```

### 4. GitHub repo (public)
1. Push this folder to a public repo. `.env` and local working files are ignored.
2. Go to **Settings → Secrets and variables → Actions** and add the repository secret `DISCORD_WEBHOOK_URL`.
3. Go to **Actions → Release Monitor → Run workflow** for the first run. It baselines every artist without alerting and commits `seen_releases.json`.

What's public: the code, `artists.txt` and `seen_releases.json`, which holds release names, dates and links.

### 5. cron-job.org trigger
Create a fine-grained GitHub token that has access to this repo only, with **Contents: Read and write**. That's the permission `repository_dispatch` needs. Then set up a cron-job.org job that runs every 30 minutes:

- URL: `https://api.github.com/repos/<your-user>/quiet-orchard/dispatches`
- Method: `POST`
- Headers:
  - `Accept: application/vnd.github+json`
  - `Authorization: Bearer <token>`
  - `X-GitHub-Api-Version: 2022-11-28`
- Body: `{"event_type": "check-releases"}`

GitHub's own `schedule:` trigger isn't used because it skips and delays runs under load.

### 6. Trial period, then go live
The workflow starts with `SILENT: 'true'`. Runs scan and record releases and log what they would have posted, but send nothing to Discord. After a day without problems, change it to `'false'` in `.github/workflows/monitor.yml`.

## Settings

Set these in `.env` locally or under `env:` in `.github/workflows/monitor.yml`.

| Variable | Default | Meaning |
|---|---|---|
| `REQUESTS_PER_SECOND` | `2` | Cap across all workers. Unpaced bursts of about 13 per second were blocked in testing |
| `CONCURRENCY` | `2` | Parallel workers |
| `MAX_RELEASE_AGE_DAYS` | `7` | Alert window, counted in NZ calendar days |
| `MAX_PAGES` | `2` | Most pages read per artist when a whole page is new |
| `FAILURE_WARN_RATIO` | `0.10` | Share of failed artists that triggers a Discord warning |
| `WARNING_COOLDOWN_HOURS` | `6` | Minimum gap between warnings |
| `RUN_DEADLINE_SECONDS` | `600` | Stop scanning after this long; unfinished artists are retried next run |
| `SILENT` | `false` | Record releases as seen but don't post (trial period) |
| `DRY_RUN` | `false` | Don't post or save |
| `AMAZON_DOMAIN` | `music.amazon.com.au` | Web player domain |

The command-line options are:
- `--dry-run` and `--silent`: the same as the settings above.
- `--test-discord`: sends one test message to the webhook.
- `--force-notify`: alerts recent releases from newly added artists instead of baselining them.

## Tools
- `tools/match_artists.py`: builds `artists.txt` from the Apple Music tool's list. It writes a review CSV and resumes from a cache, and `--retry-unsure` searches again for unsure matches.
- `tools/nz_timing.py`: logs which releases appear at each given UTC time. Run it across NZ and Sydney midnight to see which one the site follows.

## Tests
```bash
python -m unittest discover -s tests
```
