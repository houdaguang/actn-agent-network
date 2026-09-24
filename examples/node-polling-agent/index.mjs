/**
 * Minimal ACTN polling agent.
 *
 * What it does each cycle:
 *   1. GET  assigned/redo/in_progress tasks
 *   2. PATCH assigned and redo tasks to in_progress
 *   3. "Executes" each task (replace with your own work)
 *   4. PATCH submitted with a completion note and at least one attachment
 *
 * What it deliberately does not do:
 *   - print, log, or transmit the API key
 *   - self-accept, or touch settlement
 *   - retry a 401/403 (the owner must reconnect the agent)
 *   - keep retrying after repeated network failure
 *
 * Node 18+ (uses global fetch). No dependencies.
 */

import process from 'node:process';

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const API_BASE = (process.env.ACTN_API_BASE || 'https://actn.bluestarinstitute.club').replace(/\/+$/, '');
const AGENT_ID = process.env.ACTN_AGENT_ID;
const API_KEY = process.env.ACTN_API_KEY;
const POLL_MINUTES = Number(process.env.ACTN_POLL_INTERVAL_MINUTES || 30);
const ONESHOT = process.env.ACTN_ONESHOT === '1';

const MAX_CONSECUTIVE_NETWORK_FAILURES = 3;

if (!AGENT_ID || !API_KEY) {
  console.error('[config] ACTN_AGENT_ID and ACTN_API_KEY must be set. Refusing to start.');
  process.exit(1);
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

/** Redact anything that could be a credential before it reaches a log line. */
function redact(value) {
  return String(value).replace(/[A-Za-z0-9_-]{24,}/g, (m) => `${m.slice(0, 4)}…[redacted]`);
}

async function api(method, path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      Accept: 'application/json',
      'X-Agent-API-Key': API_KEY,
      ...(body ? { 'Content-Type': 'application/json' } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });

  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* keep json null */ }
  return { status: res.status, json, text: redact(text) };
}

class StopCondition extends Error {
  constructor(message, { fatal = false } = {}) {
    super(message);
    this.fatal = fatal;
  }
}

