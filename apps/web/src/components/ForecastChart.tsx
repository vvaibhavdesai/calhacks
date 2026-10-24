import type { CashForecastPoint } from '../api/types';
import { centsScaleRatio, centsSign, formatUsd, maxAbsCents } from '../lib/money';
import { formatBusinessDate } from '../lib/time';
import { BusinessDate, Money } from './Formatted';

const W = 720;
const H = 240;
const TOP = 16;
const BOTTOM = 196;
const LEFT = 8;
const RIGHT = 8;

/**
 * Projected available cash per day (single series, server values only).
 * Accessibility: the SVG is a labelled image with a text summary, every bar
 * has a native tooltip, shortfall days carry a text label (never color alone),
 * and the exact figures are always available in the table below it.
 */
export function ForecastChart({ points, horizonDays }: { points: CashForecastPoint[]; horizonDays: number }) {
  if (points.length === 0) return <p className="muted">The server returned no forecast points.</p>;

  const values = points.map((p) => p.projected_available_cents);
  const scale = maxAbsCents(values);
  // Split the plot between the positive and negative extents (display geometry only),
  // keeping room below the zero line for the "Shortfall" labels.
  const posMax = maxAbsCents(values.filter((v) => centsSign(v) > 0));
  const negMax = maxAbsCents(values.filter((v) => centsSign(v) < 0));
  const posShare = centsScaleRatio(posMax, scale);
  const negShare = centsScaleRatio(negMax, scale);
  const plot = BOTTOM - TOP;
  const negZone = negShare > 0 ? Math.max(0.2, negShare / (posShare + negShare)) : 0;
  const zeroY = TOP + plot * (1 - negZone);
  const posLen = (zeroY - TOP) / (posShare || 1);
  const negLen = negShare > 0 ? (plot * negZone - 18) / negShare : 0;
  const slot = (W - LEFT - RIGHT) / points.length;
  const barW = Math.max(8, Math.min(56, slot - 12));
  const shortfalls = points.filter((p) => p.shortfall);
  const summary =
    `Projected available cash for ${points.length} days (horizon ${horizonDays} days). ` +
    (shortfalls.length
      ? `Shortfall on ${shortfalls.map((p) => `${formatBusinessDate(p.date)} (${formatUsd(p.projected_available_cents)})`).join(', ')}.`
      : 'No shortfall projected.');

  return (
    <figure className="chart">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`Projected available cash by day. ${summary}`}
        className="chart-svg"
        preserveAspectRatio="xMidYMid meet"
      >
        <title>Projected available cash by day</title>
        <desc>{summary}</desc>
        <line x1={LEFT} x2={W - RIGHT} y1={zeroY} y2={zeroY} className="chart-baseline" />
        {points.map((p, i) => {
          const sign = centsSign(p.projected_available_cents);
          const len = Math.max(sign === 0 ? 0 : 2, centsScaleRatio(p.projected_available_cents, scale) * (sign < 0 ? negLen : posLen));
          const x = LEFT + i * slot + (slot - barW) / 2;
          const y = sign < 0 ? zeroY : zeroY - len;
          const label = `${formatBusinessDate(p.date)}: ${formatUsd(p.projected_available_cents)}${p.shortfall ? ' (shortfall)' : ''}`;
          return (
            <g key={p.date}>
              <rect x={x} y={y} width={barW} height={len} rx={3} className={p.shortfall ? 'chart-bar is-shortfall' : 'chart-bar'}>
                <title>{label}</title>
              </rect>
              {p.shortfall ? (
                <text x={x + barW / 2} y={y + len + 14} textAnchor="middle" className="chart-label is-shortfall">
                  Shortfall
                </text>
              ) : null}
              <text x={x + barW / 2} y={H - 14} textAnchor="middle" className="chart-axis">
                {p.date.slice(5)}
              </text>
            </g>
          );
        })}
      </svg>
      <figcaption className="muted">
        Bars show the server&apos;s projected available cash at the end of each day; the zero line is marked. Exact figures are
        in the table below.
      </figcaption>
      <div className="table-scroll" role="region" aria-label="Forecast figures" tabIndex={0}>
        <table className="data-table">
          <caption>Forecast figures (exact server values)</caption>
          <thead>
            <tr>
              <th scope="col">Date</th>
              <th scope="col" className="is-numeric">
                Opening
              </th>
              <th scope="col" className="is-numeric">
                Inflows
              </th>
              <th scope="col" className="is-numeric">
                Outflows
              </th>
              <th scope="col" className="is-numeric">
                Projected available
              </th>
              <th scope="col">Shortfall</th>
            </tr>
          </thead>
          <tbody>
            {points.map((p) => (
              <tr key={p.date}>
                <th scope="row">
                  <BusinessDate value={p.date} />
                </th>
                <td className="is-numeric">
                  <Money cents={p.opening_cents} />
                </td>
                <td className="is-numeric">
                  <Money cents={p.inflows_cents} />
                </td>
                <td className="is-numeric">
                  <Money cents={p.outflows_cents} />
                </td>
                <td className="is-numeric">
                  <Money cents={p.projected_available_cents} />
                </td>
                <td>{p.shortfall ? <span className="badge badge-bad">Shortfall</span> : <span className="muted">No</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </figure>
  );
}
