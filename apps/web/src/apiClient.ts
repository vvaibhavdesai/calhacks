// Minimal placeholder API client. A real generated client lands once C03 is
// frozen and packages/contracts/scripts/generate_ts_types.py is implemented
// (see that file's docstring). This hand-written client exists only so the
// A01 authenticated-shell placeholder page has something real to call --
// it must not grow business logic ahead of the generated client.

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export interface LoginResponse {
  access_token: string;
  token_type: string;
}

export interface MeResponse {
  id: string;
  email: string;
  display_name: string | null;
  capabilities: string[];
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ?? detail;
    } catch {
      // response had no JSON body; fall back to statusText
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export function login(email: string, password: string): Promise<LoginResponse> {
  return request<LoginResponse>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function me(accessToken: string): Promise<MeResponse> {
  return request<MeResponse>("/api/v1/auth/me", {
    headers: { Authorization: `Bearer ${accessToken}` },
  });
}
