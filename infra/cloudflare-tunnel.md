# Cloudflare tunnel — adding shillscore.tg-itsavibe.com

The shared tunnel config lives at `~/.cloudflared/config.yml`. Add **one** ingress entry above the catch-all:

```yaml
  # shillscore — crypto-Twitter signal accuracy tracker
  - hostname: shillscore.tg-itsavibe.com
    service: http://localhost:3006
```

Then:

```bash
cloudflared tunnel route dns 736eda62-c751-4cb5-aefe-c3bfe7ba167a shillscore.tg-itsavibe.com
sudo systemctl restart cloudflared
```

Port `3006` is what `infra/docker-compose.yml` binds the `web` container to (loopback only). The API is **not** publicly exposed — Next.js reaches it via the internal docker network as `http://api:8000`.

If you ever need the API publicly (e.g. for the Telegram bot to hit it from outside), add a second ingress on `api.shillscore.tg-itsavibe.com` and bind `api` to a loopback port in compose.
