/**
 * B06 screens on the dev/test fixture transport with the ILLUSTRATIVE night3
 * story (src/fixtures/illustrative/recorder_v1.json). Fixture-only: NOT B06
 * integration acceptance, which needs lane A's A02/A09 API.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Ajv2020 from 'ajv/dist/2020';
import addFormats from 'ajv-formats';
import { parse } from 'yaml';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { ApiClient } from '../api/client';
import type { AppConfig } from '../config/env';
import { RECORDER_CAPS, runNight3Correction, challengeTacoPolicy } from '../test/night3';
import { axeViolations, jsonResponse, makeServer, renderApp, type RecordedRequest } from '../test/renderApp';

const SPEC_ID = 'https://encore.invalid/openapi.draft.json';
let ajv: Ajv2020;
beforeAll(() => {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const spec = parse(readFileSync(path.resolve(here, '../../contracts-draft/openapi.draft.yaml'), 'utf8')) as object;
  ajv = new Ajv2020({ strict: false, allErrors: true });
  addFormats(ajv);
  ajv.addSchema({ ...spec, $id: SPEC_ID });
});
function expectSchema(name: string, data: unknown) {
  const v = ajv.getSchema(`${SPEC_ID}#/components/schemas/${name}`)!;
  if (!v(data)) throw new Error(`${name}: ${ajv.errorsText(v.errors)}`);
}

const night3 = (opts: Parameters<typeof makeServer>[0] = {}) => makeServer({ story: 'night3', capabilities: RECORDER_CAPS, ...opts });
const liveConfig: AppConfig = { dataMode: 'live', apiOrigin: '', authLoginUrl: null };

function deferred() {
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  return { gate, release };
}

/** Controllable SSE stream on global fetch (the API itself goes through the fixture transport). */
function stubStream() {
  const enc = new TextEncoder();
  const state: { push: ((seq: number) => void) | null } = { push: null };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init: RequestInit) =>
      new Response(
        new ReadableStream({
          start(c) {
            state.push = (seq) => {
              const e = { event_id: `sse_${seq}`, type: 'challenge.created', schema_version: 1, cursor: `c${seq}`, stream_sequence: seq, aggregate_id: 'x', aggregate_version: 1, occurred_at: '2026-10-19T17:00:00Z', recorded_at: '2026-10-19T17:00:00Z' };
              c.enqueue(enc.encode(`id: c${seq}\ndata: ${JSON.stringify(e)}\n\n`));
            };
            init.signal?.addEventListener('abort', () => c.error(new DOMException('Aborted', 'AbortError')));
          },
        }),
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
      ),
    ),
  );
  return state;
}
afterEach(() => vi.unstubAllGlobals());

const cards = () => within(screen.getByRole('list', { name: /Events, page/ })).getAllByRole('article');

// ------------------------------------------------------------------- timeline

