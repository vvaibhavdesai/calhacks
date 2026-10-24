/**
 * B04 decision workflow: one-time case resolution vs reusable policy approval.
 * Uses the dev/test fixture transport (ILLUSTRATIVE outputs) plus scripted
 * server responses. Fixture-only: not B04 integration acceptance.
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import Ajv2020 from 'ajv/dist/2020';
import addFormats from 'ajv-formats';
import { parse } from 'yaml';
import { beforeAll, describe, expect, it } from 'vitest';
import { axeViolations, jsonResponse, makeServer, renderApp, type RecordedRequest } from '../test/renderApp';

const CASE = 'illus_case_taco_rate';
const POLICY = 'illus_pv_taco_rate_v1';
const RESOLVE = `/api/v1/cases/${CASE}/resolve`;
const APPROVE = `/api/v1/policies/${POLICY}/approve`;
const DECIDERS = ['fixture.read', 'case.resolve', 'policy.approve'];

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

function deferred() {
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  return { gate, release };
}

async function chooseEmailRateAndReview(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('radio', { name: /Apply the producer email rate \(10%\)/ }));
  await user.click(screen.getByRole('button', { name: 'Review one-time decision' }));
  return screen.getByRole('button', { name: 'Confirm one-time resolution' });
}

function caseSection() {
  return screen.getByRole('heading', { name: 'One-time resolution (this case only)' }).closest('section') as HTMLElement;
}

async function policySection() {
  return (await screen.findByRole('heading', { name: 'Reusable policy (separate approval)' })).closest('section') as HTMLElement;
}

describe('capability gating (backend session drives controls)', () => {
  it('a read-only session sees evidence and impact but no decision controls', async () => {
    const { transport, commands } = makeServer({ capabilities: ['fixture.read'] });
    const { container } = renderApp(`/cases/${CASE}`, transport);
    await screen.findByRole('heading', { name: 'One-time resolution (this case only)' });
    await waitFor(() => expect(within(caseSection()).getByText(/does not include the/)).toBeInTheDocument());
    expect(within(caseSection()).getByText('case.resolve')).toBeInTheDocument();
    expect(screen.queryByRole('radio')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Review one-time decision/ })).not.toBeInTheDocument();
    const ps = await policySection();
    await waitFor(() => expect(within(ps).getByText(/needed to approve reusable/)).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /Review policy approval/ })).not.toBeInTheDocument();
    expect(commands()).toHaveLength(0);
    expect(await axeViolations(container)).toEqual([]);
  });

  it('case.resolve alone does not unlock policy approval', async () => {
    const { transport } = makeServer({ capabilities: ['fixture.read', 'case.resolve'] });
    renderApp(`/cases/${CASE}`, transport);
    expect(await screen.findByRole('button', { name: 'Review one-time decision' })).toBeInTheDocument();
    const ps = await policySection();
    await waitFor(() => expect(within(ps).getByText(/needed to approve reusable/)).toBeInTheDocument());
  });
});

describe('one-time case resolution', () => {
  it('shows two distinct decisions with explicit scope', async () => {
    const { transport } = makeServer({ capabilities: DECIDERS });
    const { container } = renderApp(`/cases/${CASE}`, transport);
    await screen.findByRole('button', { name: 'Review one-time decision' });
    expect(caseSection()).toHaveTextContent('It does not create or approve a reusable rule');
    expect(await within(await policySection()).findByRole('button', { name: 'Review policy approval' })).toBeDisabled();
    expect(await policySection()).toHaveTextContent('It does not resolve any open case');
    expect(await axeViolations(container)).toEqual([]);
  });

  it('review binds the exact version and evidence hash; nothing is sent until confirmed; envelope is correct', async () => {
    const user = userEvent.setup();
    const { transport, commands } = makeServer({ capabilities: DECIDERS });
    renderApp(`/cases/${CASE}`, transport);
    await chooseEmailRateAndReview(user);
    const review = screen.getByLabelText('Review one-time decision');
    expect(review).toHaveTextContent('Bound to case version3');
    expect(review).toHaveTextContent(`sha256:${'3c'.repeat(32)}`);
    expect(review).toHaveTextContent('This case only. No reusable policy is created or approved.');
    expect(commands()).toHaveLength(0);

    await user.click(screen.getByRole('button', { name: 'Confirm one-time resolution' }));
    expect(await screen.findByText(/Server confirmed/)).toHaveTextContent('Server confirmed: case is now RESOLVED (version 4).');
    const sent = commands();
    expect(sent).toHaveLength(1);
    const req = sent[0]!;
    expect(req.path).toBe(RESOLVE);
    expect(req.headers['idempotency-key']).toMatch(/^idem-/);
    expect(req.headers['content-type']).toBe('application/json');
    const body = JSON.parse(req.body!);
    expectSchema('CommandEnvelope', body);
    expectSchema('ResolveCasePayload', body.payload);
    expect(body).toEqual({
      command_id: expect.any(String),
      expected_version: 3,
      payload: {
        action: 'RESOLVE_CASE_ONE_TIME',
        decision: 'ACCEPT_OPTION',
        option_id: 'opt_email_rate',
        evidence_hash: `sha256:${'3c'.repeat(32)}`,
      },
    });
    // The case page now shows the server's record; the policy was NOT approved by this.
    expect(await screen.findByRole('heading', { name: 'Decision on record' })).toBeInTheDocument();
    expect(sent.some((r) => r.path === APPROVE)).toBe(false);
    expect(within(await policySection()).getByRole('button', { name: 'Review policy approval' })).toBeInTheDocument();
  });

  it('never shows success before the server confirms, and a double click sends one request', async () => {
    const user = userEvent.setup();
    const hold = deferred();
    const { transport, commands } = makeServer({
      capabilities: DECIDERS,
      intercept: (req, forward) => (req.method === 'POST' ? hold.gate.then(forward) : undefined),
    });
    renderApp(`/cases/${CASE}`, transport);
    const confirm = await chooseEmailRateAndReview(user);
    await user.dblClick(confirm);
    await user.click(confirm);
    expect(await screen.findByText(/waiting for the server to confirm/)).toBeInTheDocument();
    expect(screen.queryByText(/Server confirmed/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resolving…' })).toBeDisabled();
    expect(screen.getAllByText('OPEN').length).toBeGreaterThan(0); // record still shows the server state
    expect(commands()).toHaveLength(1);

    await act(async () => hold.release());
    expect(await screen.findByText(/Server confirmed/)).toBeInTheDocument();
    expect(commands()).toHaveLength(1);
  });

  it('409 refetches, shows the current version and requires a new review', async () => {
    const user = userEvent.setup();
    let conflicted = false;
    const { transport, commands } = makeServer({
      capabilities: DECIDERS,
      intercept: (req, forward) => {
        if (req.method === 'POST') {
          conflicted = true;
          return Promise.resolve(
            jsonResponse(409, { code: 'VERSION_CONFLICT', message: 'Changed.', request_id: 'r-409', retryable: false, current_version: 5 }),
          );
        }
        if (conflicted && req.path === `/api/v1/cases/${CASE}`) {
          // Someone else updated the case meanwhile: the server now serves version 5.
          return forward().then(async (res) => jsonResponse(200, { ...(await res.json()), version: 5 }));
        }
        return undefined;
      },
    });
    renderApp(`/cases/${CASE}`, transport);
    await chooseEmailRateAndReview(user);
    await user.click(screen.getByRole('button', { name: 'Confirm one-time resolution' }));
    expect(await screen.findByText(/changed while you were reviewing it \(now version 5\)/)).toBeInTheDocument();
    expect(screen.queryByText(/Server confirmed/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Confirm one-time resolution' })).not.toBeInTheDocument();
    expect(screen.getByText('version 5')).toBeInTheDocument();
    expect(commands()).toHaveLength(1);

    // A new review binds to the new version with a NEW idempotency key.
    await user.click(screen.getByRole('button', { name: 'Review one-time decision' }));
    expect(screen.getByLabelText('Review one-time decision')).toHaveTextContent('Bound to case version5');
  });

  it('422 shows field errors and does not claim success', async () => {
    const user = userEvent.setup();
    const { transport } = makeServer({
      capabilities: DECIDERS,
      intercept: (req) =>
        req.method === 'POST'
          ? Promise.resolve(
              jsonResponse(422, {
                code: 'EVIDENCE_CHANGED',
                message: 'The evidence changed.',
                request_id: 'r-422',
                retryable: false,
                field_errors: [{ field: 'payload.evidence_hash', message: 'does not match the current evidence' }],
              }),
            )
          : undefined,
    });
    renderApp(`/cases/${CASE}`, transport);
    await chooseEmailRateAndReview(user);
    await user.click(screen.getByRole('button', { name: 'Confirm one-time resolution' }));
    const alert = await screen.findByText(/The evidence changed\./);
    expect(alert.closest('[role="alert"]')).toHaveTextContent('payload.evidence_hash: does not match the current evidence');
    expect(screen.queryByText(/Server confirmed/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry safely' })).not.toBeInTheDocument();
  });

  it('403 from the server is shown as no permission (the server is authoritative)', async () => {
    const user = userEvent.setup();
    const { transport } = makeServer({
      capabilities: DECIDERS,
      intercept: (req) =>
        req.method === 'POST'
          ? Promise.resolve(jsonResponse(403, { code: 'FORBIDDEN', message: 'Capability revoked.', request_id: 'r-403', retryable: false }))
          : undefined,
    });
    renderApp(`/cases/${CASE}`, transport);
    await chooseEmailRateAndReview(user);
    await user.click(screen.getByRole('button', { name: 'Confirm one-time resolution' }));
    expect(await screen.findByText(/You do not have permission: Capability revoked\./)).toBeInTheDocument();
    expect(screen.queryByText(/Server confirmed/)).not.toBeInTheDocument();
  });

  it('a network failure can be retried safely with the same idempotency key and body', async () => {
    const user = userEvent.setup();
    let failures = 1;
    const { transport, commands } = makeServer({
      capabilities: DECIDERS,
      intercept: (req) => {
        if (req.method === 'POST' && failures > 0) {
          failures -= 1;
          return Promise.reject(new TypeError('Failed to fetch'));
        }
        return undefined;
      },
    });
    renderApp(`/cases/${CASE}`, transport);
    await chooseEmailRateAndReview(user);
    await user.click(screen.getByRole('button', { name: 'Confirm one-time resolution' }));
    expect(await screen.findByText('Not confirmed.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Retry safely' }));
    expect(await screen.findByText(/Server confirmed/)).toBeInTheDocument();
    const [a, b] = commands() as [RecordedRequest, RecordedRequest];
    expect(commands()).toHaveLength(2);
    expect(b.headers['idempotency-key']).toBe(a.headers['idempotency-key']);
    expect(b.body).toBe(a.body);
  });

  it('the chosen option survives a reload (URL) and the confirmed decision survives a refresh (server state)', async () => {
    const user = userEvent.setup();
    const { transport } = makeServer({ capabilities: DECIDERS });
    const first = renderApp(`/cases/${CASE}?decision=opt_contract_rate`, transport);
    expect(await screen.findByRole('radio', { name: /signed contract rate/ })).toBeChecked();
    await user.click(screen.getByRole('button', { name: 'Review one-time decision' }));
    await user.click(screen.getByRole('button', { name: 'Confirm one-time resolution' }));
    await screen.findByText(/Server confirmed/);
    first.unmount();

    renderApp(`/cases/${CASE}`, transport); // "refresh": new mount, same server
    expect(await screen.findByRole('heading', { name: 'Decision on record' })).toBeInTheDocument();
    expect(screen.getByText('Apply the signed contract rate (15%)')).toBeInTheDocument();
    expect(screen.getByText('test-actor')).toBeInTheDocument();
    expect(within(caseSection()).getByText(/No decision is pending/)).toBeInTheDocument();
  });
});

describe('reusable policy approval (separate command)', () => {
  it('approves exactly this DRAFT version within the shown scope via /policies/{id}/approve', async () => {
    const user = userEvent.setup();
    const { transport, commands } = makeServer({ capabilities: DECIDERS });
    renderApp(`/policies/${POLICY}`, transport);
    const region = await screen.findByRole('region', { name: 'Reusable policy (separate approval)' });
    const review = await within(region).findByRole('button', { name: 'Review policy approval' });
    expect(review).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: /I approve reuse of policy version 1 within exactly this scope/ }));
    await user.click(review);
    const box = screen.getByLabelText('Review policy approval');
    expect(box).toHaveTextContent('Bound to record revision1');
    expect(box).toHaveTextContent('Taco Libre only, Golden Coast 2026 festival nights');
    expect(commands()).toHaveLength(0);

    await user.click(screen.getByRole('button', { name: 'Confirm policy approval' }));
    expect(await screen.findByText(/Server confirmed: policy version is now/)).toHaveTextContent('ACTIVE (version 2)');
    const [req] = commands() as [RecordedRequest];
    expect(commands()).toHaveLength(1);
    expect(req.path).toBe(APPROVE);
    expect(req.headers['idempotency-key']).toMatch(/^idem-/);
    const body = JSON.parse(req.body!);
    expectSchema('CommandEnvelope', body);
    expectSchema('ApprovePolicyPayload', body.payload);
    expect(body.expected_version).toBe(1);
    expect(body.payload).toMatchObject({
      action: 'APPROVE_REUSABLE_POLICY',
      policy_id: 'illus_pol_taco_rate',
      policy_version: 1,
      evidence_hash: `sha256:${'3c'.repeat(32)}`,
      source_case_id: CASE,
      scope: {
        festival_id: 'fest_bf25e974c723d827',
        vendor_ids: ['ven_c309836b4f9601f2'],
        business_date_from: '2026-10-16',
        business_date_to: '2026-10-18',
      },
    });
    // Approving reuse did not resolve the case.
    expect(commands().some((r) => r.path === RESOLVE)).toBe(false);
    await waitFor(() => expect(screen.getAllByText('ACTIVE').length).toBeGreaterThan(0));
  });

  it('a policy changed during review is not approved blindly', async () => {
    const user = userEvent.setup();
    let bumped = false;
    const { transport, commands } = makeServer({
      capabilities: DECIDERS,
      intercept: (req, forward) => {
        if (req.method === 'POST') {
          bumped = true;
          return Promise.resolve(jsonResponse(409, { code: 'VERSION_CONFLICT', message: 'Changed.', request_id: 'r', retryable: false, current_version: 2 }));
        }
        if (bumped && req.path === `/api/v1/policies/${POLICY}`) {
          return forward().then(async (res) => jsonResponse(200, { ...(await res.json()), record_version: 2 }));
        }
        return undefined;
      },
    });
    renderApp(`/policies/${POLICY}`, transport);
    await user.click(await screen.findByRole('checkbox', { name: /I approve reuse/ }));
    await user.click(screen.getByRole('button', { name: 'Review policy approval' }));
    await user.click(screen.getByRole('button', { name: 'Confirm policy approval' }));
    expect(await screen.findByText(/changed while you were reviewing it \(now revision 2\)/)).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /I approve reuse/ })).not.toBeChecked();
    expect(screen.queryByText(/Server confirmed/)).not.toBeInTheDocument();
    expect(commands()).toHaveLength(1);
  });
});
