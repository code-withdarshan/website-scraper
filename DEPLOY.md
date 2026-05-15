# Deploying to Streamlit Community Cloud

The app is ready to host on **share.streamlit.io** (free, no credit card).

## Files Streamlit Cloud needs

These already exist in this folder:

| File                       | Purpose                                       |
| -------------------------- | --------------------------------------------- |
| `streamlit_app.py`         | The Streamlit UI (entry point)                |
| `scraper.py`               | Shared scraper module (used by Streamlit app) |
| `requirements.txt`         | Python deps installed automatically by Cloud  |
| `.streamlit/config.toml`   | Theme + upload size + headless mode           |

Files that are **not** used by Streamlit Cloud (they're for the Flask version): `app.py`, `templates/`, `static/`, `Dockerfile`, `docker-compose.yml`, `start.bat`. You can leave them in the repo — Streamlit Cloud just ignores them.

## Step-by-step deployment

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
gh repo create email-scraper --public --source=. --push
```

(Or create a repo manually on github.com and `git push` to it.)

### 2. Connect Streamlit Cloud

1. Go to **https://share.streamlit.io**
2. Sign in with GitHub
3. Click **New app**
4. Select your repo
5. **Main file path:** `streamlit_app.py`
6. **Branch:** `main`
7. Click **Deploy**

In about 60 seconds, your app is live at a URL like:

```
https://<your-username>-email-scraper-streamlit-app-<hash>.streamlit.app
```

### 3. Custom domain (optional)

Free tier only gives the `*.streamlit.app` URL. For a custom domain, you'd need to host elsewhere (Render, Railway, Fly.io, or a VPS) using the Flask version + waitress or the Dockerfile.

## Resource limits on Streamlit Cloud (free tier)

| Limit              | Value                            | Impact on this app                                                               |
| ------------------ | -------------------------------- | -------------------------------------------------------------------------------- |
| Memory             | 1 GB                             | Fine for 500-URL batches                                                          |
| CPU                | Shared                           | Slower for large batches; consider lowering `MAX_WORKERS` in `streamlit_app.py`  |
| Disk               | Ephemeral                        | History is held in `st.session_state`, lost on app restart (per-tab)             |
| Wall-clock         | App sleeps after 7 days idle     | First request after wake-up takes ~30 s                                          |
| Upload size        | Capped by `maxUploadSize` config | Currently 10 MB                                                                  |
| Public visibility  | Anyone with the URL can use it   | Add password gating below if needed                                              |

## Adding a password (optional)

If you want to lock the public Streamlit URL behind a password, add to the top of `streamlit_app.py`:

```python
import hmac

def _check_password():
    def password_entered():
        if hmac.compare_digest(st.session_state["pwd"], st.secrets["password"]):
            st.session_state["auth"] = True
            del st.session_state["pwd"]
        else:
            st.session_state["auth"] = False

    if st.session_state.get("auth"):
        return True
    st.text_input("Password", type="password", on_change=password_entered, key="pwd")
    if st.session_state.get("auth") is False:
        st.error("Wrong password")
    return False

if not _check_password():
    st.stop()
```

Then in **App settings → Secrets** on Streamlit Cloud, add:

```toml
password = "your-secret-here"
```

## Running locally

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Or just double-click `start-streamlit.bat`.

The app will open at **http://localhost:8501**.

## Flask vs Streamlit — which to use?

| Need                            | Use         |
| ------------------------------- | ----------- |
| Free public hosting             | Streamlit   |
| Custom HTML/JS, fine-tuned UX   | Flask       |
| Real-time live ticker per URL   | Flask (SSE) |
| Quick share with a teammate     | Streamlit   |
| Production at scale             | Flask       |
| Embed in another web page       | Flask       |

Both versions read from the same `scraper.py`, so improvements you make there benefit both.
