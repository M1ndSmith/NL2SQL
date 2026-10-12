export const tokenKey = "nl2sql.token"

export class ApiError extends Error {
  code: string

  constructor(code: string, message: string) {
    super(message)
    this.code = code
  }
}

export function readToken(): string | null {
  if (typeof sessionStorage === "undefined") return null
  return sessionStorage.getItem(tokenKey)
}

export function writeToken(token: string) {
  sessionStorage.setItem(tokenKey, token)
}

export function clearToken() {
  sessionStorage.removeItem(tokenKey)
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json")
  const token = readToken()
  if (token) headers.set("Authorization", `Bearer ${token}`)
  let response: Response
  try {
    response = await fetch(path, { ...init, headers })
  } catch {
    throw new ApiError("unavailable", "The API is unavailable.")
  }
  if (response.status === 204) return undefined as T
  const text = await response.text()
  let body: { error?: { code?: string; message?: string } } = {}
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      throw new ApiError("request_failed", "Request failed.")
    }
  }
  if (!response.ok) {
    const error = body.error ?? {}
    throw new ApiError(error.code || "request_failed", error.message || "Request failed.")
  }
  return body as T
}
