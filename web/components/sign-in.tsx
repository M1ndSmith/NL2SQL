"use client"

import { useState, type FormEvent } from "react"

type SignInProps = {
  busy: boolean
  notice: string
  onSubmit: (userId: string, password: string) => Promise<void>
}

export function SignIn({ busy, notice, onSubmit }: SignInProps) {
  const [userId, setUserId] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState("")

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError("")
    try {
      await onSubmit(userId.trim(), password)
    } catch (err) {
      setError(err instanceof Error ? err.message : "authentication failed")
    }
  }

  const message = error || notice

  return (
    <div className="sign-in">
      <form onSubmit={submit}>
        <h1>Sign in</h1>
        <label>
          User
          <input
            value={userId}
            autoComplete="username"
            onChange={(event) => setUserId(event.target.value)}
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            autoComplete="current-password"
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        <button className="btn-primary" type="submit" disabled={busy || !userId.trim() || !password}>
          Sign in
        </button>
        {message ? <p className="form-error">{message}</p> : null}
      </form>
    </div>
  )
}
