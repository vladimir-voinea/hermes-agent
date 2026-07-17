// v2 (opencode-style flat/borderless) palette — exact hex values, do not
// derive from theme.ts. Kept standalone so the v2 view has zero coupling
// to the existing (bordered) theme system.
export const P = {
  bg: '#16161e',
  body: '#9aa0c0',
  text: '#a9b1d6',
  dim: '#6b7089',
  faint: '#3b3f52',
  primary: '#c0caf5',
  accent: '#7aa2f7',
  cyan: '#7dcfff',
  green: '#9ece6a',
  red: '#f7768e',
  yellow: '#e0af68',
  orange: '#ff9e64'
} as const
