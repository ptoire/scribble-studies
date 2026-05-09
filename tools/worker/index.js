const GH_API = 'https://api.github.com/repos/ptoire/kellogg-data/contents/data/is_sessions_local.json';

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, PUT, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { ...CORS, 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }
  });
}

async function ghRead(ghHeaders) {
  const resp = await fetch(GH_API, { headers: ghHeaders });
  if (!resp.ok) throw new Error(`GitHub GET ${resp.status}`);
  const info = await resp.json();
  let sessions;
  if (info.content) {
    sessions = JSON.parse(atob(info.content.replace(/\s/g, '')));
  } else if (info.git_url) {
    const blobResp = await fetch(info.git_url, { headers: ghHeaders });
    if (!blobResp.ok) throw new Error(`GitHub blob ${blobResp.status}`);
    const blob = await blobResp.json();
    sessions = JSON.parse(atob(blob.content.replace(/\s/g, '')));
  } else {
    sessions = [];
  }
  const arr = Array.isArray(sessions) ? sessions : (sessions.sessions || []);
  return { sessions: arr, sha: info.sha || '' };
}

const BLOB_KEYS = new Set(['full_dataurl', 'thumb_dataurl', 'anim']);
function slimSession(s) {
  return Object.fromEntries(Object.entries(s).filter(([k]) => !BLOB_KEYS.has(k)));
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === 'OPTIONS') return new Response(null, { headers: CORS });

    const url = new URL(request.url);
    const path = url.pathname;

    if (!env.GH_TOKEN)
      return json({ error: 'GH_TOKEN not configured' }, 500);

    const ghHeaders = {
      'Authorization': `Bearer ${env.GH_TOKEN}`,
      'Accept': 'application/vnd.github.v3+json',
      'User-Agent': 'kellogg-sync-worker',
      'Content-Type': 'application/json',
    };

    // ── Binary asset endpoints: /thumb/:id  /drawing/:id  /anim/:id ─────────
    // All stored in THUMBS KV with type-prefixed keys (thumb: / drawing: / anim:)
    const assetMatch = path.match(/^\/(thumb|drawing|anim)\/([a-zA-Z0-9_-]+)$/);
    if (assetMatch) {
      const [, type, sid] = assetMatch;
      if (!env.THUMBS) return json({ error: 'THUMBS KV namespace not bound' }, 500);
      const key = type + ':' + sid;

      if (request.method === 'GET') {
        const val = await env.THUMBS.get(key);
        if (!val) return new Response('', { status: 404, headers: CORS });
        const ct = type === 'anim' ? 'application/json' : 'text/plain';
        const cc = type === 'thumb' ? 'public, max-age=86400' : 'public, max-age=604800';
        return new Response(val, { headers: { ...CORS, 'Content-Type': ct, 'Cache-Control': cc } });
      }

      if (request.method === 'PUT') {
        const body = await request.text();
        if (!body) return json({ error: 'empty body' }, 400);
        await env.THUMBS.put(key, body);
        return json({ ok: true });
      }

      return new Response('Method not allowed', { status: 405, headers: CORS });
    }

    // ── Session list: /sessions ───────────────────────────────────────────────
    if (!path.endsWith('/sessions'))
      return new Response('Not found', { status: 404, headers: CORS });

    try {
      if (request.method === 'GET') {
        const { sessions } = await ghRead(ghHeaders);
        // Migrate any thumbnails still in GitHub JSON → KV (background, non-blocking)
        if (env.THUMBS) {
          const toMigrate = sessions.filter(s => s.session_id && s.thumb_dataurl);
          if (toMigrate.length) {
            ctx.waitUntil(
              Promise.all(toMigrate.map(s => env.THUMBS.put('thumb:' + s.session_id, s.thumb_dataurl)))
            );
          }
        }
        return json(sessions.map(slimSession));
      }

      if (request.method === 'POST') {
        const body = await request.json();
        const incoming = body.sessions || [];
        const deletedIds = new Set(body.deleted || []);

        let existing = [], sha = '';
        try { ({ sessions: existing, sha } = await ghRead(ghHeaders)); } catch {}

        // Migrate any thumbnails still in GitHub JSON → KV before rewriting the file
        if (env.THUMBS) {
          const toMigrate = existing.filter(s => s.session_id && s.thumb_dataurl);
          if (toMigrate.length) {
            await Promise.all(toMigrate.map(s => env.THUMBS.put('thumb:' + s.session_id, s.thumb_dataurl)));
          }
        }

        const byId = {};
        existing.forEach(s => { if (s.session_id) byId[s.session_id] = slimSession(s); });
        incoming.forEach(s => {
          if (!s.session_id) return;
          byId[s.session_id] = { ...(byId[s.session_id] || {}), ...slimSession(s) };
        });
        deletedIds.forEach(id => {
          delete byId[id];
          if (env.THUMBS) ctx.waitUntil(Promise.all([
            env.THUMBS.delete('thumb:' + id),
            env.THUMBS.delete('drawing:' + id),
            env.THUMBS.delete('anim:' + id),
          ]));
        });

        const upload = Object.values(byId);
        const content = btoa(unescape(encodeURIComponent(JSON.stringify(upload, null, 2))));
        const putBody = JSON.stringify({ message: 'sync: IS Studio session', content, ...(sha ? { sha } : {}) });
        const put = await fetch(GH_API, { method: 'PUT', headers: ghHeaders, body: putBody });
        if (!put.ok) {
          const err = await put.text();
          return json({ error: `GitHub PUT ${put.status}: ${err}` }, 502);
        }
        return json({ ok: true });
      }

      return new Response('Method not allowed', { status: 405, headers: CORS });

    } catch (e) {
      return json({ error: e.message }, 500);
    }
  }
};
