export type Role = 'ADMIN' | 'DEVELOPER' | 'INTERN'

export interface AuthorizationContext {
  identity: { id: string; name: string }
  role: Role
  company: { id: string; name: string }
  projects: readonly { id: string; name: string }[]
  permissions: readonly string[]
  capabilities: { requestAccess: boolean; reviewAccessRequests: boolean }
}

// Future project API calls must use this scope's projectId and AbortSignal.
// A switch aborts the signal and replaces the scope, even when returning to the same project.
export interface ProjectScope {
  projectId: string
  generation: number
  signal: AbortSignal
}

export type AuthorizationResolver = (token: string, signal: AbortSignal) => Promise<AuthorizationContext>
