import { useState } from 'react';
import { useApp } from '../app/AppContext';
import { isApiError } from '../api/errors';

/**
 * Sign-in for the backend's JWT bearer scheme: POST /api/v1/auth/login
 * exchanges email and password for a token the client then sends as
 * Authorization. There is no cookie session and no hosted login page, so this
 * form is the only way in; VITE_ENCORE_AUTH_LOGIN_URL still takes precedence
 * when an environment fronts the API with its own identity provider.
 *
 * The password is held only in this component's state for the one request.
 */
export function SignIn() {
  const { signIn, config } = useApp();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (config.authLoginUrl) {
    return (
      <section className="panel" aria-labelledby="signin-heading">
        <h2 id="signin-heading" className="panel-title">Sign in required</h2>
        <p>This environment signs in through its own identity provider.</p>
        <a className="button" href={config.authLoginUrl}>
          Sign in
        </a>
      </section>
    );
  }

  if (!signIn) {
    return (
      <section className="panel" aria-labelledby="signin-heading">
        <h2 id="signin-heading" className="panel-title">Sign in required</h2>
        <p>No sign-in is configured for this environment.</p>
      </section>
    );
  }

  return (
    <section className="panel" aria-labelledby="signin-heading">
      <h2 id="signin-heading" className="panel-title">Sign in</h2>
      <p className="muted">Encore signs you in against the API; your password is never stored in the browser.</p>
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          if (busy) return;
          setBusy(true);
          setError(null);
          signIn(email, password)
            .catch((cause: unknown) => {
              setError(isApiError(cause) ? cause.message : 'Sign-in failed.');
            })
            .finally(() => {
              setBusy(false);
              setPassword('');
            });
        }}
      >
        <div className="field">
          <label htmlFor="signin-email">Email</label>
          <input
            id="signin-email"
            name="email"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="signin-password">Password</label>
          <input
            id="signin-password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        {error ? (
          <p className="state-body" role="alert">
            {error}
          </p>
        ) : null}
        <button type="submit" className="button button-primary" disabled={busy}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </section>
  );
}
