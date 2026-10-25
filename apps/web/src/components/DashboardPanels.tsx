import type { Dashboard } from '../api/types';
import { ariaUsd, centsScaleRatio, formatBasisPoints, formatUsd, maxAbsCents } from '../lib/money';
import { BusinessDate, Money } from './Formatted';

/**
 * The landing screen's festival-level panels, all from GET /dashboard.
 *
 * Chart choices, deliberately narrow: every plot here encodes ONE measure,
 * so each uses a single hue and carries identity in the text beside the mark
 * rather than in the colour. That removes the need for a categorical palette
 * (and the colourblind-separation problem that comes with one) in a UI that
 * is light-only and number-first. Exact figures always sit in a table or a
 * direct label next to the bar, so no reading depends on judging a length.
 *
 * Money is never arithmetic here: bar geometry comes from
 * `centsScaleRatio`/`maxAbsCents` and every figure is rendered by <Money> or
 * `formatUsd`, which is the rail lib/money.ts exists to enforce.
 */

/** A hero figure with its own caption. No plot, so no hover layer. */
function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: 'plain' | 'warn';
}) {
  return (
    <div className={`stat${tone === 'warn' ? ' is-warn' : ''}`}>
      <dt>{label}</dt>
      <dd>
        <span className="stat-value">{value}</span>
        {sub ? <span className="stat-sub">{sub}</span> : null}
      </dd>
    </div>
  );
}

export function DashboardStats({ data }: { data: Dashboard }) {
  const t = data.totals;
  const a = data.autonomy;
  const resolved = a.auto_resolved + a.human_resolved;
  return (
    <dl className="stat-row">
      <Stat
        label="Eligible sales settled"
        value={formatUsd(t.eligible_sales_cents)}
        sub={`${t.settlements} settlements · ${t.vendors} vendors`}
      />
      <Stat
        label="Commission assessed"
        value={formatUsd(t.commission_cents)}
        sub={
          t.effective_rate_bps === null
            ? undefined
            : `effective ${formatBasisPoints(t.effective_rate_bps)}`
        }
      />
      <Stat
        label="Vendor payouts"
        value={formatUsd(t.payout_cents)}
        sub="simulated rail - no real transfer"
      />
      <Stat
        label="Resolved without a human"
        value={a.autonomy_rate_bps === null ? '--' : formatBasisPoints(a.autonomy_rate_bps)}
        sub={`${a.auto_resolved} of ${resolved} resolved by policy · ${a.open_cases} awaiting a decision`}
        tone={a.open_cases > 0 ? 'warn' : 'plain'}
      />
    </dl>
  );
}

/** One horizontal magnitude bar: a label, a single-hue fill, its exact value. */
function Bar({
  label,
  note,
  cents,
  scale,
  title,
  trailing,
}: {
  label: React.ReactNode;
  note?: string;
  cents: number;
  scale: string;
  title: string;
  trailing?: React.ReactNode;
}) {
  const ratio = centsScaleRatio(cents, scale);
  return (
    <div className="hbar-row">
      <span className="hbar-label">
        {label}
        {note ? <span className="hbar-note">{note}</span> : null}
      </span>
      <span className="hbar-track">
        <span className="hbar-fill" style={{ width: `${Math.max(1, ratio * 100)}%` }} title={title} />
      </span>
      <span className="hbar-value">
        <Money cents={cents} />
        {trailing}
      </span>
    </div>
  );
}

/**
 * Eligible sales per festival night. One series, so no legend: the heading
 * names the measure and every bar carries its own value.
 */