describe('flight recorder timeline', () => {
  it('distinguishes facts, agent proposals, attempted actions, confirmed effects and projections; never upgrades a proposal or intent', async () => {
    const { transport } = night3();
    const { container } = renderApp('/recorder', transport);
    await waitFor(() => expect(cards().length).toBe(25));
    for (const card of cards()) {
      const text = card.textContent ?? '';
      if (text.includes('Agent proposal')) {
        expect(text).toMatch(/not an approval/);
        expect(text).not.toMatch(/Confirmed effect/);
      }
      if (text.includes('Attempted action')) {
        expect(text).toMatch(/not confirmed/);
        expect(text).not.toMatch(/Confirmed effect/);
      }
    }
    // "Submitted" is an attempted action, not a confirmed effect; only the provider confirmation is an effect.
    const submitted = cards().find((c) => c.textContent?.includes('payout.submitted'))!;
    expect(within(submitted).getByText('Attempted action — not confirmed')).toBeInTheDocument();
    const confirmed = cards().find((c) => c.textContent?.includes('payout.confirmed'))!;
    expect(within(confirmed).getByText('Confirmed effect')).toBeInTheDocument();
    const late = cards().find((c) => c.textContent?.includes('ingested late'))!;
    expect(within(late).getByText('Recorded later, effective earlier')).toBeInTheDocument();
    expect(within(late).getByText('Recorded (knowledge time)')).toBeInTheDocument();
    expect(within(late).getByText('Occurred (business time)')).toBeInTheDocument();
    // Hash chain: an adjacent older event on the page is compared (display check only; the server verifies).
    await userEvent.setup().click(within(cards()[1]!).getByRole('button', { name: 'Details' }));
    expect(within(cards()[1]!).getByText('Matches the hash of the previous event shown.')).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it('shows C06 fields in details, opens sources in the evidence drawer and links actor, run, vendor and records', async () => {
    const user = userEvent.setup();
    const { transport } = night3();
    renderApp('/recorder?aggregate_id=illus_pv_taco_rate_v1', transport);
    await waitFor(() => expect(cards().length).toBeGreaterThan(0));
    const approved = cards().find((c) => c.textContent?.includes('policy.approved'))!;
    expect(within(approved).getByRole('link', { name: /Events by Maya R\./ })).toHaveAttribute('href', '/recorder?actor_id=illus_actor_maya');
    expect(within(approved).getByText('policy.approve')).toBeInTheDocument();
    expect(within(approved).getByRole('link', { name: 'illus_run_n1' })).toHaveAttribute('href', '/runs/illus_run_n1');
    await user.click(within(approved).getByRole('button', { name: 'Details' }));
    expect(within(approved).getByRole('button', { name: 'Hide details' })).toHaveAttribute('aria-expanded', 'true');
    expect(within(approved).getByText('illus_cmd_0002')).toBeInTheDocument();
    expect(within(approved).getByRole('link', { name: 'illus_corr_n1_taco' })).toBeInTheDocument();
    expect(within(approved).getAllByText(/^sha256:[0-9a-f]{64}$/, { selector: 'code' })).toHaveLength(2); // hash + previous hash
    expect(within(approved).getByText('Previous event is not on this page.')).toBeInTheDocument();
    expect(within(approved).getByRole('link', { name: /Approval: Reusable policy approval|Policy version: Policy v1/ })).toBeInTheDocument();
    await user.click(within(approved).getByRole('button', { name: /Sources for Event #7 inputs/ }));
    expect(await screen.findByRole('dialog')).toHaveTextContent(/3\.1 Commission: 15%/);
  });

  it('filters through the URL (category, actor type, time range in festival time) and keeps filters on reload', async () => {
    const user = userEvent.setup();
    const { transport, requests } = night3();
    const a = renderApp('/recorder', transport);
    await waitFor(() => expect(cards().length).toBeGreaterThan(0));
    await user.selectOptions(screen.getByLabelText('Category'), 'PROPOSAL');
    await user.selectOptions(screen.getByLabelText('Actor type'), 'AGENT');
    await user.type(screen.getByLabelText('Recorded from'), '2026-10-19T00:00');
    await user.click(screen.getByRole('button', { name: 'Apply filters' }));
    await waitFor(() => expect(cards()).toHaveLength(1));
    expect(cards()[0]).toHaveTextContent('agent.supersession_flagged');
    const last = requests.filter((r) => r.path === '/api/v1/events').pop()!;
    expect(last.url).toContain('category=PROPOSAL');
    expect(last.url).toContain('recorded_from=2026-10-19T07%3A00%3A00Z'); // Pacific midnight in UTC
    a.unmount();
    renderApp('/recorder?category=PROPOSAL', transport);
    await waitFor(() => expect(cards()).toHaveLength(2));
    expect(screen.getByLabelText('Category')).toHaveValue('PROPOSAL');
  });

  it('live SSE events keep the pinned list, expanded details and filters; new events are only indicated', async () => {
    const user = userEvent.setup();
    const stream = stubStream();
    const { transport, inner } = night3();
    renderApp('/recorder?category=FACT', transport, liveConfig);
    await waitFor(() => expect(cards().length).toBeGreaterThan(0));
    await waitFor(() => expect(stream.push).not.toBeNull());
    await waitFor(() => expect(screen.getByText(/View pinned at stream sequence/)).toBeInTheDocument());
    const before = cards().map((c) => c.textContent);
    await user.click(within(cards()[0]!).getByRole('button', { name: 'Details' }));

    await challengeTacoPolicy(new ApiClient({ transport: inner })); // another user records a challenge
    stream.push!(1);
    expect(await screen.findByRole('button', { name: 'Show new events' })).toBeInTheDocument();
    expect(screen.getByText(/3 new events/)).toBeInTheDocument();
    expect(cards().map((c) => c.textContent)).toEqual(before.map((t, i) => (i === 0 ? cards()[0]!.textContent : t)));
    expect(within(cards()[0]!).getByRole('button', { name: 'Hide details' })).toBeInTheDocument();
    expect(screen.getByLabelText('Category')).toHaveValue('FACT');

    await user.click(screen.getByRole('button', { name: 'Show new events' }));
    await waitFor(() => expect(cards()[0]).toHaveTextContent(/payout\.held|policy\.suspended|challenge\.created/));
    expect(cards().some((c) => c.textContent?.includes('challenge.created'))).toBe(true);
    expect(screen.getByLabelText('Category')).toHaveValue('FACT');
    expect(screen.queryByRole('button', { name: 'Show new events' })).not.toBeInTheDocument();
  });
});

// -------------------------------------------------------------------- history

describe('historical state (as known then)', () => {
  it('separates the knowledge-time cutoff from the business-date filter and only reads (GET)', async () => {
    const user = userEvent.setup();
    const { transport, requests } = night3();
    const { container } = renderApp('/recorder/history', transport);
    expect(await screen.findByText('Choose a knowledge-time cutoff')).toBeInTheDocument();
    await user.type(screen.getByLabelText('Knowledge-time cutoff'), '2026-10-19T05:00');
    await user.selectOptions(screen.getByLabelText('Business date'), '2026-10-16');
    await user.click(screen.getByRole('button', { name: 'Show history' }));
    const table = await screen.findByRole('table', { name: /Records as known at 2026-10-19T12:00:00Z/ });
    expect(within(table).getByRole('row', { name: /Taco Libre: apply the producer email rate/ })).toHaveTextContent('ACTIVE');
    expect(within(table).queryByText(/follow-up email/)).not.toBeInTheDocument();
    const later = screen.getByRole('heading', { name: /Recorded after this cutoff/ }).closest('section')!;
    expect(later).toHaveTextContent(/ingested late/);
    const hist = requests.filter((r) => r.path === '/api/v1/history/projection').pop()!;
    expect(hist.url).toContain('knowledge_cutoff=2026-10-19T12%3A00%3A00Z');
    expect(hist.url).toContain('business_date=2026-10-16');
    expect(requests.every((r) => r.method === 'GET')).toBe(true);
    expect(screen.getAllByRole('note').some((n) => /READ-ONLY HISTORY/.test(n.textContent ?? ''))).toBe(true);
    expect(await axeViolations(container)).toEqual([]);
  });

  it('history for a past cutoff does not silently change when a later correction arrives (live refetch)', async () => {
    const stream = stubStream();
    const { transport, inner, requests } = night3();
    renderApp('/recorder/history?cutoff=2026-10-19T12:00:00Z&business_date=2026-10-16', transport, liveConfig);
    const table = await screen.findByRole('table', { name: /Records as known at/ });
    const before = table.textContent;
    const laterSection = () => screen.getByRole('heading', { name: /Recorded after this cutoff/ }).closest('section')!;
    expect(laterSection()).not.toHaveTextContent(/Correcting entry posted/);
    await waitFor(() => expect(stream.push).not.toBeNull());
    const count = () => requests.filter((r) => r.path === '/api/v1/history/projection').length;
    const n = count();

    await runNight3Correction(new ApiClient({ transport: inner }));
    stream.push!(1);
    await waitFor(() => expect(count()).toBeGreaterThan(n));
    await waitFor(() => expect(laterSection()).toHaveTextContent(/Correcting entry posted: Night 1 commission/));
    expect(screen.getByRole('table', { name: /Records as known at/ }).textContent).toBe(before);
    expect(requests.filter((r) => r.method !== 'GET')).toEqual([]); // the history screen itself sent no command
  });
});

// ---------------------------------------------------------- replays/branches

describe('replays and branches are labelled and cannot touch production', () => {
  it('replay: side-effect-free label, explicit start, progress and zero effects; no production controls', async () => {
    const user = userEvent.setup();
    const { transport, commands } = night3();
    const { container } = renderApp('/recorder/replays', transport);
    expect(screen.getAllByRole('note').some((n) => /SIDE-EFFECT-FREE REPLAY.*no live Claude calls, no payments and no production postings/.test(n.textContent ?? ''))).toBe(true);
    await screen.findByRole('table', { name: 'Replays' });
    await user.selectOptions(await screen.findByLabelText('Close run to replay'), 'illus_run_n2');
    await user.click(screen.getByRole('button', { name: 'Review replay' }));
    expect(commands()).toHaveLength(0);
    await user.click(screen.getByRole('button', { name: 'Start side-effect-free replay' }));
    await screen.findByRole('heading', { name: /Replay of run/ });
    expectSchema('StartReplayPayload', JSON.parse(commands()[0]!.body!).payload);
    await waitFor(() => expect(screen.getByText(/7 of 7 recorded results consumed/)).toBeInTheDocument(), { timeout: 6000 });
    expect(screen.getByText('Live Claude calls').nextSibling).toHaveTextContent('0');
    expect(screen.getByText('Payment effects').nextSibling).toHaveTextContent('0');
    expect(screen.queryByRole('button', { name: /apply|promote|resend|pay|post/i })).not.toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });

  it('read-only sessions get no start control', async () => {
    const { transport } = night3({ capabilities: ['fixture.read'] });
    renderApp('/recorder/replays', transport);
    expect(await screen.findByText(/does not include the/)).toHaveTextContent('replay.start');
    expect(screen.queryByRole('button', { name: 'Review replay' })).not.toBeInTheDocument();
  });

  it('branch: "BRANCH — not production" everywhere, server comparison, no promote/merge/apply button, only a link to a new production command', async () => {
    const { transport, commands } = night3();
    const { container } = renderApp('/recorder/branches/illus_branch_taco_15', transport);
    const table = await screen.findByRole('table', { name: /Production compared with this what-if branch/ });
    expect(within(table).getByRole('columnheader', { name: 'BRANCH (not production)' })).toBeInTheDocument();
    const row = within(table).getByRole('row', { name: /Commission assessed, Night 2/ });
    expect(row).toHaveTextContent('$500.00');
    expect(row).toHaveTextContent('$750.00');
    expect(row).toHaveTextContent('+$250.00');
    expect(screen.getAllByRole('note').some((n) => /BRANCH — not production.*cannot be promoted/.test(n.textContent ?? ''))).toBe(true);
    expect(screen.queryByRole('button', { name: /promote|merge|apply|overwrite|publish/i })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Start a challenge of policy version illus_pv_taco_rate_v1 in production/ })).toHaveAttribute(
      'href',
      '/recorder/challenges/new?kind=POLICY_VERSION&id=illus_pv_taco_rate_v1',
    );
    expect(commands()).toHaveLength(0);
    expect(await axeViolations(container)).toEqual([]);
  });

  it('opening a branch sends one explicit command and shows it as a branch', async () => {
    const user = userEvent.setup();
    const { transport, commands } = night3();
    renderApp('/recorder/branches', transport);
    await user.type(await screen.findByLabelText('Branch name'), 'Taco at 15%');
    await user.type(screen.getByLabelText('Start from production as known at'), '2026-10-19T09:45');
    await user.selectOptions(screen.getByLabelText('Policy version to override'), 'illus_pv_taco_rate_v1');
    await user.type(screen.getByLabelText(/Commission in the branch/), '1500');
    await user.click(screen.getByRole('button', { name: 'Review branch' }));
    expect(screen.getByText('None: isolated storage, external effects disabled')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Open branch' }));
    expect(await screen.findByRole('heading', { name: /BRANCH: Taco at 15%/ })).toBeInTheDocument();
    expect(commands()).toHaveLength(1);
    const body = JSON.parse(commands()[0]!.body!);
    expect(body.expected_version).toBeNull();
    expectSchema('CreateBranchPayload', body.payload);
    expect(body.payload.base_knowledge_cutoff).toBe('2026-10-19T16:45:00Z');
  });
});

// ------------------------------------------- challenge -> preview -> approve

describe('Night 3: challenge, impact preview, explicit approval, progress and original vs corrected', () => {
  async function challengeThroughUi(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole('link', { name: 'Challenge this policy version' }));
    await screen.findByRole('heading', { name: /Challenged policy version/ });
    await user.selectOptions(await screen.findByLabelText('Superseding evidence (source document)'), 'illus_src_taco_followup');
    await user.type(screen.getByLabelText(/Why this policy version is wrong/), 'Producer confirmed 15% for festival nights.');
    await user.click(screen.getByLabelText(/I understand recording this challenge pauses use/));
    await user.click(screen.getByRole('button', { name: 'Review challenge' }));
    await user.click(screen.getByRole('button', { name: 'Confirm challenge' }));
  }

  it('walks the whole story with exact bindings, one POST per intent and no success before the server confirms', async () => {
    const user = userEvent.setup();
    const hold = deferred();
    let held = false;
    const { transport, commands, requests } = night3({
      intercept: (req, forward) => {
        if (!held && /\/corrections\/[^/]+\/approve$/.test(req.path)) {
          held = true;
          return hold.gate.then(forward);
        }
        return undefined;
      },
    });
    const { container } = renderApp('/policies/illus_pv_taco_rate_v1', transport);
    await challengeThroughUi(user);

    // Challenge recorded: policy paused, dependent draft payout held.
    const paused = (await screen.findByRole('heading', { name: 'Paused while the challenge is open' })).closest('section')!;
    expect(paused).toHaveTextContent(/ACTIVE.*SUSPENDED/);
    expect(paused).toHaveTextContent(/Draft payout held/);
    const challengeCmd = commands()[0]!;
    expect(challengeCmd.headers['idempotency-key']).toBeTruthy();
    const env = JSON.parse(challengeCmd.body!);
    expectSchema('CommandEnvelope', env);
    expectSchema('CreateChallengePayload', env.payload);
    expect(env.expected_version).toBe(2);
    expect(env.payload.target).toEqual({ kind: 'POLICY_VERSION', id: 'illus_pv_taco_rate_v1', version: 2 });

    await user.click(screen.getByRole('button', { name: 'Request impact preview' }));
    await screen.findByRole('heading', { name: /Impact preview by state/ });
    for (const h of [/Draft — revise/, /Pending — request cancellation/, /Completed — offset or recovery/, /Posted books/, /Statements/, /No remaining payout — human recovery or waiver/]) {
      expect(screen.getByRole('heading', { name: h })).toBeInTheDocument();
    }
    const pending = screen.getByRole('table', { name: /Pending — request cancellation/ });
    expect(within(pending).getByText('Request cancellation and reconcile')).toBeInTheDocument();
    expect(within(pending).getByText('−$250.00')).toBeInTheDocument();
    const completed = screen.getByRole('table', { name: /Completed — offset or recovery/ });
    expect(completed).toHaveTextContent(/not reversed or "unsent"/);
    expect(screen.getByRole('table', { name: /No remaining payout/ })).toHaveTextContent('Needs a human decision');
    expect(await axeViolations(container)).toEqual([]);

    // Explicit approval bound to version + hashes; double click sends one POST; no success while pending.
    const before = commands().length;
    await user.click(screen.getByLabelText(/I approve exactly this correction plan \(9 affected records\)/));
    await user.click(screen.getByRole('button', { name: 'Review correction approval' }));
    const review = screen.getByLabelText('Review correction approval');
    expect(review).toHaveTextContent(/Bound to correction version1/);
    const confirm = within(review).getByRole('button', { name: 'Confirm correction approval' });
    await user.dblClick(confirm);
    await user.click(within(review).getByRole('button', { name: /Approving/ })).catch(() => undefined);
    expect(commands()).toHaveLength(before + 1);
    expect(screen.getByText(/Submitting… waiting for the server to confirm/)).toBeInTheDocument();
    expect(screen.queryByText(/correction completed/)).not.toBeInTheDocument();
    const approve = commands()[before]!;
    const aenv = JSON.parse(approve.body!);
    expectSchema('ApproveCorrectionPayload', aenv.payload);
    expect(aenv.expected_version).toBe(1);
    expect(aenv.payload.item_ids).toHaveLength(9);
    hold.release();

    expect(await screen.findByText(/Accepted by the server for processing/)).toHaveTextContent(/Not complete yet/);
    await waitFor(() => expect(screen.getByText(/correction completed \(server confirmed\)/)).toBeInTheDocument(), { timeout: 8000 });
    const results = screen.getByRole('table', { name: 'Original and corrected records' });
    expect(within(results).getAllByRole('link', { name: 'Journal entry: Original entry (kept) v1' }).map((l) => l.getAttribute('href'))).toEqual(['/journal/illus_je_n1_fee_taco', '/journal/illus_je_n2_fee_taco']);
    expect(within(results).getAllByRole('link', { name: 'Journal entry: Reversing entry v1' })).toHaveLength(2);
    expect(within(results).getByRole('link', { name: 'Payout: Replacement payout v1' })).toHaveAttribute('href', '/payouts/illus_payout_n2_taco_c1');
    expect(screen.getByRole('link', { name: 'History as known when the correction was approved' })).toBeInTheDocument();
    expect(requests.some((r) => r.path.startsWith('/api/v1/jobs/'))).toBe(true);
  }, 20000);

  it('409 on approval: nothing applied, the correction is refetched and the review must be redone', async () => {
    const user = userEvent.setup();
    let conflictSent = false;
    const { transport, inner, commands } = night3({
      intercept: (req) => {
        if (!conflictSent && /\/approve$/.test(req.path) && req.path.includes('/corrections/')) {
          conflictSent = true;
          return Promise.resolve(jsonResponse(409, { code: 'VERSION_CONFLICT', message: 'Changed.', request_id: 'r1', retryable: false, current_version: 2 }));
        }
        return undefined;
      },
    });
    const client = new ApiClient({ transport: inner });
    const ch = await challengeTacoPolicy(client);
    const { previewFor } = await import('../test/night3');
    const cId = await previewFor(client, ch);
    renderApp(`/recorder/corrections/${cId}`, transport);
    await user.click(await screen.findByLabelText(/I approve exactly this correction plan/));
    await user.click(screen.getByRole('button', { name: 'Review correction approval' }));
    await user.click(screen.getByRole('button', { name: 'Confirm correction approval' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/Not applied.*changed since you reviewed it \(current version 2\)/);
    expect(screen.queryByText(/Accepted by the server/)).not.toBeInTheDocument();
    expect(commands()).toHaveLength(1);
  });

  it('422 shows field errors; a session without the capability sees no approval controls', async () => {
    const user = userEvent.setup();
    const { transport, inner } = night3({
      intercept: (req) =>
        /\/corrections\/[^/]+\/approve$/.test(req.path)
          ? Promise.resolve(jsonResponse(422, { code: 'PREVIEW_CHANGED', message: 'The impact preview changed since review.', request_id: 'r2', retryable: false, field_errors: [{ field: 'payload.preview_hash', message: 'stale' }] }))
          : undefined,
    });
    const client = new ApiClient({ transport: inner });
    const { previewFor } = await import('../test/night3');
    const cId = await previewFor(client, await challengeTacoPolicy(client));
    const a = renderApp(`/recorder/corrections/${cId}`, transport);
    await user.click(await screen.findByLabelText(/I approve exactly this correction plan/));
    await user.click(screen.getByRole('button', { name: 'Review correction approval' }));
    await user.click(screen.getByRole('button', { name: 'Confirm correction approval' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('The impact preview changed since review.');
    expect(alert).toHaveTextContent('payload.preview_hash');
    a.unmount();

    const ro = makeServer({ story: 'night3', capabilities: ['fixture.read', 'challenge.create'] });
    const roClient = new ApiClient({ transport: ro.inner });
    const id2 = await previewFor(roClient, await challengeTacoPolicy(roClient));
    renderApp(`/recorder/corrections/${id2}`, ro.transport);
    expect(await screen.findByText(/needed to approve a financial correction/)).toHaveTextContent('correction.approve');
    expect(screen.queryByRole('button', { name: /Review correction approval/ })).not.toBeInTheDocument();
  });

  it('a read-only session cannot record a challenge', async () => {
    const { transport } = night3({ capabilities: ['fixture.read'] });
    renderApp('/recorder/challenges/new?kind=POLICY_VERSION&id=illus_pv_taco_rate_v1', transport);
    expect(await screen.findByText(/needed to record a challenge/)).toHaveTextContent('challenge.create');
    expect(screen.queryByRole('button', { name: 'Review challenge' })).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------- payout trace

describe('payout trace (sources and the original human instruction)', () => {
  it('walks payout -> pinned settlement -> policy version -> human decision -> sources', async () => {
    const user = userEvent.setup();
    const { transport } = night3();
    const { container } = renderApp('/payouts/illus_payout_n1_taco', transport);
    await user.click(await screen.findByRole('link', { name: 'Trace to sources' }));
    const lineage = await screen.findByRole('list', { name: 'Payout lineage' });
    await waitFor(() => expect(lineage).toHaveTextContent(/version 2 \(the exact version this payout pays\)/));
    expect(lineage).toHaveTextContent(/Taco Libre: apply the producer email rate \(10%\) for this festival v1/);
    expect(lineage).toHaveTextContent(/Reuse approved by a human: illus_actor_maya with policy\.approve/);
    expect(lineage).toHaveTextContent(/Decided by a human: illus_actor_maya with case\.resolve/);
    expect(lineage).toHaveTextContent(/chose “Apply the producer email rate \(10%\)”/);
    expect(lineage).toHaveTextContent('src_4dcc7ce9f370b56b');
    expect(lineage).toHaveTextContent('src_1b532360bfaa3697');
    await user.click(screen.getByRole('button', { name: /All sources for All sources in this trace/ }));
    expect(await screen.findByRole('dialog')).toHaveTextContent(/we agreed 10% commission for Taco Libre/);
    expect(await axeViolations(container)).toEqual([]);
  });

  it('after a later correction the trace still shows what was paid and why, and says a later version exists', async () => {
    const { transport, inner } = night3();
    await runNight3Correction(new ApiClient({ transport: inner }));
    renderApp('/payouts/illus_payout_n1_taco/trace', transport);
    const lineage = await screen.findByRole('list', { name: 'Payout lineage' });
    await waitFor(() => expect(lineage).toHaveTextContent(/A later settlement version 3 exists/));
    expect(lineage).toHaveTextContent(/ACTIVE when applied/);
    expect(lineage).toHaveTextContent(/now REVOKED/);
    expect(lineage).toHaveTextContent(/Decided by a human: illus_actor_maya/);
  });

  it('a payout without exception rules ends at contract terms and the automatic gate', async () => {
    const { transport } = night3();
    renderApp('/payouts/illus_payout_n1_kettle/trace', transport);
    const lineage = await screen.findByRole('list', { name: 'Payout lineage' });
    await waitFor(() => expect(lineage).toHaveTextContent(/released by the automatic gate within its limit/));
    expect(lineage).toHaveTextContent(/Kettle Corn Co: next-morning cash-office count belongs to the prior night v1/);
    const lemon = makeServer({ story: 'night3' });
    renderApp('/payouts/illus_payout_n1_lemon/trace', lemon.transport);
    await waitFor(() => expect(screen.getAllByRole('list', { name: 'Payout lineage' }).pop()).toHaveTextContent(/No exception rule was applied/));
  });
});

export type { RecordedRequest };