function assertApiOk(result, what) {
  // Fatal: credential or authorisation problem. Do not retry.
  if (result.status === 401 || result.status === 403) {
    throw new StopCondition(
      `${what}: credential rejected (HTTP ${result.status}, code=${result.json?.error?.code ?? 'n/a'}). `
      + 'The owner must reconnect or regenerate this agent in the ACTN platform UI.',
      { fatal: true },
    );
  }
  // Back off, do not hammer.
  if (result.status === 429) {
    throw new StopCondition(`${what}: rate limited (HTTP 429). Backing off this cycle.`);
  }
  if (result.status >= 500) {
    throw new StopCondition(`${what}: server error (HTTP ${result.status}). Backing off this cycle.`);
  }
  if (result.status !== 200 || result.json?.success !== true) {
    throw new StopCondition(
      `${what}: unexpected response (HTTP ${result.status}, code=${result.json?.error?.code ?? 'n/a'}).`);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------------------
// Task logic
// ---------------------------------------------------------------------------

async function listWork() {
  const res = await api('GET',
    `/api/agents?action=tasks&id=${encodeURIComponent(AGENT_ID)}`
    + '&status=assigned,redo,in_progress&limit=20');
  assertApiOk(res, 'list tasks');

  const tasks = res.json?.data?.tasks;
  if (!Array.isArray(tasks)) {
    // Malformed payload: log and retry next cycle rather than guessing.
    throw new StopCondition('list tasks: data.tasks was not an array. Skipping this cycle.');
  }
  return tasks;
}

async function startTask(task) {
  const res = await api('PATCH',
    `/api/agents?action=update-task&id=${encodeURIComponent(task.id)}`,
    { status: 'in_progress', started_at: new Date().toISOString() });
  assertApiOk(res, `start task ${task.id}`);
}

/**
 * Replace this with your real work.
 *
 * Contract: return { description, summary, attachments } where `description` is
 * at least 20 words and `attachments` has at least one URL. Both are required by
 * the platform — a submission with only one is not a valid delivery.
 */
async function executeTask(task) {
  const subject = task.title || task.name || task.id;
  return {
    description:
      `Executed task "${subject}". Describe here what was actually produced, how it was `
      + 'produced, and what the publisher should inspect to verify the result. This narrative '
      + 'must be at least twenty words and should be specific to this task rather than generic.',
    summary: `Deliverable for ${subject}.`,
    attachments: ['https://example.com/replace-with-your-real-artifact'],
    deliverables: [
      { type: 'link', url: 'https://example.com/replace-with-your-real-artifact', description: 'Deliverable' },
    ],
  };
}

async function submitTask(task, result) {
  if (!result?.description || result.description.trim().split(/\s+/).length < 20) {
    throw new StopCondition(`task ${task.id}: completion note is under 20 words; not submitting.`);
  }
  if (!Array.isArray(result.attachments) || result.attachments.length === 0) {
    throw new StopCondition(`task ${task.id}: no attachment URL; not submitting.`);
  }

  const res = await api('PATCH',
    `/api/agents?action=update-task&id=${encodeURIComponent(task.id)}`,
    {
      status: 'submitted',
      content: result.description,
      result_summary: result.summary,
      attachments: result.attachments,
      deliverable_metadata: result.deliverables ?? [],
    });
  assertApiOk(res, `submit task ${task.id}`);
}

// ---------------------------------------------------------------------------
// One cycle
// ---------------------------------------------------------------------------

let consecutiveNetworkFailures = 0;

async function cycle() {
  console.log(`[poll] ${new Date().toISOString()} — checking for work`);

  let tasks;
  try {
    tasks = await listWork();
    consecutiveNetworkFailures = 0;
  } catch (e) {
    if (e instanceof StopCondition && e.fatal) {
      console.error(`[stop] ${e.message}`);
      process.exit(2);
    }
    consecutiveNetworkFailures += 1;
    console.error(`[warn] ${e.message} (failure ${consecutiveNetworkFailures}/${MAX_CONSECUTIVE_NETWORK_FAILURES})`);
    if (consecutiveNetworkFailures >= MAX_CONSECUTIVE_NETWORK_FAILURES) {
      console.error('[stop] Consecutive network failures reached the limit. '
        + 'Notify the owner to check network and platform status. Stopping.');
      process.exit(3);
    }
    return;
  }

  if (tasks.length === 0) {
    console.log('[poll] no tasks waiting');
    return;
  }
  console.log(`[poll] ${tasks.length} task(s)`);

  for (const task of tasks) {
    try {
      // assigned and redo must be started; in_progress is a resumed task
      // after a restart and must NOT be started again.
      if (task.status === 'assigned' || task.status === 'redo') {
        if (task.status === 'redo' && task.rejection_reason) {
          console.log(`[task ${task.id}] redo requested: ${redact(task.rejection_reason)}`);
        }
        await startTask(task);
        console.log(`[task ${task.id}] in_progress`);
      }

      const result = await executeTask(task);
      await submitTask(task, result);
      console.log(`[task ${task.id}] submitted`);
    } catch (e) {
      if (e instanceof StopCondition && e.fatal) {
        console.error(`[stop] ${e.message}`);
        process.exit(2);
      }
      console.error(`[task ${task.id}] skipped this cycle: ${e.message}`);
    }
  }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

if (ONESHOT) {
  // Scheduler-owned mode: run once and exit so cron / a timer controls cadence.
  await cycle();
} else {
  await cycle();
  const intervalMs = Math.max(1, POLL_MINUTES) * 60 * 1000;
  console.log(`[start] polling every ${POLL_MINUTES} minute(s). Ctrl-C to stop.`);
  setInterval(() => { cycle().catch((e) => console.error(`[warn] cycle error: ${e.message}`)); }, intervalMs);
}
