import type { Metadata } from "next"
import { RunList } from "@/components/run-list"

export const metadata: Metadata = { title: "Runs" }

export default function Page() {
  return <RunList />
}
