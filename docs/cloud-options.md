# Cloud options

By default lanpulse is **local only**: the snapshot is written to the box and served at `http://<box>:8080`. To see status away from the site, choose one of these.

| Option | Cost | Setup | Privacy | Freshness |
|---|---|---|---|---|
| **A. Private tunnel to the box** (Tailscale, WireGuard) | free | install on box + phone | private | live |
| **B. GitHub Gist publisher** | free | token + gist | unlisted (anyone with the URL can read) | ~1-5 min (CDN cache) |
| **C. HTTP publisher** (S3/R2/GCS, WebDAV/Nextcloud, own server) | free to cheap | bucket or share | as private as the endpoint | live |
| **D. Public HTTPS tunnel** (Cloudflare Tunnel) | free | domain on Cloudflare | add Cloudflare Access to keep it private | live |

A GitHub Pages copy of the dashboard can read B, C or D. An `https://` page can't read a plain `http://` box (browsers block mixed content). For A, use the box's own dashboard over the tunnel.

## A. Private tunnel (simplest, nothing published)

Install [Tailscale](https://tailscale.com) on the box (`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`) and on your phone or laptop. Then open `http://<box-tailscale-name>:8080` from anywhere.
For an HTTPS address that GitHub Pages can read, run `sudo tailscale serve --bg 8080`.

## B. GitHub Gist

1. Create a secret gist containing a file named `snapshot.json` (any content), and note its ID.
2. Create a fine-grained or classic token with only the **gist** scope.
3. On the box, edit `/opt/monitor/lanpulse.env`:
   ```
   LANPULSE_PUBLISHERS=gist
   GIST_ID=<id>
   GIST_TOKEN=<token>
   ```
4. Restart the collector: `cd /opt/monitor && sudo docker compose restart lanpulse-collector`
5. In the dashboard (GitHub Pages or anywhere), choose ⚙ → Snapshot URL:
   `https://gist.githubusercontent.com/<user>/<id>/raw/snapshot.json`

Gists are rate-limited (5,000 API calls/hour). The default 30-second interval uses 120/hour. Raising `LANPULSE_INTERVAL=60` is kinder.

## C. HTTP publisher

`LANPULSE_PUBLISHERS=http` sends the snapshot to `PUBLISH_HTTP_URL` on every cycle:

- **Nextcloud/WebDAV:** `PUBLISH_HTTP_URL=https://cloud.example.com/remote.php/dav/files/<user>/lanpulse/snapshot.json`, `PUBLISH_HTTP_AUTH=Basic <base64 user:app-password>`
- **S3 / Cloudflare R2 / GCS:** a long-lived presigned PUT URL, or a small proxy that signs. The bucket object needs public read, or a CORS rule allowing your dashboard's origin.
- **Webhook / your own API:** `PUBLISH_HTTP_METHOD=POST`

The dashboard reads it back with ⚙ → Snapshot URL. Add an Authorization header there if the endpoint needs one. It's stored only in that browser.

## D. Cloudflare Tunnel

`cloudflared tunnel --url http://localhost:8080` gives an HTTPS address for the box's dashboard and snapshot (`/data/snapshot.json` already sends `Access-Control-Allow-Origin: *`). Put Cloudflare Access in front if the status shouldn't be public.

## Planned

A Google Drive provider and publisher (OAuth, data in your own Drive), Supabase/Firebase realtime, MQTT. See `web/providers.js` and `collector/collector.py` for where they plug in.
