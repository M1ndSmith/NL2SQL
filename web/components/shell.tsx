"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react"
import { HomeIcon, Mark, PanelIcon, PlayIcon, RunsIcon } from "@/components/icons"
import { SignIn } from "@/components/sign-in"
import { ApiError, api, clearToken, readToken, writeToken } from "@/lib/api"
import { mapRun, type ApiRun, type AuditRun } from "@/lib/runs"

export type ModelChoice = { provider: string; model: string; start: boolean }
export type Spend = { spent: number; cap: number }

type Session = { userId: string; role: string }

type ConsoleApi = {
  session: Session | null
  models: ModelChoice[]
  spend: Spend | null
  rows: AuditRun[]
  reload: () => Promise<void>
}

const ConsoleContext = createContext<ConsoleApi | null>(null)

export function useConsole(): ConsoleApi {
  const value = useContext(ConsoleContext)
  if (!value) throw new Error("useConsole must be used inside Shell")
  return value
}

const links = [
  { href: "/", label: "Home", icon: HomeIcon },
  { href: "/playground", label: "Playground", icon: PlayIcon },
  { href: "/runs", label: "Runs", icon: RunsIcon },
]

function initials(userId: string) {
  return userId.slice(0, 2).toUpperCase()
}

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname()
  const [collapsed, setCollapsed] = useState(false)
  const [ready, setReady] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState("")
  const [session, setSession] = useState<Session | null>(null)
  const [models, setModels] = useState<ModelChoice[]>([])
  const [spend, setSpend] = useState<Spend | null>(null)
  const [rows, setRows] = useState<AuditRun[]>([])

  const loadAccount = useCallback(async () => {
    const me = await api<{ user_id: string; role: string }>("/v1/me")
    const [modelsBody, spendBody, runsBody] = await Promise.all([
      api<{ models: ModelChoice[] }>("/v1/models").catch((err: unknown) => {
        if (err instanceof ApiError && err.code === "authentication_failed") throw err
        return { models: [] as ModelChoice[] }
      }),
      api<Spend>("/v1/spend").catch((err: unknown) => {
        if (err instanceof ApiError && err.code === "authentication_failed") throw err
        return null
      }),
      api<{ runs: ApiRun[] }>("/v1/runs").catch((err: unknown) => {
        if (err instanceof ApiError && err.code === "authentication_failed") throw err
        return null
      }),
    ])
    setSession({ userId: me.user_id, role: me.role })
    setModels(modelsBody.models)
    setSpend(spendBody)
    if (runsBody) setRows(runsBody.runs.map(mapRun))
  }, [])

  const reload = useCallback(async () => {
    const [spendBody, runsBody] = await Promise.all([
      api<Spend>("/v1/spend").catch(() => null),
      api<{ runs: ApiRun[] }>("/v1/runs").catch(() => null),
    ])
    setSpend(spendBody)
    if (runsBody) setRows(runsBody.runs.map(mapRun))
  }, [])

  useEffect(() => {
    let cancelled = false
    async function boot() {
      if (!readToken()) {
        if (!cancelled) setReady(true)
        return
      }
      try {
        await loadAccount()
      } catch (err) {
        if (err instanceof ApiError && err.code === "authentication_failed") clearToken()
        else if (!cancelled) setNotice(err instanceof Error ? err.message : "The API is unavailable.")
      } finally {
        if (!cancelled) setReady(true)
      }
    }
    boot()
    return () => {
      cancelled = true
    }
  }, [loadAccount])

  async function signIn(userId: string, password: string) {
    setBusy(true)
    setNotice("")
    try {
      const body = await api<{ token: string }>("/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ user_id: userId, password }),
      })
      writeToken(body.token)
      await loadAccount()
    } catch (err) {
      clearToken()
      setSession(null)
      throw err
    } finally {
      setBusy(false)
    }
  }

  async function signOut() {
    try {
      await api("/v1/auth/logout", { method: "POST" })
    } catch {
      // The token is cleared either way.
    }
    clearToken()
    setSession(null)
    setModels([])
    setSpend(null)
    setRows([])
    setNotice("")
  }

  const apiValue = useMemo(
    () => ({ session, models, spend, rows, reload }),
    [session, models, spend, rows, reload],
  )

  return (
    <ConsoleContext.Provider value={apiValue}>
      <div className="shell">
        <aside className={collapsed ? "rail collapsed" : "rail"}>
          <div className="rail-top">
            <Mark />
            <span className="word">NL2SQL</span>
            <button
              type="button"
              className="toggle"
              aria-label="Toggle sidebar"
              aria-expanded={!collapsed}
              onClick={() => setCollapsed((open) => !open)}
            >
              <PanelIcon />
            </button>
          </div>
          {session ? (
            <nav className="nav" aria-label="Console">
              {links.map((link) => {
                const active = pathname === link.href
                const Icon = link.icon
                return (
                  <Link
                    key={link.href}
                    href={link.href}
                    className={active ? "active" : undefined}
                    aria-current={active ? "page" : undefined}
                  >
                    <Icon />
                    <span className="nav-label">{link.label}</span>
                  </Link>
                )
              })}
            </nav>
          ) : null}
          {session ? (
            <div className="rail-bottom">
              <section className="role-card" aria-label="Role">
                <div className="lbl">Role</div>
                <div className="val">{session.role}</div>
                {spend ? (
                  <>
                    <div className="lbl today">Today</div>
                    <div className="spend mono">
                      {spend.spent.toFixed(2)} / {spend.cap.toFixed(2)}
                    </div>
                  </>
                ) : null}
              </section>
              <button type="button" className="user" aria-label="Sign out" onClick={signOut}>
                <span className="avatar" aria-hidden="true">
                  {initials(session.userId)}
                </span>
                <span className="user-meta">
                  <span className="name">{session.userId}</span>
                  <span className="sub">{session.role}</span>
                </span>
              </button>
            </div>
          ) : null}
        </aside>
        <div className="canvas-frame">
          <main className="canvas">
            {!ready ? null : session ? children : <SignIn busy={busy} notice={notice} onSubmit={signIn} />}
          </main>
        </div>
      </div>
    </ConsoleContext.Provider>
  )
}
