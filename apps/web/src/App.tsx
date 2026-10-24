import { useState } from "react";

import { ApiError, login, me, type MeResponse } from "./apiClient";

// Placeholder authenticated-shell page only. Real screens (sidebar, tables,
// evidence drawers, flight-recorder timeline) are a later task (B03+). This
// page exists to prove, end to end, that: the frontend can reach the API,
// capability-enforced authentication works, and no secret is ever present
// in frontend code (only VITE_API_BASE_URL, a public value).
export function App() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [profile, setProfile] = useState<MeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const { access_token } = await login(email, password);
      setAccessToken(access_token);
      const profileResponse = await me(access_token);
      setProfile(profileResponse);
    } catch (err) {
      setProfile(null);
      setAccessToken(null);
      setError(err instanceof ApiError ? err.message : "Unable to sign in");
    } finally {
      setLoading(false);
    }
  }

  function handleSignOut() {
    setAccessToken(null);
    setProfile(null);
  }

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", maxWidth: 480, margin: "4rem auto" }}>
      <h1>Encore</h1>
      <p style={{ color: "#555" }}>Authenticated-shell placeholder (A01 scaffold).</p>

      {!accessToken || !profile ? (
        <form onSubmit={handleSubmit}>
          <div style={{ marginBottom: 12 }}>
            <label htmlFor="email">Email</label>
            <br />
            <input
              id="email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div style={{ marginBottom: 12 }}>
            <label htmlFor="password">Password</label>
            <br />
            <input
              id="password"
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <button type="submit" disabled={loading}>
            {loading ? "Signing in..." : "Sign in"}
          </button>
          {error && <p style={{ color: "crimson" }}>{error}</p>}
        </form>
      ) : (
        <div>
          <p>
            Signed in as <strong>{profile.email}</strong>
          </p>
          <p>
            Capabilities:{" "}
            {profile.capabilities.length > 0 ? profile.capabilities.join(", ") : "none"}
          </p>
          <button type="button" onClick={handleSignOut}>
            Sign out
          </button>
        </div>
      )}
    </main>
  );
}
