import { useState } from 'react'
import type { AuthorizationContext, ProjectScope } from '../auth/models'

interface Props {
  context: AuthorizationContext
  scope: ProjectScope | null
  pending: boolean
  projectError: string
  onSelectProject: (id: string) => void
  onChangeToken: () => void
}

function ObjectiveDraft() {
  const [objective, setObjective] = useState('')
  return <section className="objective">
    <label htmlFor="objective">What would you like to investigate?</label>
    <textarea id="objective" value={objective} onChange={event => setObjective(event.target.value)}
      placeholder="Describe your investigation objective" rows={4} aria-describedby="objective-note" />
    <p id="objective-note">Objective continuation is not implemented yet.</p>
    <button className="primary" disabled>Continue<span aria-hidden="true">→</span></button>
  </section>
}

export function AuthorizedWorkspace({ context, scope, pending, projectError, onSelectProject, onChangeToken }: Props) {
  const [accessOpen, setAccessOpen] = useState(false)
  const project = context.projects.find(item => item.id === scope?.projectId)
  const accessLabel = context.role === 'ADMIN'
    ? (context.capabilities.reviewAccessRequests && context.permissions.includes('ADMIN') ? 'Review access requests' : null)
    : (context.capabilities.requestAccess ? 'Request additional access' : null)

  return <section className="workspace" aria-label="Authorized workspace">
    <div className="access-row">
      <span className="granted">● Access granted</span>
      <button className="text-button" onClick={onChangeToken}>Change token</button>
    </div>
    <header className="context">
      <div className="company-row">
        <p className="eyebrow">{context.company.name}</p>
        <span className="badge" aria-label="Authenticated role">{context.role}</span>
      </div>
      <p className="identity">{context.identity.name}</p>
      <div className="project-row">
        <div>
          <p className="eyebrow">Active project</p>
          <h2>{project?.name ?? (pending ? 'Validating project access…' : 'No active project')}</h2>
        </div>
        {context.projects.length > 1 && <div className="project-picker">
          <label htmlFor="active-project">Switch project</label>
          <select id="active-project" value={project?.id ?? ''} onChange={event => onSelectProject(event.target.value)}>
            <option value="" disabled>Choose an authorized project</option>
            {context.projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </div>}
      </div>
      {accessLabel && <button className="access-action" aria-expanded={accessOpen} aria-controls="access-info"
        onClick={() => setAccessOpen(open => !open)}>{accessLabel}<span aria-hidden="true">↗</span></button>}
      {accessOpen && accessLabel && <aside id="access-info" className="access-info" aria-label={accessLabel}>
        <p>{context.role === 'ADMIN'
          ? 'Access request review will be available in a future update. Approval and rejection are not available yet.'
          : 'Access request submission will be available in a future update. No request has been submitted.'}</p>
        <button className="text-button" onClick={() => setAccessOpen(false)}>Close</button>
      </aside>}
    </header>
    {pending && <p className="workspace-message" role="status">Checking current project permissions…</p>}
    {projectError && <div className="workspace-message error" role="alert">
      <p>{projectError}</p>
      {context.projects.length === 1 && <button className="text-button" onClick={() => onSelectProject(context.projects[0].id)}>Use authorized project</button>}
    </div>}
    {!pending && context.projects.length === 0 && <section className="restricted" aria-label="Restricted access">
      <h2>No authorized projects</h2>
      <p>Your current authorization does not include a project. Change token or check with your administrator.</p>
    </section>}
    {project && scope && !pending && <ObjectiveDraft key={scope.generation} />}
  </section>
}
