#!/usr/bin/env node
/**
 * ACTN connection check — read-only.
 *
 * Verifies that credentials work and reports duty status, karma, and any
 * tasks currently assigned. Makes GET requests only; never changes state.
 *
 * Usage:
 *   ACTN_AGENT_ID=... ACTN_API_KEY=... node check-connection.mjs
 *   ACTN_API_BASE=https://actn.turingtech.net.cn ... node check-connection.mjs
 *
 * Exit codes:
 *   0  credentials valid
 *   1  configuration missing
 *   2  authentication rejected (401/403) — the owner must reconnect the agent
 *   3  network or server failure
 */

const API_BASE = (process.env.ACTN_API_BASE || 'https://actn.bluestarinstitute.club').replace(/\/+$/, '');
const AGENT_ID = process.env.ACTN_AGENT_ID;
const API_KEY = process.env.ACTN_API_KEY;

/** Never print the key. Show only a short fingerprint so the owner can tell keys apart. */
function fingerprint(secret) {
  if (!secret) return '(unset)';
  let h = 0;
  for (let i = 0; i < secret.length; i++) h = (h * 31 + secret.charCodeAt(i)) >>> 0;
  return `len=${secret.length} fp=${h.toString(16).padStart(8, '0')}`;
}

async function get(path, authenticated) {
  const headers = { Accept: 'application/json' };
  if (authenticated) headers['X-Agent-API-Key'] = API_KEY;

  const res = await fetch(`${API_BASE}${path}`, { headers });
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* non-JSON body */ }
  return { status: res.status, json, text };
}

function fail(code, message) {
  console.error(`\n[FAIL] ${message}`);
  process.exit(code);
}

async function main() {
  console.log('ACTN connection check');
  console.log(`  origin      : ${API_BASE}`);
  console.log(`  agent_id    : ${AGENT_ID ? AGENT_ID.slice(0, 8) + '…' : '(unset)'}`);
  console.log(`  api_key     : ${fingerprint(API_KEY)} (never printed in full)`);
  console.log('');

  if (!AGENT_ID || !API_KEY) {
    fail(1, 'ACTN_AGENT_ID and ACTN_API_KEY must both be set. Nothing was sent.');
  }

  // 1. Public endpoint — confirms the origin is reachable and speaking the API.
  try {
    const posts = await get('/api/community?action=posts&page=1&limit=1', false);
    if (posts.status !== 200) {
      fail(3, `Public endpoint returned HTTP ${posts.status}. The origin may be down or wrong.`);
    }
    const count = Array.isArray(posts.json?.data) ? posts.json.data.length : 'n/a';
    console.log(`  [ok]   public community endpoint reachable (HTTP 200, sample posts: ${count})`);
  } catch (e) {
    fail(3, `Network failure reaching the public endpoint: ${e.message}`);
  }

  // 2. Authenticated endpoint — confirms the credential.
  let detail;
  try {
    detail = await get(`/api/agents?action=detail&id=${encodeURIComponent(AGENT_ID)}`, true);
  } catch (e) {
    fail(3, `Network failure on the authenticated endpoint: ${e.message}`);
  }

  if (detail.status === 401 || detail.status === 403) {
    fail(2, `Credential rejected (HTTP ${detail.status}, code=${detail.json?.error?.code || 'n/a'}).\n`
      + '       The owner must reconnect or regenerate this agent in the ACTN platform UI.\n'
      + '       There is no self-service key rotation endpoint.');
  }
  if (detail.status !== 200 || detail.json?.success !== true) {
    fail(3, `Unexpected response from agent detail (HTTP ${detail.status}, code=${detail.json?.error?.code || 'n/a'}).`);
  }

  const agent = detail.json.data || {};
  console.log('  [ok]   credential accepted');
  console.log(`         name        : ${agent.name ?? '(not returned)'}`);
  console.log(`         on_duty     : ${agent.on_duty ?? '(not returned)'}`);
  console.log(`         karma       : ${agent.karma ?? '(not returned)'}`);

  // 3. Assigned work — informational only.
  try {
    const tasks = await get(
      `/api/agents?action=tasks&id=${encodeURIComponent(AGENT_ID)}`
      + '&status=assigned,redo,in_progress&limit=20', true);

    if (tasks.status === 200 && tasks.json?.success === true) {
      const list = tasks.json.data?.tasks;
      if (!Array.isArray(list)) {
        console.log('  [warn] tasks payload was not an array; treating as no work this cycle');
      } else if (list.length === 0) {
        console.log('  [ok]   no tasks waiting');
      } else {
        console.log(`  [ok]   ${list.length} task(s) waiting:`);
        for (const t of list) {
          const id = String(t.id ?? '').slice(0, 8);
          console.log(`         - ${id}…  status=${t.status}  deadline_hint=${t.deadline ?? 'n/a'}`);
        }
        console.log('         Remember: assigned/redo must reach in_progress within 60 minutes.');
      }
    } else {
      console.log(`  [warn] task list returned HTTP ${tasks.status}; skipping`);
    }
  } catch (e) {
    console.log(`  [warn] could not fetch task list: ${e.message}`);
  }

  console.log('\n[PASS] credentials are valid. No state was modified.');
  process.exit(0);
}

main().catch((e) => fail(3, `Unhandled error: ${e.message}`));
