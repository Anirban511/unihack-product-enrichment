# Deployment guide

Three things get deployed, in this order. Do them in order — each step needs a
URL or token from the one before it.

```
                       Space 1: unihack-api   (SDK: Docker)
   GitHub repo ─push─▶  FastAPI + Chromium
   (source of           exposes /v1/enrich, /docs
    truth)                        ▲
                                  │ HTTP (API_BASE_URL)
                       Space 2: unihack-ui    (SDK: Streamlit)
                        the front end, no scraping logic
```

**Why two Spaces:** a Space exposes exactly one port, so one process per Space.
The API needs the Docker SDK because it needs Chromium — the keyless search
backends answer plain HTTP with an anti-bot page, and many manufacturer pages
render their spec panel client-side, so without a real browser the pipeline
retrieves almost nothing. The UI is a plain Streamlit Space that just draws
whatever the API returns.

---

> **Usernames are pre-filled for `Anirban511`.** If your Hugging Face handle is
> different from your GitHub one, swap `Anirban511` for it in every
> `huggingface.co` / `.hf.space` URL below.

## 0. Before you start — three accounts and two tokens

| What | Where | Cost |
| --- | --- | --- |
| GitHub account | <https://github.com/signup> | free |
| Hugging Face account | <https://huggingface.co/join> | free |
| GitHub PAT (token) | created in step 1 | free |
| Hugging Face write token | created in step 3c | free |
| Groq API key | <https://console.groq.com/keys> | free tier |

> **Rotate your Groq key first.** The key currently in `.env` has been pasted
> into a chat log. Go to <https://console.groq.com/keys>, delete the old key,
> click **Create API Key**, and copy the new one. You will paste it into
> Hugging Face in step 3 — it never goes into git. `.env` is in `.gitignore`.

---

## 1. Create a GitHub Personal Access Token (PAT)

A PAT is a password that only works for git, that you can revoke. GitHub no
longer accepts your account password over HTTPS, so you need one.

1. Sign in to GitHub.
2. Click your **avatar** (top-right) → **Settings**.
3. Scroll the left sidebar to the bottom → **Developer settings**.
4. **Personal access tokens** → **Tokens (classic)** → **Generate new token**
   → **Generate new token (classic)**.
5. Fill it in:
   - **Note:** `unihack-laptop`
   - **Expiration:** 90 days
   - **Scopes:** tick **`repo`** only. Nothing else is needed.
6. **Generate token**.
7. **Copy it now** — `ghp_xxxxxxxxxxxxxxxxxxxx`. GitHub never shows it again.
   Paste it somewhere safe temporarily (Notepad is fine, delete it after).

> If it starts with `github_pat_` you used the *fine-grained* tab. That also
> works — give it **Repository access → All repositories** and
> **Repository permissions → Contents: Read and write**.

### Create the empty repository

