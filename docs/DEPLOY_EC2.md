# Deploying DBCompare on EC2

The app is one container. The databases are external (RDS), so there is nothing to
orchestrate: build the image, mount `.env`, open one port to the people who should reach
it.

## 1. The instance

| | |
|---|---|
| AMI | Amazon Linux 2023, or Ubuntu 22.04+ |
| Type | `t3.small` is enough (2 GB RAM; the row diff holds both sides in memory, and compose caps the container at 2 GB) |
| Disk | 20 GB - the image is Alpine based, roughly 350-450 MB with the ODBC driver and pandas/pyarrow |
| Outbound | 1433 to the SQL Server host, 443 to `api.openai.com` |

**Security group, inbound.** This is the decision that matters, because CORS is open
(see below). Allow **8501 from your office or VPN CIDR only** - not `0.0.0.0/0` - and 22
from the same range for SSH.

The instance also needs to reach the databases: if RDS is in the same VPC, add the
instance's security group to the RDS security group on 1433.

The image is built on `python:3.12-alpine`. Every compiled dependency (pyodbc, pandas,
pyarrow, numpy, uv) ships a musllinux wheel, so nothing is compiled during the build, and
Microsoft publishes a signed Alpine `.apk` for ODBC Driver 18 which the Dockerfile
verifies against their GPG key before installing.

On a Graviton instance, set `DOCKER_PLATFORM=linux/arm64` in `.env`; Microsoft publishes
an arm64 driver and the Dockerfile picks it by `TARGETARCH`.

## 2. Docker

```bash
# Amazon Linux 2023
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user      # log out and back in

# Ubuntu
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
```

Compose v2 ships with Docker; `docker compose version` should print 2.24 or later (the
`env_file: required: false` syntax needs it).

## 3. The application

```bash
git clone <your remote> dbcompare && cd dbcompare
cp .env.example .env
vi .env            # server, login, database names, OPENAI_API_KEY
mkdir -p logs

docker compose up -d --build
docker compose logs -f          # first build takes a few minutes
```

Open `http://<instance-ip>:8501`.

Check the connection and the key without opening a browser:

```bash
docker compose exec dbcompare python check_setup.py
```

It prints both databases, their table counts, the ODBC driver in use, and the last six
characters of the API key - enough to confirm *which* key is live.

## 4. Where the OpenAI key comes from

In order, each beating the one below it:

1. **The Streamlit sidebar**, for the current session only.
2. **`.env`**, mounted at `/app/.env`. `config.py` loads it with `override=True`, so a
   value here wins even if the container environment already had one.
3. **The host's environment**, passed through by `docker-compose.yml`
   (`OPENAI_API_KEY: ${OPENAI_API_KEY:-}`) for anything `.env` does not set.

So the default is `.env`, with the system environment as the fallback:

```bash
# .env has the key -> that one is used
docker compose up -d

# .env has no OPENAI_API_KEY -> the host's is used
export OPENAI_API_KEY=sk-...
docker compose up -d
```

`.env` is in `.dockerignore`, so it is never baked into the image - only mounted, and
read-only. A rebuilt or pushed image carries no credentials.

For anything longer-lived than a trial, keep the key in AWS Secrets Manager or SSM
Parameter Store and write `.env` at boot, rather than committing it to the instance.

## 5. CORS

`server.enableCORS = false` in `.streamlit/config.toml`: **every origin is allowed.**
Streamlit answers with `Access-Control-Allow-Origin: *` and accepts a WebSocket
connection from any origin.

Be clear about the trade: any page a user visits while logged in can open a WebSocket to
this app and drive it as that user - run comparisons, read your schema, read the report.
`enableXsrfProtection` is still on, but it covers form posts and uploads, not the
WebSocket. **The security group is therefore the only thing protecting the app.** Keep
8501 closed to the internet.

To lock it down instead, in `.env`:

```env
STREAMLIT_ENABLE_CORS=true
CORS_ORIGINS=https://dbcompare.internal.example.com
```

and `docker compose up -d` again. No rebuild: both are environment overrides of the
config file.

## 6. Behind a reverse proxy (recommended for anything shared)

Run the app on loopback and let nginx or an ALB terminate TLS and authenticate:

```env
BIND_ADDRESS=127.0.0.1
```

nginx needs the WebSocket upgrade headers, or the app loads and then hangs:

```nginx
location / {
    proxy_pass http://127.0.0.1:8501;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 300s;   # a comparison can run for minutes
}
```

`proxy_read_timeout` matters: a full run against a large schema takes longer than the
60-second default, and the browser would lose the socket mid-comparison.

## 7. Operating it

```bash
docker compose ps                     # health comes from /_stcore/health
docker compose logs -f --tail=100
tail -f logs/dbcompare.log            # the app's own log, survives the container
docker compose up -d --build          # deploy a change
docker compose down                   # stop
```

Logs are capped at 3 x 10 MB by the json-file driver, and `logs/dbcompare.log` rotates at
2 MB x 3, so neither fills the disk.

## 8. Things that actually go wrong

| Symptom | Cause |
|---|---|
| `Data source name not found` | `MSSQL_DRIVER` copied from a Windows `.env`. The compose file pins `ODBC Driver 18 for SQL Server`; do not override it. |
| `SSL Provider: certificate verify failed` | Driver 18 encrypts by default. Keep `MSSQL_TRUST_CERT=yes` for RDS' own certificate, or install the RDS CA bundle. |
| Login timeout | Security group. The instance must be allowed into RDS on 1433. |
| App loads, stays "connecting" | A proxy without the WebSocket upgrade headers (section 6). |
| Wrong API key in use | `check_setup.py` prints the last six characters. Remember `.env` beats the host environment. |
