export type AuditRun = {
  id: string
  at: string
  sql: string
  rowcount: number | null
  decision: string
}

export type ApiRun = {
  created_at: string
  sql: string
  rowcount: number | null
  decision: string
}

export function stamp(date = new Date()): string {
  const pad = (value: number, width = 2) => String(value).padStart(width, "0")
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`
}

export function mapRun(row: ApiRun, index: number): AuditRun {
  const when = new Date(row.created_at)
  return {
    id: `${row.created_at}:${index}`,
    at: Number.isNaN(when.getTime()) ? row.created_at : stamp(when),
    sql: row.sql,
    rowcount: row.rowcount,
    decision: row.decision,
  }
}

export function rowLabel(count: number | null): string {
  if (count == null) return ""
  return count === 1 ? "1 row" : `${count} rows`
}