1. Go to <https://github.com/new>.
2. **Repository name:** `unihack-product-enrichment`
3. **Public** (Streamlit Cloud's free tier can only deploy public repos).
4. **Do not** tick "Add a README", ".gitignore" or "license" — the repo must be
   empty or the first push will be rejected.
5. **Create repository**.

---

## 2. Push the code

Run these in the project folder. The remotes are already configured; paste your PAT
when git asks for a **password** (your username goes in the username prompt).

The repository is already initialised, committed, and pointed at both remotes,
so this is the only command you need:

```bash
cd "c:/Users/ANIRBAN/Downloads/UNIHACK"
git push -u origin main
```

**When prompted:**
- `Username for 'https://github.com':` → your GitHub username
- `Password for 'https://...':` → **paste the PAT**, not your account password
  (nothing appears on screen while pasting — that is normal, press Enter)

To avoid retyping it every push:

```bash
git config --global credential.helper manager
```

### Check nothing secret was committed

```bash
git ls-files | grep -E "^\.env$|secrets.toml$"
```

This must print **nothing**. If it prints `.env`, stop and run:

```bash
git rm --cached .env
git commit -m "Remove .env from tracking"
```

---

## 3. Deploy the API to Hugging Face Spaces

### 3a. Create the Space

1. Sign in at <https://huggingface.co>.
2. Click your **avatar** (top-right) → **New Space**.
   (Direct link: <https://huggingface.co/new-space>)
3. Fill it in:
   - **Owner:** your username
   - **Space name:** `unihack-api`
   - **License:** `mit`
   - **Select the Space SDK:** **Docker** → **Blank**
   - **Space hardware:** `CPU basic · 2 vCPU · 16 GB · FREE`
   - **Visibility:** **Public**
4. **Create Space**. You land on a page with git instructions — ignore them for
   a moment.

### 3b. Add your secrets

1. On the Space page → **Settings** (top-right tab).
2. Scroll to **Variables and secrets** → **New secret**.
3. Add this one:

   | Name | Value |
   | --- | --- |
   | `GROQ_API_KEY` | your **new** Groq key from step 0 |

4. Then **New variable** (not secret) for these two:

   | Name | Value |
   | --- | --- |
   | `ENABLE_SELENIUM` | `true` |
   | `GROQ_MODEL` | `openai/gpt-oss-120b` |

The Dockerfile already sets `CHROME_BINARY` and `CHROMEDRIVER_PATH`, so you do
not add those.

### 3c. Create a Hugging Face access token

1. Avatar → **Settings** → **Access Tokens**
   (direct: <https://huggingface.co/settings/tokens>).
2. **Create new token** → **Write** tab.
3. **Token name:** `unihack-push`, **Create token**, then **Copy**.
   It looks like `hf_xxxxxxxxxxxxxxxxxxxx`.

### 3d. Push the code to the Space

A Space *is* a git repo, so you add it as a second remote and push the same
commit you pushed to GitHub.

```bash
cd "c:/Users/ANIRBAN/Downloads/UNIHACK"

git remote add hf https://huggingface.co/spaces/Anirban511/unihack-api
git push hf main
```

When prompted:
- **Username:** your Hugging Face username
- **Password:** paste the **`hf_...` token**

> **If the push is rejected** with `fetch first`, the Space already has a README
> commit. Overwrite it — the repo you are pushing contains its own README with
> the correct Space metadata:
> ```bash
> git push hf main --force
> ```

### 3e. Watch it build

Go to your Space page. It shows **Building**. Click **Logs** → **Build** to
follow along. The first build takes **8–15 minutes** (it compiles Chromium
dependencies and a large Python stack). When the header turns **Running**:

- API docs: `https://Anirban511-unihack-api.hf.space/docs`
- Health:   `https://Anirban511-unihack-api.hf.space/v1/health`

Note the URL shape: **dashes, not slashes**, and it is `.hf.space`, not
`huggingface.co`.

Open `/v1/health` and check this line:

```json
"http_cache": { "browser_available": true }
```

If it says `false`, Chromium did not start — check **Logs → Container**.

---

## 4. Deploy the front end — second Hugging Face Space

The UI is a separate Space because a Space exposes only one port.

### 4a. Create it

1. <https://huggingface.co/new-space>
2. Fill it in:
   - **Space name:** `unihack-ui`
   - **License:** `mit`
   - **SDK:** **Streamlit**
   - **Space hardware:** `CPU basic · FREE`
   - **Visibility:** **Public**
3. **Create Space**.

### 4b. Point it at your API

**Settings → Variables and secrets → New variable** (a *variable*, not a
secret — it is only a URL):

| Name | Value |
| --- | --- |
| `API_BASE_URL` | `https://Anirban511-unihack-api.hf.space` |

Use the URL from step 3e. Dashes, not slashes. No trailing slash.

### 4c. Push the three UI files

The UI Space gets its own small repo — just the app, not the whole pipeline.
Run this from anywhere; it clones the empty Space, copies three files in, and
pushes. Use the **same `hf_...` token** as the password.

```bash
cd "c:/Users/ANIRBAN/Downloads/UNIHACK"

git clone https://huggingface.co/spaces/Anirban511/unihack-ui ../unihack-ui
cp deploy/hf-ui/README.md      ../unihack-ui/README.md
cp deploy/hf-ui/app.py         ../unihack-ui/app.py
cp deploy/hf-ui/requirements.txt ../unihack-ui/requirements.txt

cd ../unihack-ui
git add .
git commit -m "Streamlit front end for the enrichment API"
git push
```

If `git push` says the remote has commits you do not have:

```bash
git pull --rebase --allow-unrelated-histories
git push
```

Build takes 2–4 minutes. The UI is then at:

```
https://Anirban511-unihack-ui.hf.space
```

Open it, press **Check connection** in the sidebar — you want *"API reachable"*
and `Browser tier: ✅ available`. Then enrich `PDSH4816AF`.

> `deploy/hf-ui/app.py` is a copy of `streamlit_app.py`. After editing the UI,
> re-copy it before pushing: `cp streamlit_app.py deploy/hf-ui/app.py`.

---

## 5. Optional — Streamlit Community Cloud as a backup

Handy if a Space is slow to wake during the demo. It deploys from GitHub and
redeploys automatically on every push.

1. <https://share.streamlit.io> → **Sign in with GitHub** → authorise.
2. **Create app** → **Deploy a public app from GitHub**.
   - **Repository:** `Anirban511/unihack-product-enrichment`
   - **Branch:** `main`
   - **Main file path:** `streamlit_app.py`
3. **Advanced settings** *before* deploying:
   - **Python version:** `3.11`
   - **Secrets:**
     ```toml
     API_BASE_URL = "https://Anirban511-unihack-api.hf.space"
     ```
4. **Deploy**.

Both front ends talk to the same API, so they stay in sync automatically.

---

## 6. Updating after a code change

```bash
git add .
git commit -m "what changed"
git push origin main     # GitHub: source of truth
git push hf main         # API Space: rebuilds the Docker image (~8 min)
```

For a UI-only change:

```bash
cp streamlit_app.py deploy/hf-ui/app.py
git add . && git commit -m "UI tweak" && git push origin main
cp deploy/hf-ui/app.py ../unihack-ui/app.py
cd ../unihack-ui && git add . && git commit -m "UI tweak" && git push
```

Only the UI Space rebuilds — the API keeps running and keeps its warm cache.

---

## Sleep behaviour — read this before the demo

Neither free tier stays hot forever, and there is no free host that does.

| Platform | Sleeps after | Wake time |
| --- | --- | --- |
| HF Space, API (Docker, free CPU) | 48 h idle | ~30-60 s cold start |
| HF Space, UI (Streamlit, free CPU) | 48 h idle | ~20 s cold start |
| Streamlit Cloud (if you add it) | ~7 days idle | ~30 s, one click |

Neither *spins down mid-request* the way Render's free tier does — a sleeping
Space wakes on the first request and serves it. For a live demo:

- Open both URLs **10 minutes before** you present, and run one enrichment.
  That warms the container *and* the HTTP cache, so demo parts return fast.
- Warm the specific parts you plan to show. Cached fetches make a 90-second
  enrichment finish in about 2 seconds.
- If you want it never to sleep: HF Space **Settings → Sleep time → Never**
  (needs an upgraded Space, ~$0.03/h on CPU upgrade).

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Authentication failed` on `git push` | used account password | use the PAT as the password |
| `remote: Repository not found` | typo in username/repo, or PAT lacks `repo` scope | recheck both |
| `Updates were rejected` on `git push hf` | Space has its own initial commit | `git push hf main --force` |
| Space stuck on **Building** > 20 min | build genuinely is slow first time | check **Logs → Build** for a real error |
| `/v1/health` shows `browser_available: false` | Chromium failed to launch | **Logs → Container**; confirm the Dockerfile installed `chromium-driver` |
| UI says "Not reachable" | wrong `API_BASE_URL` | `https://user-space.hf.space` — dashes, `.hf.space`, no trailing slash |
| UI reachable but every request 504s | API Space still waking | open the API `/docs` once, wait for it, retry |
| Enrichment returns empty everything | Groq key missing or wrong | `/v1/health` → `llm.configured` must be `true` |
| Request times out in the UI | cold Space + uncached part | run it once, retry; the second call is cached |
| `ModuleNotFoundError` in the UI Space | missing dep | check `deploy/hf-ui/requirements.txt` was pushed |
| UI Space build fails on `streamlit_app.py` | wrong filename | the UI Space needs the file named **`app.py`** |

---

## Security checklist

- [ ] Old Groq key deleted at <https://console.groq.com/keys>
- [ ] New key stored **only** in the HF Space secret and your local `.env`
- [ ] `git ls-files | grep .env` prints nothing
- [ ] GitHub PAT deleted from wherever you temporarily pasted it
- [ ] PAT has `repo` scope only, with an expiry date

If a key ever does get committed: rotate it immediately. Deleting the file in a
later commit does **not** remove it from git history.
