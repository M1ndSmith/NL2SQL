import type { ReactNode } from "react"

type IconProps = { className?: string }

function Svg({ className, children }: IconProps & { children: ReactNode }) {
  return (
    <svg
      className={className ?? "icon"}
      viewBox="0 0 16 16"
      width="16"
      height="16"
      aria-hidden="true"
      fill="none"
    >
      {children}
    </svg>
  )
}

export function Mark() {
  return (
    <Svg>
      <rect x="1.5" y="1.5" width="13" height="13" stroke="currentColor" strokeWidth="2" />
    </Svg>
  )
}

export function HomeIcon() {
  return (
    <Svg>
      <path
        d="M2.5 7.2 8 2.8l5.5 4.4V13a.5.5 0 0 1-.5.5H3a.5.5 0 0 1-.5-.5V7.2Z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </Svg>
  )
}

export function PlayIcon() {
  return (
    <Svg>
      <path
        d="M5.5 1.8h5M6.2 1.8v3.4L3.2 13.2h9.6L9.8 5.2V1.8"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </Svg>
  )
}

export function RunsIcon() {
  return (
    <Svg>
      <path
        d="M3 4.5h10M3 8h10M3 11.5h6"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </Svg>
  )
}

export function PanelIcon() {
  return (
    <Svg>
      <rect x="2" y="2.5" width="12" height="11" rx="1" stroke="currentColor" strokeWidth="1.5" />
      <path d="M6 2.5v11" stroke="currentColor" strokeWidth="1.5" />
    </Svg>
  )
}

export function ArrowIcon() {
  return (
    <Svg>
      <path
        d="M3 8h10M9.5 4.5 13 8l-3.5 3.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </Svg>
  )
}
