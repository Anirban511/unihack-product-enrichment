# Deployment guide

Three things get deployed, in this order. Do them in order — each step needs a
URL or token from the one before it.

```
   GitHub repo  ──push──▶  Hugging Face Space (Docker)   = the API + Chromium
        │                            │
        │                            ▼
        └────────────▶  Streamlit Community Cloud        = the front end
                        (reads API_BASE_URL from secrets)
```

**Why split it:** Streamlit Community Cloud runs one Streamlit process and
cannot expose REST endpoints, and it has no Chromium. Hugging Face Spaces with
the Docker SDK can do both, so the scraping stack lives there and the UI just
draws what the API returns.

---

## 0. Before you start — three accounts and two tokens

| What | Where | Cost |
| --- | --- | --- |
| GitHub account | <https://github.com/signup> | free |
| Hugging Face account | <https://huggingface.co/join> | free |
| Streamlit Cloud account | <https://share.streamlit.io> — sign in *with GitHub* | free |
| GitHub PAT (token) | created in step 1 | free |
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

Run these in the project folder. Replace `YOUR_GH_USERNAME` and paste your PAT
when git asks for a **password** (your username goes in the username prompt).

```bash
cd "c:/Users/ANIRBAN/Downloads/UNIHACK"

git init
git branch -M main
git add .
git commit -m "Unilog product enrichment pipeline: API, UI, deployment"

git remote add origin https://github.com/YOUR_GH_USERNAME/unihack-product-enrichment.git
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

git remote add hf https://huggingface.co/spaces/YOUR_HF_USERNAME/unihack-api
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

- API docs: `https://YOUR_HF_USERNAME-unihack-api.hf.space/docs`
- Health:   `https://YOUR_HF_USERNAME-unihack-api.hf.space/v1/health`

Note the URL shape: **dashes, not slashes**, and it is `.hf.space`, not
`huggingface.co`.

Open `/v1/health` and check this line:

```json
"http_cache": { "browser_available": true }
```

If it says `false`, Chromium did not start — check **Logs → Container**.

---

## 4. Deploy the front end to Streamlit Community Cloud

1. Go to <https://share.streamlit.io> and **Sign in with GitHub**. Authorise it.
2. **Create app** → **Deploy a public app from GitHub**.
3. Fill it in:
   - **Repository:** `YOUR_GH_USERNAME/unihack-product-enrichment`
   - **Branch:** `main`
   - **Main file path:** `streamlit_app.py`
   - **App URL:** pick anything, e.g. `unihack-enrichment`
4. Click **Advanced settings** *before* deploying:
   - **Python version:** `3.11`
   - **Secrets:** paste exactly this, with your own Space URL:

     ```toml
     API_BASE_URL = "https://YOUR_HF_USERNAME-unihack-api.hf.space"
     ```

5. **Deploy**. First build takes 3–6 minutes.

When it loads, press **Check connection** in the sidebar. You should see
*"API reachable"* and the model name. Then enrich `PDSH4816AF`.

---

## 5. Optional — host the UI on Hugging Face too

Useful as a backup, or if you would rather keep everything on one platform.

1. <https://huggingface.co/new-space> → name `unihack-ui` → SDK **Streamlit** →
   CPU basic → Public → **Create Space**.
2. **Settings → Variables and secrets → New variable**:

   | Name | Value |
   | --- | --- |
   | `API_BASE_URL` | `https://YOUR_HF_USERNAME-unihack-api.hf.space` |

3. Push the three files in `deploy/hf-ui/`:

```bash
cd "c:/Users/ANIRBAN/Downloads/UNIHACK"
git clone https://huggingface.co/spaces/YOUR_HF_USERNAME/unihack-ui /tmp/unihack-ui
cp deploy/hf-ui/README.md deploy/hf-ui/app.py deploy/hf-ui/requirements.txt /tmp/unihack-ui/
cd /tmp/unihack-ui
git add .
git commit -m "Streamlit front end"
git push
```

UI lives at `https://YOUR_HF_USERNAME-unihack-ui.hf.space`.

> `deploy/hf-ui/app.py` is a copy of `streamlit_app.py`. After changing the UI,
> re-copy it: `cp streamlit_app.py deploy/hf-ui/app.py`.

---

## 6. Updating after a code change

```bash
git add .
git commit -m "what changed"
git push origin main     # GitHub  → Streamlit Cloud redeploys automatically
git push hf main         # HF Space → rebuilds the Docker image
```

Streamlit Cloud watches the repo and redeploys on push. The HF Space only
rebuilds when you push to it.

---

## Sleep behaviour — read this before the demo

Neither free tier stays hot forever, and there is no free host that does.

| Platform | Sleeps after | Wake time |
| --- | --- | --- |
| HF Space (free CPU) | 48 h idle | ~30 s cold start |
| Streamlit Cloud | ~7 days idle | ~30 s, one click |

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
| Streamlit: "Not reachable" | wrong `API_BASE_URL` | it is `https://user-space.hf.space` — dashes, `.hf.space` |
| Enrichment returns empty everything | Groq key missing or wrong | `/v1/health` → `llm.configured` must be `true` |
| Request times out in the UI | cold Space + uncached part | run it once, retry; the second call is cached |
| `ModuleNotFoundError` on Streamlit Cloud | Python version | Advanced settings → Python 3.11 → **Reboot app** |

---

## Security checklist

- [ ] Old Groq key deleted at <https://console.groq.com/keys>
- [ ] New key stored **only** in the HF Space secret and your local `.env`
- [ ] `git ls-files | grep .env` prints nothing
- [ ] GitHub PAT deleted from wherever you temporarily pasted it
- [ ] PAT has `repo` scope only, with an expiry date

If a key ever does get committed: rotate it immediately. Deleting the file in a
later commit does **not** remove it from git history.
