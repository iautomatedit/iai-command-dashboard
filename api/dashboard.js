// Calls the "Build 3A - Master Clients Bridge" n8n workflow's production
// webhook directly and returns its live payload.
//
// Note on the URL format: n8n registers a webhook with a custom `path` at
//   /webhook/<path>
// NOT at /webhook/<webhookId>/<path>. The webhookId form only applies when
// the path is auto-generated. Getting this wrong returns a 404 whose message
// ("...is not registered") is literally accurate but easy to misread as the
// workflow being inactive. See decisions/log.md in mind-palace-app.

const BRIDGE_URL = 'https://xavierautomated.app.n8n.cloud/webhook/master-clients';

export default async function handler(req, res) {
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 7000);

    const response = await fetch(BRIDGE_URL, { signal: controller.signal });
    clearTimeout(timeout);

    if (!response.ok) {
      res.status(502).json({ error: `Bridge webhook returned ${response.status}` });
      return;
    }

    const payload = await response.json();

    if (!payload || !payload.summary) {
      res.status(502).json({ error: 'Bridge responded but payload shape was unexpected.' });
      return;
    }

    res.setHeader('Cache-Control', 'no-store');
    res.status(200).json(payload);
  } catch (err) {
    res.status(500).json({
      error: err.name === 'AbortError' ? 'Request to the n8n bridge timed out' : String(err.message || err)
    });
  }
}
