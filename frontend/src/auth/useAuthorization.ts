import { useCallback, useEffect, useRef, useState } from 'react'
import { AuthorizationError, resolveAuthorization } from './api'
import type { AuthorizationContext, ProjectScope } from './models'

export function useAuthorization() {
  const [context, setContext] = useState<AuthorizationContext | null>(null)
  const [scope, setScope] = useState<ProjectScope | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [projectError, setProjectError] = useState('')
  const generation = useRef(0)
  const request = useRef<AbortController | null>(null)
  const projectRequest = useRef<AbortController | null>(null)
  const credential = useRef('')
  const timer = useRef<number | undefined>(undefined)

  const invalidate = useCallback(() => {
    generation.current++
    request.current?.abort()
    projectRequest.current?.abort()
    request.current = null
    projectRequest.current = null
    window.clearTimeout(timer.current)
  }, [])

  const reset = useCallback(() => {
    invalidate()
    credential.current = ''
    setContext(null)
    setScope(null)
    setPending(false)
    setError('')
    setProjectError('')
  }, [invalidate])

  useEffect(() => () => { invalidate(); credential.current = '' }, [invalidate])

  async function resolveSession(token: string, requestedProject?: string, previous?: AuthorizationContext) {
    invalidate()
    const current = generation.current
    const controller = new AbortController()
    request.current = controller
    setScope(null) // Unmount all previous project content before awaiting validation.
    setPending(true)
    setError('')
    setProjectError('')
    timer.current = window.setTimeout(() => {
      if (generation.current === current) {
        reset()
        setError('Authorization timed out. Please retry.')
      }
    }, 15000)
    try {
      const result = await resolveAuthorization(token, controller.signal)
      if (generation.current !== current) return
      if (previous && (previous.identity.id !== result.identity.id || previous.company.id !== result.company.id))
        throw new AuthorizationError('Authorization changed. Enter your token again.')
      credential.current = token
      setContext(result)
      const project = requestedProject
        ? result.projects.find(item => item.id === requestedProject)
        : result.projects[0]
      if (requestedProject && !project) {
        setProjectError('Access to that project is no longer available. Choose an authorized project or change token.')
      } else if (project) {
        const projectController = new AbortController()
        projectRequest.current = projectController
        setScope({ projectId: project.id, generation: current, signal: projectController.signal })
      }
    } catch (reason) {
      if (generation.current === current) {
        reset()
        setError(reason instanceof AuthorizationError ? reason.message : 'Authorization is unavailable. Please retry.')
      }
    } finally {
      if (generation.current === current) {
        window.clearTimeout(timer.current)
        request.current = null
        setPending(false)
      }
    }
  }

  async function authorize(token: string) {
    if (request.current) return
    if (!token.trim()) { setError('Enter an access token to continue.'); return }
    await resolveSession(token.trim())
  }

  async function selectProject(projectId: string) {
    if (!context || !credential.current) return
    if (!context.projects.some(project => project.id === projectId)) {
      reset()
      setError('Project access could not be verified. Enter your token again.')
      return
    }
    await resolveSession(credential.current, projectId, context)
  }

  return { context, scope, pending, error, projectError, authorize, selectProject, reset }
}
