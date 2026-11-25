// Data sources for the dashboard. Each provider returns a lanpulse.snapshot/1 object.
// Add a new one by adding an entry here: { label, help, fields: [...], load(cfg) }.
// See docs/snapshot-schema.md for the format.

async function getJSON(url, opts = {}) {
  const r = await fetch(url, { cache: "no-store", ...opts });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} from ${url}`);
  return r.json();
}

export const PROVIDERS = {
  local: {
    label: "This box (local)",
    help: "Reads data/snapshot.json from the same box that serves this page. Use this on the monitoring box or on the local network.",
    fields: [],
    load: () => getJSON("data/snapshot.json"),
  },

  url: {
    label: "Snapshot URL (cloud or another box)",
    help: "Any HTTPS address that returns a snapshot: a GitHub Gist raw URL, an S3/R2/GCS public object, a WebDAV/Nextcloud share, or another lanpulse box behind HTTPS (Tailscale, Cloudflare Tunnel). An HTTPS page can't read plain http:// addresses.",
    fields: [{ key: "url", label: "Snapshot URL", placeholder: "https://…/snapshot.json" },
             { key: "auth", label: "Authorization header (optional)", placeholder: "Bearer …", secret: true }],
    load: (cfg) => {
      if (!cfg.url) throw new Error("No snapshot URL set. Open settings.");
      const headers = cfg.auth ? { Authorization: cfg.auth } : {};
      // cache-bust for CDNs such as gist.githubusercontent.com
      const u = cfg.url + (cfg.url.includes("?") ? "&" : "?") + "_=" + Date.now();
      return getJSON(u, { headers });
    },
  },

  demo: {
    label: "Demo data",
    help: "Built-in sample data so you can try the dashboard without a box.",
    fields: [],
    load: async () => {
      const s = await getJSON("demo/snapshot.json");
      // keep the demo looking live: move every timestamp forward so "generated" is now
      const shift = Date.now() - Date.parse(s.generated);
      const move = (v) => new Date(Date.parse(v.replace(" ", "T") + (/[Z+]/.test(v.slice(10)) ? "" : "Z")) + shift).toISOString();
      const walk = (o) => {
        if (Array.isArray(o)) return o.forEach(walk);
        if (!o || typeof o !== "object") return;
        for (const [k, v] of Object.entries(o)) {
          if (typeof v === "string" && ["generated", "since", "at", "first_seen", "last_seen"].includes(k)) o[k] = move(v);
          else walk(v);
        }
      };
      walk(s);
      return s;
    },
  },
};

// Roadmap (contributions welcome): Google Drive (OAuth, like household-style apps),
// Supabase/Firebase realtime, MQTT over WebSockets, Home Assistant.
