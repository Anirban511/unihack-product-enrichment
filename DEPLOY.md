# Deployment guide

Three things get deployed, in this order. Do them in order — each step needs a
URL or token from the one before it.

```
   GitHub repo ─push─▶  ONE Hugging Face Space  (SDK: Gradio, free)
   (source of            space_app.py
    truth)                 ├── /          Gradio UI
                           ├── /docs      OpenAPI / Swagger
                           └── /v1/...    the REST API
                          + Chromium, installed from packages.txt
```

**Why one Space, and why Gradio.** The free tier does not offer the Docker SDK,
but a Gradio Space runs an arbitrary Python file *and* installs Debian packages
from `packages.txt` — which is all we need to get Chromium. `space_app.py`
mounts the Gradio UI **into** the FastAPI app rather than the other way round,
so the REST API keeps its own routes and status codes and the UI is just
another mount point. One URL, one container, one free Space.

Chromium is not optional: the keyless search backends answer plain HTTP with an
anti-bot page, and many manufacturer pages render their spec panel client-side,
so without a real browser the pipeline retrieves almost nothing.

---

> **URLs are pre-filled.** GitHub is `Anirban511`; Hugging Face is
> `Excalibur51`. The two handles differ, so copy the commands as written rather
> than substituting one for the other.

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

## 3. Deploy to Hugging Face Spaces

### 3a. Create the Space

1. Sign in at <https://huggingface.co>.
2. Go to <https://huggingface.co/new-space>.
3. Fill it in:
   - **Owner:** `Excalibur51`
   - **Space name:** `unihack-api`
   - **License:** `mit`
   - **Select the Space SDK:** **Gradio** → template **Blank**
   - **Space hardware:** `CPU basic · 2 vCPU · 16 GB · FREE`
   - **Visibility:** **Public**
4. **Create Space**. Ignore the git instructions it shows you.

> **Pick CPU, not ZeroGPU.** This pipeline never touches a GPU — it fetches
> pages, drives a headless browser and calls a *remote* LLM. A ZeroGPU Space
> refuses to start without a `@spaces.GPU` entry point; `space_app.py` registers
> a dummy one so it will boot anyway, but CPU basic is the correct hardware and
> avoids ZeroGPU's queueing entirely.

> **Docker is not needed.** If the Docker SDK is greyed out or asks for
> billing, that is fine — Gradio does everything here, including Chromium via
> `packages.txt`.

### 3b. Add your secret

1. On the Space page → **Settings** (top tab).
2. **Variables and secrets** → **New secret**:

   | Name | Value |
   | --- | --- |
   | `GROQ_API_KEY` | your **new** Groq key from step 0 |

3. Optionally **New variable** (plain, not secret):

   | Name | Value |
   | --- | --- |
   | `GROQ_MODEL` | `openai/gpt-oss-120b` |

That is the only required setting. Chromium's paths are auto-detected at
runtime, so there is nothing else to configure.

### 3c. Create a Hugging Face access token

