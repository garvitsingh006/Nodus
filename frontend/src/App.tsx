import { AccessGate } from './components/AccessGate'
import { AuthorizedWorkspace } from './components/AuthorizedWorkspace'
import { useAuthorization } from './auth/useAuthorization'
import './App.css'

export default function App() {
  const auth = useAuthorization()
  return <main className={auth.context ? 'page authorized' : 'page locked'}>
    <div className="shell">
      <header className="brand"><h1>NODUS</h1><div className="accent" /></header>
      {auth.context
        ? <AuthorizedWorkspace context={auth.context} scope={auth.scope} pending={auth.pending}
            projectError={auth.projectError} onSelectProject={auth.selectProject} onChangeToken={auth.reset} />
        : <AccessGate pending={auth.pending} error={auth.error} onSubmit={auth.authorize} onCancel={auth.reset} />}
    </div>
  </main>
}
