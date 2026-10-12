"use client"

import { useState } from "react"
import { RunRow } from "@/components/run-row"
import { useConsole } from "@/components/shell"
import { writeText } from "@/lib/clipboard"
import { rowLabel } from "@/lib/runs"

export function RunList() {
  const { rows } = useConsole()
  const [copied, setCopied] = useState(false)

  async function copy() {
    const text = rows
      .map((row) => `${row.at}  ${row.decision}  ${rowLabel(row.rowcount)}  ${row.sql}`)
      .join("\n")
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1500)
    await writeText(text)
  }

  return (
    <div className="runs">
      <header className="runs-head">
        <h1>Runs</h1>
        <div className="runs-actions">
          <button type="button" className="btn-outline" onClick={copy}>
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      </header>
      <div className="run-wrap">
        <div className="run-scroller">
          {rows.length === 0 ? <p className="empty runs-empty">No runs.</p> : null}
          {rows.map((row) => (
            <RunRow key={row.id} row={row} />
          ))}
        </div>
      </div>
    </div>
  )
}
