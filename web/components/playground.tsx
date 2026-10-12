"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import { PlayIcon } from "@/components/icons"
import { useConsole } from "@/components/shell"
import { ApiError, api } from "@/lib/api"
import { rowLabel } from "@/lib/runs"

const KEYWORDS = new Set([
  "SELECT",
  "AS",
  "FROM",
  "WHERE",
  "AND",
  "OR",
  "INSERT",
  "UPDATE",
  "DELETE",
])

type Cell = string | number | null

type Turn = {
  status: string
  thread_id: string
  trace: string[]
  answer?: string
  approval?: { sql?: string }
  sql?: string
  columns?: string[]
  rows?: Cell[][]
  rowcount?: number | null
}

type View =
  | { kind: "idle" }
  | { kind: "error"; code: string; message: string }
  | { kind: "turn"; turn: Turn; editing: boolean; draft: string }

function SqlText({ text }: { text: string }) {
  const parts = text.split(/(\b[A-Za-z_]+\b)/)
  return (
    <pre className="sql mono">
      {parts.map((part, index) =>
        KEYWORDS.has(part) ? (
          <span key={index} className="kw">
            {part}
          </span>
        ) : (
          <span key={index}>{part}</span>
        ),
      )}
    </pre>
  )
}

function statusFor(turn: Turn): string | null {
  if (turn.status === "awaiting_approval") return "awaiting approval"
  const trace = turn.trace ?? []
  if (turn.sql != null) {
    const approved = trace.some((line) => line.includes("human: approved"))
    return `${approved ? "approved" : "executed"} · ${rowLabel(turn.rowcount ?? 0)}`
  }
  if (trace.some((line) => line.includes("human: rejected"))) return "rejected"
  return null
}

function cellText(cell: Cell) {
  return cell == null ? "" : String(cell)
}

export function Playground() {
  const { models, reload } = useConsole()
  const [question, setQuestion] = useState("")
  const [provider, setProvider] = useState("")
  const [busy, setBusy] = useState(false)
  const [view, setView] = useState<View>({ kind: "idle" })
  const inflight = useRef(false)
  const active = provider || models.find((item) => item.start)?.provider || models[0]?.provider || ""

  const submit = useCallback(async () => {
    const text = question.trim()
    if (!text || inflight.current) return
    inflight.current = true
    setBusy(true)
    try {
      const body: { question: string; provider?: string } = { question: text }
      if (active) body.provider = active
      const turn = await api<Turn>("/v1/questions", { method: "POST", body: JSON.stringify(body) })
      setView({ kind: "turn", turn, editing: false, draft: turn.approval?.sql ?? "" })
      await reload()
    } catch (err) {
      setView({
        kind: "error",
        code: err instanceof ApiError ? err.code : "request_failed",
        message: err instanceof Error ? err.message : "Request failed.",
      })
    } finally {
      inflight.current = false
      setBusy(false)
    }
  }, [question, active, reload])

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault()
        submit()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [submit])

  async function decide(action: "approve" | "reject" | "edit") {
    if (view.kind !== "turn" || inflight.current) return
    if (action === "edit" && !view.draft.trim()) return
    inflight.current = true
    setBusy(true)
    try {
      const payload = action === "edit" ? { action, sql: view.draft } : { action }
      const turn = await api<Turn>(`/v1/questions/${view.turn.thread_id}/decision`, {
        method: "POST",
        body: JSON.stringify(payload),
      })
      setView({ kind: "turn", turn, editing: false, draft: turn.approval?.sql ?? "" })
      await reload()
    } catch (err) {
      setView({
        kind: "error",
        code: err instanceof ApiError ? err.code : "request_failed",
        message: err instanceof Error ? err.message : "Request failed.",
      })
    } finally {
      inflight.current = false
      setBusy(false)
    }
  }

  const chosen = models.find((item) => item.provider === active)
  const status = view.kind === "error" ? view.code : view.kind === "turn" ? statusFor(view.turn) : null

  return (
    <div className="playground">
      <header className="pg-head">
        <PlayIcon />
        <h1>Playground</h1>
        <button type="button" className="btn-outline" onClick={() => setQuestion("")}>
          Clear
        </button>
      </header>
      <div className="pg-body">
        <section className="question-col" aria-label="Question">
          <label className="q-label" htmlFor="question">
            Question
          </label>
          <textarea
            id="question"
            value={question}
            placeholder="How many customers are named Ada?"
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              if ((event.ctrlKey || event.metaKey) && event.key === "Enter") event.preventDefault()
            }}
          />
          <footer className="q-foot">
            {models.length === 1 ? (
              <span className="chip" title={models[0].provider}>
                {models[0].model}
              </span>
            ) : null}
            {models.length > 1 ? (
              <select
                className="chip"
                aria-label="Model"
                title={chosen?.provider}
                value={active}
                onChange={(event) => setProvider(event.target.value)}
              >
                {models.map((item) => (
                  <option key={item.provider} value={item.provider}>
                    {item.model}
                  </option>
                ))}
              </select>
            ) : null}
            <span className="run-side">
              <span className="kbd">Ctrl+Enter</span>
              <button type="button" className="btn-primary" disabled={busy || !question.trim()} onClick={submit}>
                Run
              </button>
            </span>
          </footer>
        </section>
        <section className="response-col" aria-label="Response">
          <header className="resp-head">
            <h2>Response</h2>
            {status ? <p className="status">{status}</p> : null}
          </header>
          <div className="resp-body">
            {view.kind === "idle" ? <p className="empty">Run a question to see the SQL.</p> : null}
            {view.kind === "error" ? <p className="answer">{view.message}</p> : null}
            {view.kind === "turn" ? (
              <TurnView
                view={view}
                busy={busy}
                onDraft={(draft) => setView({ ...view, draft })}
                onEdit={() => setView({ ...view, editing: true })}
                onDecide={decide}
              />
            ) : null}
          </div>
        </section>
      </div>
    </div>
  )
}

