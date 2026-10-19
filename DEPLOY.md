# Deploying InvoiceHub to Render

Two steps: get the code onto GitHub, point Render at it. The repo already
contains a `render.yaml`, so Render configures itself.

---

## 1 · Push to GitHub

The folder is already a git repository with one commit. You only need a remote.

Create an **empty** repository on GitHub — no README, no `.gitignore`, no
licence, or the first push will be rejected as a non-fast-forward. Make it
**private**: this repo describes how your AP mailbox is routed and which
accounts your vendors are paid through, even though it contains no credentials.

Then, from inside the `invoice-hub` folder:

```bash
git remote add origin https://github.com/YOUR-USERNAME/invoice-hub.git
git push -u origin main
```

If GitHub asks for a password, it wants a **personal access token**, not your
account password — Settings → Developer settings → Personal access tokens →
Fine-grained → Repository access: only this repo → Permissions: Contents
read/write. Paste the token at the password prompt.

Using the GitHub CLI instead, from the same folder:

```bash
gh repo create invoice-hub --private --source=. --remote=origin --push
```

### Before you push, confirm nothing secret is going up

```bash
git ls-files | grep -iE '\.env$|fernet|\.db$|\.key$|\.pem$'
```

That must print **nothing**. `.gitignore` already excludes `.env`,
`.fernet_key`, the database and `storage/`, but it costs a second to check,
and a secret in git history is not removed by deleting it in a later commit.

---

## 2 · Deploy on Render

Render dashboard → **New** → **Blueprint** → select the repository. It reads
`render.yaml` and proposes the service. You will be prompted for the env vars
marked `sync: false`.

### Set this one before the first deploy

**`INVOICEHUB_FERNET_KEY`** — encrypts the API credentials you enter later.
Generate it locally:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Paste the output into Render and **keep a copy somewhere else**. If you lose
both it and the disk, every stored credential becomes permanently unreadable
and has to be re-entered.

Do not let Render generate this one. Render's generator makes a random string;
Fernet needs exactly 32 url-safe base64 bytes, and a generated value will fail
to decrypt anything you store.

The other `sync: false` vars can stay blank — enter those credentials in the
portal's Settings page instead, which is the intended path.

### After the first deploy

1. Open the service URL and sign in with
   `admin@invoicehub.local` / `changeme`, then **change the password
   immediately**. The portal nags until you do.

2. If you are connecting QuickBooks, set `QBO_REDIRECT_URI` to
   `https://YOUR-SERVICE.onrender.com/api/qbo/callback` and add exactly that
   URI to your Intuit app. OAuth rejects a mismatch with an unhelpful error.

3. Check `https://YOUR-SERVICE.onrender.com/healthz`. It should return
   `"storage_writable": true`. If it returns 503 with
   `"storage_writable": false`, the disk did not mount — stop and fix that
   before entering any data, because nothing you enter will survive.

4. The app starts in demo mode. Turn it off in Settings when your credentials
   are in and you want live API calls.

---

## Three things that will bite you

**The disk is not optional.** Render's filesystem is ephemeral. The blueprint
mounts a 5 GB disk at `/data` and points the database and document storage at
it. Without that disk every vendor, invoice and reconciliation is destroyed on
each deploy and each restart, with no error — it simply comes back empty.
Disks require a paid instance type; the free tier has none.

**The free tier sleeps, so agents will not run.** Render spins free services
down after inactivity, and the scheduler runs inside the web process. A
sleeping service runs no agents: no mailbox sweep, no month-end statement
requests. If you only want the portal and will press the buttons yourself,
free is fine. If you want the automation, it needs a plan that stays warm.

**Do not scale past one instance.** APScheduler is in-process. Two instances
means every scheduled agent fires twice — two statement requests to the same
vendor, two reconciliation runs. The blueprint does not autoscale; leave it
that way unless you move the agents to a separate worker.

---

## Updating it later

```bash
git add -A
git commit -m "what changed"
git push
```

`autoDeploy: true` is set, so Render rebuilds on every push to `main`.

Run the tests first — they take a few seconds and use a throwaway database:

```bash
python tests/smoke.py
```

---

## If the build fails

**Health check timeout / "no open ports detected"** — the container must bind
`$PORT`. The `Dockerfile` uses shell-form `CMD` precisely so `${PORT}` expands;
if you rewrite it as JSON exec form, the variable will not expand and the
deploy will fail this way.

**`cryptography` fails to build** — the Dockerfile installs `gcc` and
`libffi-dev` for exactly this. If you switch to Render's native Python runtime
instead of Docker, you may need to pin a `cryptography` wheel that matches
their Python version.

**Everything resets after a deploy** — the disk is not mounted, or
`INVOICEHUB_DB` and `INVOICEHUB_STORAGE` are not pointing at `/data`. Check
`/healthz`.

**Credentials stopped decrypting** — `INVOICEHUB_FERNET_KEY` changed, or was
never set and the generated key file went away with the disk. Re-enter the
credentials in Settings and set the env var properly this time.
