import { Link, useRouteError } from 'react-router-dom';

/**
 * What a screen shows when its render throws.
 *
 * Without this, React Router's default boundary replaces the entire
 * application with a stack trace, so one screen reading a field the server
 * does not send looks like a total outage. The shell, the navigation and every
 * other screen stay usable, and the message says plainly that the failure is
 * in this screen rather than in the data.
 */
export function RouteError() {
  const error = useRouteError();
  const message =
    error instanceof Error
      ? error.message
      : typeof error === 'string'
        ? error
        : 'Unknown error';
  return (
    <section className="panel" aria-labelledby="route-error-heading">
      <h2 id="route-error-heading" className="panel-title">
        This screen could not be displayed
      </h2>
      <p>
        Something on this screen failed to render. The rest of the application is unaffected — pick
        another screen from the navigation, or <Link to="/">return to the overview</Link>.
      </p>
      <p className="muted">{message}</p>
      {error instanceof Error && error.stack ? (
        <details>
          <summary>Technical details</summary>
          <pre className="route-error-stack">{error.stack}</pre>
        </details>
      ) : null}
    </section>
  );
}