1. Avatar → **Settings** → **Access Tokens**
   (direct: <https://huggingface.co/settings/tokens>).
2. **Create new token** → **Write** tab.
3. **Token name:** `unihack-push`, **Create token**, then **Copy**.
   It looks like `hf_xxxxxxxxxxxxxxxxxxxx`.

### 3d. Push the code to the Space

A Space *is* a git repo. The `hf` remote is already configured locally, so:

```bash
cd "c:/Users/ANIRBAN/Downloads/UNIHACK"
git push hf main --force
```

`--force` is deliberate: the Space was created with its own README commit, and
our README carries the correct Space metadata.

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
follow along. The first build takes **6–12 minutes** (apt installs Chromium,
pip installs a large Python stack). When the header turns **Running**:

- UI:       `https://Excalibur51-unihack-api.hf.space/`
- API docs: `https://Excalibur51-unihack-api.hf.space/api/docs`
- Health:   `https://Excalibur51-unihack-api.hf.space/api/v1/health`

(On a Space the REST API is mounted under `/api`, because the platform's own
runtime owns the root port. Running locally it sits at the root: `/docs`,
`/v1/...`.)

Note the URL shape: **dashes, not slashes**, and it is `.hf.space`, not
`huggingface.co`.

Open `/v1/health` and check this line:

```json
"http_cache": { "browser_available": true }
```

If it says `false`, Chromium did not start — check **Logs → Container**.

---

## 4. The front end — already deployed

There is no second step. `space_app.py` mounts the Gradio UI into the same
FastAPI application, so the moment the Space says **Running** the UI is live at
the Space root:

```
https://Excalibur51-unihack-api.hf.space/
```

Open it and enrich `PDSH4816AF`. The tabs are:

| Tab | What it does |
| --- | --- |
| **Enrich a part** | one part -> the delivery row, the five descriptions, the attributes, and a citation for every value |
| **Batch / CSV** | upload the input CSV, download the 252-column delivery CSV |
| **Reference & policy** | which reference tables loaded, and the sourcing rules |

The **"What the pipeline REFUSED to say"** panel is worth showing in a demo: it
lists values that were produced and then discarded for lack of a source.

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
     API_BASE_URL = "https://Excalibur51-unihack-api.hf.space"
     ```
4. **Deploy**.

Both front ends talk to the same API, so they stay in sync automatically.

---

## 6. Updating after a code change

```bash
git add .
git commit -m "what changed"
git push origin main     # GitHub: source of truth
git push hf main         # Space: rebuilds and restarts (~3 min after the first build)
```

Both the UI and the API ship in that one push — they cannot drift apart.


---

## Sleep behaviour — read this before the demo

Neither free tier stays hot forever, and there is no free host that does.

| Platform | Sleeps after | Wake time |
| --- | --- | --- |
| HF Space (Gradio, free CPU) | 48 h idle | ~30-60 s cold start |
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
| `RUNTIME_ERROR: No @spaces.GPU function detected` | Space is on ZeroGPU hardware | prefer **CPU basic**; `space_app.py` also registers a dummy GPU entry point so ZeroGPU will start |
| `[Errno 98] address already in use ... 7860` | the Space runtime already owns 7860; a second server cannot bind it | handled: on a Space the app uses `demo.launch()` and mounts the REST API at `/api` |
| Build fails on `chromium` | `packages.txt` not picked up | confirm it is at the repo root and was pushed |
| `push rejected ... contains binary files` | Hugging Face wants binaries in Xet/LFS, not plain git | keep binaries out of the repo (`*.pdf` is gitignored) or `git lfs track` them |
| UI loads but `/docs` 404s | Gradio started standalone instead of mounted | check **Logs → Container** for `Uvicorn running on` |
| `/v1/health` shows `browser_available: false` | Chromium failed to launch | **Logs → Container**; confirm the Dockerfile installed `chromium-driver` |
| Streamlit Cloud UI says "Not reachable" | wrong `API_BASE_URL` | `https://user-space.hf.space` — dashes, `.hf.space`, no trailing slash |
| First request after idle times out | Space still waking | reload, wait ~40 s, retry |
| Enrichment returns empty everything | Groq key missing or wrong | `/v1/health` → `llm.configured` must be `true` |
| Request times out in the UI | cold Space + uncached part | run it once, retry; the second call is cached |
| `ModuleNotFoundError` in the Space | missing dep | add it to `requirements.txt`, push again |

---

## Security checklist

- [ ] Old Groq key deleted at <https://console.groq.com/keys>
- [ ] New key stored **only** in the HF Space secret and your local `.env`
- [ ] `git ls-files | grep .env` prints nothing
- [ ] GitHub PAT deleted from wherever you temporarily pasted it
- [ ] PAT has `repo` scope only, with an expiry date

If a key ever does get committed: rotate it immediately. Deleting the file in a
later commit does **not** remove it from git history.
