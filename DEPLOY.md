# Deployment — examples, not recommendations

> ## ⚠️ Use at your own risk
>
> Anything you do to make this app reachable beyond your own machine is
> **your responsibility**. The notes below are *examples* of what has
> worked, not endorsements.
>
> Before exposing the app on the internet:
>
> - **De-identify your data.** Patient identifiers, slide labels, anything
>   baked into pixels or filenames.
> - **Make sure you're allowed to do this** — IRB, data-use agreements,
>   institutional policies.
> - **The app's password is a shared deterrent, not real authentication.**
>   Treat anyone with the URL + the code as a trusted participant.
> - **Cloudflare quick tunnels have no SLA**, no uptime guarantee, and the
>   URL rotates each restart. For anything serious, use a named tunnel +
>   your own domain, a reverse proxy with real auth, or proper hosting.

The app binds `0.0.0.0` and honours `$PORT`, so on a LAN it's already
reachable at `http://<your-ip>:<port>`.

---

## A — Cloudflare Tunnel (the example most people pick)

Runs on a machine you control; results stay on your own disk.

### Quick tunnel (no account; URL changes each restart)

```bash
# one-time: download the binary (no sudo needed)
mkdir -p ~/.local/bin
curl -fL -o ~/.local/bin/cloudflared \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x ~/.local/bin/cloudflared

# every session:
python app.py &
~/.local/bin/cloudflared tunnel --url http://localhost:8000
```

The tunnel prints `https://<random>.trycloudflare.com`. **If `cloudflared`
restarts (process dies, machine reboots) the URL changes** and you must
resend it. Good for a demo, awkward for a multi-day study.

### Named tunnel (stable URL across restarts — needs a free CF account + domain)

```bash
cloudflared tunnel login
cloudflared tunnel create my-review
cloudflared tunnel route dns my-review review.<your-domain>
python app.py &
cloudflared tunnel --url http://localhost:8000 run my-review
```

The URL stays `https://review.<your-domain>` forever.

---

## B — Hugging Face Space (or any Docker host)

If you can't keep a machine on, host the Dockerfile here on
[Hugging Face Spaces](https://huggingface.co/spaces) (free, Docker SDK),
Render, Railway, Fly.io, etc.

```bash
git init && git add . && git commit -m "scoring app"
git remote add space https://huggingface.co/spaces/<you>/<space>
# you'll need to also push the candidate images and the manifest;
# adjust .gitignore for that push (manifest.json + the sampled patches).
git push space main
```

⚠️ **Result persistence.** A Space's filesystem is wiped on rebuild,
which means `results/*.csv` written there can be lost. Mitigations:
- Attach paid persistent storage; or
- Have the app periodically push CSVs to a private dataset repo (out of
  scope for this README); or
- Just download from `/admin?key=...` often.

The Cloudflare-tunnel approach (A) avoids this because results live on
your own disk.

---

## Notes

- Keep **one server process** (single worker; `Dockerfile` does this) so
  the in-memory results store stays consistent. Writes also go to disk
  on every click, so a crash loses at most one answer.
- For pure LAN use, no tunnel needed — share `http://<your-host>:8000`.
