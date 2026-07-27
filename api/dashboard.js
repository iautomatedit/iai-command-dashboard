// Reads the latest execution of the "Build 3A - Master Clients Bridge" n8n
// workflow (id below) via n8n's REST API and returns its output payload.
//
// This does NOT call the workflow's webhook directly. The webhook's public
// URL was confirmed unreachable from outside n8n during this build (a real,
// unresolved n8n Cloud infrastructure issue, not a bug in this function) -
// see decisions/log.md for the full investigation. Instead, the bridge
// workflow refreshes itself on a 5-minute schedule trigger, and this
// function reads back its most recent successful execution's result data.
// That means dashboard data is as fresh as the last schedule run, not
// instantaneous - the frontend surfaces that honestly via generated_at.

const BRIDGE_WORKFLOW_ID = 'n3wqpOoa4qaKhnhf';
const BRIDGE_NODE_NAME = 'Build Dashboard Payload';

export default async function handler(req, res) {
  const { N8N_API_KEY, N8N_BASE_URL } = process.env;

  if (!N8N_API_KEY || !N8N_BASE_URL) {
    res.status(500).json({ error: 'Missing N8N_API_KEY or N8N_BASE_URL environment variable.' });
    return;
  }

  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 7000);

    const url = `${N8N_BASE_URL}/api/v1/executions?workflowId=${BRIDGE_WORKFLOW_ID}&status=success&limit=1&includeData=true`;
    const response = await fetch(url, {
      headers: { 'X-N8N-API-KEY': N8N_API_KEY },
      signal: controller.signal
    });
    clearTimeout(timeout);

    if (!response.ok) {
      res.status(502).json({ error: `n8n API returned ${response.status}` });
      return;
    }

    const body = await response.json();
    const execution = body.data && body.data[0];

    if (!execution) {
      res.status(200).json({
        summary: { total_clients: 0, green: 0, amber: 0, red: 0, generated_at: null },
        clients: [],
        stale: true,
        note: 'No successful bridge execution found yet. The bridge workflow refreshes every 5 minutes - check back shortly, or trigger it manually in n8n.'
      });
      return;
    }

    const runData = execution.data && execution.data.resultData && execution.data.resultData.runData;
    const nodeOutput = runData && runData[BRIDGE_NODE_NAME] && runData[BRIDGE_NODE_NAME][0];
    const payload = nodeOutput && nodeOutput.data && nodeOutput.data.main && nodeOutput.data.main[0] && nodeOutput.data.main[0][0] && nodeOutput.data.main[0][0].json;

    if (!payload) {
      res.status(502).json({ error: 'Bridge execution found but payload shape was unexpected.' });
      return;
    }

    res.setHeader('Cache-Control', 's-maxage=60, stale-while-revalidate=120');
    res.status(200).json({
      ...payload,
      execution_started_at: execution.startedAt,
      execution_stopped_at: execution.stoppedAt
    });
  } catch (err) {
    res.status(500).json({ error: err.name === 'AbortError' ? 'Request to n8n timed out' : String(err.message || err) });
  }
}
