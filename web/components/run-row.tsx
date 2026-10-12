import type { AuditRun } from "@/lib/runs"
import { rowLabel } from "@/lib/runs"

export function RunRow({ row }: { row: AuditRun }) {
  return (
    <div className="run-row">
      <div className="run-line">
        <time className="mono stamp" dateTime={row.at}>
          {row.at}
        </time>
        <span className="decision">{row.decision}</span>
        <span className="rows-n mono">{rowLabel(row.rowcount)}</span>
        <span className="msg mono">{row.sql}</span>
      </div>
    </div>
  )
}
