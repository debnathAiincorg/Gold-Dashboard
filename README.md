# Gold Dashboard

Fetches the 22K gold rate per gram from Tanishq, ABP Live, and Times of India,
and shows it on a small dashboard. Runs on a schedule via GitHub Actions
(triggered externally by Power Automate) and publishes to GitHub Pages.

## Architecture

- **`gold_rate.py`** -- scrapes the three sources with Playwright, prints a
  console summary, and writes `gold_rate_data.json`.
- **`.github/workflows/update-gold-rate.yml`** -- runs `gold_rate.py` in
  GitHub Actions and commits `gold_rate_data.json` back to the repo if it
  changed. Triggered by `repository_dispatch` (external POST, e.g. from Power
  Automate) or manually via `workflow_dispatch`.
- **`gold_dashboard.html`** -- single self-contained page (Chart.js via CDN)
  that fetches `gold_rate_data.json` and renders a bar chart + summary table.
  Served by GitHub Pages, which unlike `file://` supports `fetch()` over
  HTTPS with no local server needed. Falls back to an embedded, possibly
  stale snapshot if opened directly as a local file.

## 1. Enable GitHub Pages

The site files (`gold_dashboard.html`, `gold_rate_data.json`) live at the
**repo root**, so:

1. Push this repo to GitHub (see below if you haven't yet).
2. On GitHub: **Settings -> Pages**.
3. Under **Build and deployment -> Source**, choose **Deploy from a branch**.
4. Under **Branch**, choose **`main`** and folder **`/ (root)`**, then **Save**.
5. Wait a minute or two for the first deploy. The URL will be:
   `https://<your-username>.github.io/<repo-name>/gold_dashboard.html`
   (shown at the top of the Pages settings page once live -- confirm the
   exact URL there and update `LIVE_PAGES_URL` near the top of the second
   `<script>` block in `gold_dashboard.html` if it differs).

Pages redeploys automatically whenever `main` changes -- including the
automated commits from the workflow below.

## 2. Create a Personal Access Token (for Power Automate)

Power Automate needs a token to call GitHub's REST API and fire the
`repository_dispatch` event. Use a **fine-grained** token scoped to just this
repo:

1. GitHub -> your profile photo -> **Settings**.
2. **Developer settings** (bottom of the left sidebar) -> **Personal access
   tokens** -> **Fine-grained tokens** -> **Generate new token**.
3. **Resource owner**: your account. **Repository access**: "Only select
   repositories" -> choose this repo.
4. Under **Permissions -> Repository permissions**, set:
   - **Contents**: Read and write (lets the token trigger the dispatch and
     covers the repo-level API surface Actions dispatch sits under)
   - **Actions**: Read and write (required to trigger/dispatch workflow runs)
5. Set an expiration you're comfortable with (fine-grained tokens can't be
   set to "no expiration"; note the date so you can rotate it).
6. **Generate token** and copy it immediately -- GitHub only shows it once.
   Store it in Power Automate as a secure input (e.g. an environment variable
   / secure connection), never hard-coded in plain text in the flow.

## 3. The Power Automate HTTP call

Add an **HTTP** action in your Power Automate flow (on whatever recurrence
schedule you want) with:

| Field | Value |
|---|---|
| Method | `POST` |
| URI | `https://api.github.com/repos/debnathAiincorg/Gold-Dashboard/dispatches` |
| Headers | see below |
| Body | see below |

**Headers:**

```
Accept: application/vnd.github+json
Authorization: Bearer YOUR_PERSONAL_ACCESS_TOKEN
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json
```

**Body (raw JSON):**

```json
{
  "event_type": "update-gold-rate"
}
```

A successful call returns **HTTP 204 No Content** with an empty body -- that
just means GitHub accepted the dispatch, not that the workflow finished (or
even started) yet. Check the **Actions** tab on GitHub to watch the run.

## 4. Push this repo to GitHub

If you haven't already:

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/debnathAiincorg/Gold-Dashboard.git
git push -u origin main
```

## 5. Test it

1. GitHub -> **Actions** tab -> **Update Gold Rate Data** workflow -> **Run
   workflow** (this is `workflow_dispatch`, no Power Automate needed for this
   test).
2. Watch the run. It should: check out the repo, install dependencies and
   Chromium, run `gold_rate.py`, and -- if `gold_rate_data.json` changed --
   commit and push it as the `gold-rate-bot` user.
3. Once it finishes, check the repo for a new commit updating
   `gold_rate_data.json`, and reload the Pages URL from step 1 -- it should
   show the same numbers within a minute or so (Pages rebuild time).

### If a run fails

The workflow only fails outright (red X, `::error::` annotation) when **all
three** sources fail in the same run -- gold_rate.py's own exit code only
goes non-zero in that case. If just one source breaks (e.g. a site changed
its markup), the run still succeeds but the workflow adds a `::warning::`
annotation listing which source(s) failed, and `gold_rate_data.json` simply
won't include that source for this update.

Note that GitHub Actions runners use shared datacenter IP ranges, which
sites' bot protection (Cloudflare, Akamai) sometimes blocks more
aggressively than a residential IP -- if a run fails that worked fine
locally, check the step log first for a 403/blocked response before
assuming a selector broke.
