import type { Metadata } from "next"
import type { ReactNode } from "react"
import { Inter, JetBrains_Mono } from "next/font/google"
import { Shell } from "@/components/shell"
import "./globals.css"

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
})

const mono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  display: "swap",
})

export const metadata: Metadata = {
  title: { default: "NL2SQL", template: "%s · NL2SQL" },
}

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${mono.variable}`}>
      <body className={inter.className}>
        <Shell>{children}</Shell>
      </body>
    </html>
  )
}