export function NightsChart({ data }: { data: Dashboard }) {
  const nights = data.nights;
  if (nights.length === 0) return <p className="muted">No settled night yet.</p>;
  const scale = maxAbsCents(nights.map((n) => n.eligible_sales_cents));
  const summary = nights.map((n) => `${n.business_date}: ${ariaUsd(n.eligible_sales_cents)}`).join('; ');
  return (
    <figure className="chart">
      <div className="hbars" role="img" aria-label={`Eligible sales by night. ${summary}`}>
        {nights.map((n) => (
          <Bar
            key={n.business_date}
            label={<BusinessDate value={n.business_date} />}
            cents={n.eligible_sales_cents}
            scale={scale}
            title={`${n.business_date}: ${formatUsd(n.eligible_sales_cents)} eligible`}
          />
        ))}
      </div>
      <div className="table-scroll" role="region" aria-label="Per-night figures" tabIndex={0}>
        <table className="data-table">
          <caption>Per night (exact server values)</caption>
          <thead>
            <tr>
              <th scope="col">Night</th>
              <th scope="col" className="is-numeric">Vendors</th>
              <th scope="col" className="is-numeric">Eligible sales</th>
              <th scope="col" className="is-numeric">Commission</th>
              <th scope="col" className="is-numeric">Payouts</th>
            </tr>
          </thead>
          <tbody>
            {nights.map((n) => (
              <tr key={n.business_date}>
                <th scope="row"><BusinessDate value={n.business_date} /></th>
                <td className="is-numeric">{n.vendors}</td>
                <td className="is-numeric"><Money cents={n.eligible_sales_cents} /></td>
                <td className="is-numeric"><Money cents={n.commission_cents} /></td>
                <td className="is-numeric"><Money cents={n.payout_cents} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </figure>
  );
}

/**
 * Largest vendors by eligible sales, with the commission rate actually
 * applied. A rate other than the festival default is the interesting case
 * (it came from that vendor's own signed contract), so it is called out in
 * text rather than by colour alone.
 */
export function TopVendors({ data }: { data: Dashboard }) {
  const rows = data.top_vendors;
  if (rows.length === 0) return <p className="muted">No settled vendor yet.</p>;
  const scale = maxAbsCents(rows.map((v) => v.eligible_sales_cents));
  const summary = rows
    .map((v) => `${v.name ?? v.vendor_id}: ${ariaUsd(v.eligible_sales_cents)}`)
    .join('; ');
  return (
    <figure className="chart">
      <div className="hbars" role="img" aria-label={`Largest vendors by eligible sales. ${summary}`}>
        {rows.map((v) => (
          <Bar
            key={v.vendor_id}
            label={v.name ?? v.vendor_id}
            cents={v.eligible_sales_cents}
            scale={scale}
            title={`${v.name ?? v.vendor_id}: ${formatUsd(v.eligible_sales_cents)} eligible over ${v.nights} night(s)`}
            trailing={
              v.effective_rate_bps === null ? null : (
                <span className="muted"> · {formatBasisPoints(v.effective_rate_bps)}</span>
              )
            }
          />
        ))}
      </div>
      <figcaption className="muted">
        Rate shown is commission assessed over eligible sales: above the headline rate where a vendor hit the
        festival minimum, and different from it where the vendor&apos;s own signed contract states another rate.
      </figcaption>
    </figure>
  );
}

/**
 * Where the festival's cash actually sits. These are the same figures
 * GET /cash serves; `custody` is passed through untouched, and a key the
 * server did not send is omitted rather than shown as zero.
 */
export function CustodySplit({ data }: { data: Dashboard }) {
  const c = data.custody as Record<string, unknown>;
  const num = (v: unknown): number | null => (typeof v === 'number' ? v : null);
  const office = c.cash_office as Record<string, unknown> | undefined;
  const susp = c.suspense as Record<string, unknown> | undefined;
  const rows = [
    { label: 'In the bank', cents: num(c.bank_cents) },
    {
      label: 'Protected (not the organizer’s)',
      cents: num(c.protected_cents),
      note: 'Vendor funds and tax held in custody',
    },
    { label: 'Vendor funds payable', cents: num(c.vendor_funds_payable_cents) },
    { label: 'Freely available', cents: num(c.freely_available_cents) },
    { label: 'Cash office (not yet banked)', cents: num(office?.cash_office_cents) },
    {
      label: 'Suspense (awaiting a human)',
      cents: num(susp?.suspense_cents),
      note: 'Unexplained bank activity; never income',
    },
    {
      label: 'Obligations due in the horizon',
      cents: num(c.obligations_due_within_horizon_cents),
      note: `Next ${String(c.forecast_horizon_days ?? '')} days`,
    },
  ].filter((r): r is { label: string; cents: number; note?: string } => r.cents !== null);
  if (rows.length === 0) return <p className="muted">The server returned no cash position.</p>;
  const scale = maxAbsCents(rows.map((r) => r.cents));
  return (
    <div className="hbars" role="img" aria-label={`Cash by custody. ${rows.map((r) => `${r.label}: ${ariaUsd(r.cents)}`).join('; ')}`}>
      {rows.map((r) => (
        <Bar
          key={r.label}
          label={r.label}
          note={r.note}
          cents={r.cents}
          scale={scale}
          title={`${r.label}: ${formatUsd(r.cents)}`}
        />
      ))}
    </div>
  );
}

/** Exception load by reason, so the open backlog is never hidden by a rate. */
export function AutonomyByReason({ data }: { data: Dashboard }) {
  const rows = data.autonomy.by_reason;
  if (rows.length === 0) return <p className="muted">No exception has been raised.</p>;
  return (
    <div className="table-scroll" role="region" aria-label="Exceptions by reason" tabIndex={0}>
      <table className="data-table">
        <caption>Exceptions by reason</caption>
        <thead>
          <tr>
            <th scope="col">Reason</th>
            <th scope="col" className="is-numeric">Awaiting a decision</th>
            <th scope="col" className="is-numeric">Resolved by policy</th>
            <th scope="col" className="is-numeric">Resolved by a human</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.reason_code}>
              <th scope="row">{r.reason_code.replace(/_/g, ' ')}</th>
              <td className="is-numeric">{r.open_cases}</td>
              <td className="is-numeric">{r.auto_resolved}</td>
              <td className="is-numeric">{r.human_resolved}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
