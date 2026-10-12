import type { Metadata } from "next"
import { Home } from "@/components/home"

export const metadata: Metadata = { title: { absolute: "Home · NL2SQL" } }

export default function Page() {
  return <Home />
}
