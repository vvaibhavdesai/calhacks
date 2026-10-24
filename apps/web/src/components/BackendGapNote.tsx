import { useApp } from '../app/AppContext';
import { backendGap, isLive } from '../api/operations';
import type { OperationId } from '../api/types';

/**
 * Marks data this screen CANNOT get from the real backend yet.
 *
 * src/api/operations.ts classifies every operation against the backend's own
 * OpenAPI document. For an operation that has no usable route, the screen is
 * served by the dev/test fixture, and that has to be visible: an operator must
 * never read a number here as if the server had produced it. In live mode the
 * same note says the data is unavailable, because live mode never substitutes
 * fixture data.
 */
export function BackendGapNote({ ops }: { ops: readonly OperationId[] }) {
  const { fixtureId } = useApp();
  const gaps = ops.filter((op) => !isLive(op));
  if (gaps.length === 0) return null;
  return (
    <div className="illustrative-note" role="note">
      <p>
        <strong>{fixtureId ? 'Not backed by the API.' : 'Unavailable from the API.'}</strong>{' '}
        {fixtureId
          ? 'This view is served by the dev/test fixture because the backend has no usable route for it yet:'
          : 'The backend has no usable route for this view yet:'}
      </p>
      <ul className="gap-list">
        {gaps.map((op) => (
          <li key={op}>
            <code className="code">{op}</code> — {backendGap(op)}
          </li>
        ))}
      </ul>
    </div>
  );
}
