import { useApp } from '../app/AppContext';

/**
 * Shown next to output data in fixture mode only: the records are
 * hand-authored ILLUSTRATIVE samples, not backend calculations.
 */
export function IllustrativeNote() {
  const { fixtureId } = useApp();
  if (!fixtureId) return null;
  return (
    <p className="illustrative-note" role="note">
      <strong>Illustrative sample output.</strong> Hand-authored for development only. These figures are not calculated by a
      backend and are not an expected answer.
    </p>
  );
}