function TurnView({
  view,
  busy,
  onDraft,
  onEdit,
  onDecide,
}: {
  view: Extract<View, { kind: "turn" }>
  busy: boolean
  onDraft: (draft: string) => void
  onEdit: () => void
  onDecide: (action: "approve" | "reject" | "edit") => void
}) {
  const { turn, editing, draft } = view
  const pending = turn.status === "awaiting_approval"
  const sql = pending ? (turn.approval?.sql ?? "") : (turn.sql ?? "")
  const columns = turn.columns ?? []
  const rows = turn.rows ?? []
  const numeric = rows.length > 0 && rows.every((row) => row.every((cell) => /^-?\d+(\.\d+)?$/.test(cellText(cell))))

  return (
    <>
      {sql ? (
        <>
          <h3 className="field-label">SQL</h3>
          {editing ? (
            <textarea className="sql-edit mono" value={draft} onChange={(event) => onDraft(event.target.value)} />
          ) : (
            <SqlText text={sql} />
          )}
        </>
      ) : null}
      {pending ? (
        <div className="approval-actions">
          <button type="button" className="btn-primary" disabled={busy} onClick={() => onDecide("approve")}>
            Approve
          </button>
          <button type="button" className="btn-outline" disabled={busy} onClick={() => onDecide("reject")}>
            Reject
          </button>
          <button
            type="button"
            className="btn-outline"
            disabled={busy || (editing && !draft.trim())}
            onClick={() => (editing ? onDecide("edit") : onEdit())}
          >
            {editing ? "Send edit" : "Edit"}
          </button>
        </div>
      ) : null}
      {columns.length > 0 ? (
        <>
          <h3 className="field-label" id="result-label">
            Result
          </h3>
          <table className="result" aria-labelledby="result-label">
            <thead>
              <tr>
                {columns.map((column) => (
                  <th key={column} className={numeric ? "num" : undefined}>
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={index}>
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex} className={numeric ? "num" : undefined}>
                      {cellText(cell)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </>
      ) : null}
      {turn.answer ? (
        <>
          <h3 className="field-label">Answer</h3>
          <p className="answer">{turn.answer}</p>
        </>
      ) : null}
      {!pending && turn.trace?.length ? (
        <>
          <h3 className="field-label">Trace</h3>
          <ul className="trace">
            {turn.trace.map((line, index) => (
              <li key={index} className="mono">
                {line}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </>
  )
}
