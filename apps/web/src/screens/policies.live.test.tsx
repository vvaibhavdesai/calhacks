/**
 * The policy screen's approval control, against the LIVE response shape.
 *
 * The payload is src/test/live-capture/policy-detail.json: the demo's own
 * policy row (`6dff5e19…`, Taco Libre's commission rule), captured from the
 * running API. Its two newest fields -- `evidence_hash` and
 * `required_capability` -- were taken from the same server serving that same
 * row, because the approval binds the hash and the control is hidden without
 * the capability name. The draft contract's `record_version` is NOT in it:
 * this backend has no revision counter on a policy row, so the binding falls
 * back to the policy version, and that fallback is exercised here rather than
 * only in the fixtures.
 *
 * The three states a reader can actually meet are each rendered: the DRAFT
 * that can be approved, the SUSPENDED one the demo currently holds (a
 * challenge paused it), and the ACTIVE one already in force. The two that
 * cannot be approved must SAY so -- an empty panel would leave a reader
 * wondering whether the product forgot the control or refused it.
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { ApiClient, type Transport } from '../api/client';
import type { AppConfig } from '../config/env';
import { App } from '../app/App';
import authMe from '../test/live-capture/auth-me.json';
import events from '../test/live-capture/events.json';
import festival from '../test/live-capture/festival.json';
import policies from '../test/live-capture/policies.json';
import policyDetail from '../test/live-capture/policy-detail.json';
import vendors from '../test/live-capture/vendors.json';

const liveConfig: AppConfig = { dataMode: 'live', apiOrigin: '', authLoginUrl: null };
const POLICY_ID = (policyDetail as { id: string }).id;

type Row = Record<string, unknown>;

function renderLive(detail: Row, caps: string[] = (authMe as { capabilities: string[] }).capabilities) {
  const sent: { url: string; body: string | null; key: string | null }[] = [];
  const transport: Transport = async (url: string, init?: RequestInit) => {
    const reply = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
    if (init?.method === 'POST') {
      const headers = (init.headers ?? {}) as Record<string, string>;
      sent.push({ url, body: typeof init.body === 'string' ? init.body : null, key: headers['Idempotency-Key'] ?? null });
      return reply({ command_id: 'c', aggregate_id: POLICY_ID, version: 1, status: 'ACTIVE' });
    }
    if (/\/auth\/me/.test(url)) return reply({ ...authMe, capabilities: caps });
    if (new RegExp(`/policies/${POLICY_ID}`).test(url)) return reply(detail);
    if (/\/policies(\?|$)/.test(url)) return reply(policies);
    if (/\/challenges/.test(url)) return reply({ items: [], next_cursor: null });
    if (/\/vendors(\?|$)/.test(url)) return reply(vendors);
    if (/\/festival(\?|$)/.test(url)) return reply(festival);
    if (/\/events(\?|$)/.test(url)) return reply(events);
    return reply({ code: 'not_served', path: url }, 599);
  };
  const client = new ApiClient({ transport });
  render(<App services={{ config: liveConfig, client, fixtureId: null }} initialEntries={[`/policies/${POLICY_ID}`]} />);
  return sent;
}

async function approvalPanel() {
  return (await screen.findByRole('region', { name: 'Reusable policy (separate approval)' })) as HTMLElement;
}

describe('policy approval against the live response shape', () => {
  it('offers the approval on a version waiting for a human, bound to what the server sent', async () => {
    const user = userEvent.setup();
    const sent = renderLive({ ...policyDetail, status: 'DRAFT' });
    const panel = await approvalPanel();

    const review = await within(panel).findByRole('button', { name: 'Review policy approval' });
    expect(review).toBeDisabled(); // consent first
    await user.click(screen.getByRole('checkbox', { name: /I approve reuse of policy version 1 within exactly this scope/ }));
    await user.click(review);

    const box = screen.getByLabelText('Review policy approval');
    expect(box).toHaveTextContent(`POST /policies/${POLICY_ID}/approve`);
    // No record_version on this server: the policy version is the binding.
    expect(box).toHaveTextContent('Bound to record revision1');
    expect(box).toHaveTextContent((policyDetail as { evidence_hash: string }).evidence_hash);
    expect(sent).toHaveLength(0); // nothing is sent from the review step

    await user.click(screen.getByRole('button', { name: 'Confirm policy approval' }));
    await waitFor(() => expect(sent).toHaveLength(1));
    const req = sent[0]!;
    expect(req.url).toContain(`/api/v1/policies/${POLICY_ID}/approve`);
    expect(req.key).toMatch(/^idem-/);
    const envelope = JSON.parse(req.body!);
    expect(envelope.expected_version).toBe(1);
    expect(envelope.payload).toEqual({
      action: 'APPROVE_REUSABLE_POLICY',
      policy_id: (policyDetail as { policy_id: string }).policy_id,
      policy_version: 1,
      evidence_hash: (policyDetail as { evidence_hash: string }).evidence_hash,
      // The scope is echoed back EXACTLY as served, `reason_code` and all:
      // the server refuses an approval whose scope differs by any key.
      scope: (policyDetail as { scope: Record<string, unknown> }).scope,
      source_case_id: (policyDetail as { origin_case_id: string }).origin_case_id,
    });
    expect(await screen.findByText(/Server confirmed/)).toHaveTextContent('ACTIVE');
  });

  it('says why a SUSPENDED version cannot be approved, and offers no button', async () => {
    renderLive({ ...policyDetail, status: 'SUSPENDED' });
    const panel = await approvalPanel();
    expect(panel).toHaveTextContent('only a DRAFT version can be approved for reuse');
    expect(panel).toHaveTextContent('A challenge paused this rule');
    expect(within(panel).queryByRole('button')).toBeNull();
    expect(within(panel).queryByRole('checkbox')).toBeNull();
  });

  it('says an ACTIVE version is already in force -- the demo policy, unchanged', async () => {
    renderLive(policyDetail as Row);
    const panel = await approvalPanel();
    expect(panel).toHaveTextContent('It is already approved and in force.');
    expect(within(panel).queryByRole('checkbox')).toBeNull();
  });

  it('hides the control from a session without the capability the server names', async () => {
    renderLive({ ...policyDetail, status: 'DRAFT' }, ['resolve_case']);
    const panel = await approvalPanel();
    expect(panel).toHaveTextContent('approve_policy');
    expect(panel).toHaveTextContent('does not include');
    expect(within(panel).queryByRole('checkbox')).toBeNull();
  });
});
