"use client"

import Link from "next/link"
import { ArrowIcon, PlayIcon, RunsIcon } from "@/components/icons"
import { useConsole } from "@/components/shell"

export function Home() {
  const { rows } = useConsole()
  const recent = rows.slice(0, 3)

  return (
    <div className="home">
      <div className="home-inner">
        <h1>Ask the database</h1>
        <Link className="btn-primary" href="/playground">
          Playground
          <ArrowIcon />
        </Link>

        <div className="tiles">
          <Link className="tile" href="/playground">
            <PlayIcon />
            <strong>Playground</strong>
            <span>Ask a question and read the SQL.</span>
          </Link>
          <Link className="tile" href="/runs">
            <RunsIcon />
            <strong>Runs</strong>
            <span>SQL that ran for this login.</span>
          </Link>
        </div>

        <div className="recent-head">
          <h2>Recent</h2>
          <Link className="link" href="/runs">
            View
          </Link>
        </div>
        {recent.length === 0 ? (
          <p className="empty">No runs yet.</p>
        ) : (
          <ul className="recent">
            {recent.map((row) => (
              <li key={row.id} className="recent-row">
                <time className="mono" dateTime={row.at}>
                  {row.at}
                </time>
                <span className="sql-brief">{row.sql}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
