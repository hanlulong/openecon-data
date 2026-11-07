import { Suspense, lazy } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider } from './contexts/AuthContext'
import { ErrorBoundary } from './components/ErrorBoundary'
import { RouteMeta } from './components/RouteMeta'
import { installStaleChunkRecovery, loadRoute } from './lib/staleChunkRecovery'

// Lazy route chunks can vanish under an open tab after a deploy; reload once
// to pick up the current build instead of leaving the route broken.
installStaleChunkRecovery()

const queryClient = new QueryClient()
const LandingPage = lazy(() => loadRoute(() => import('./components/LandingPage'), (module) => module.LandingPage))
const ChatPage = lazy(() => loadRoute(() => import('./components/ChatPage'), (module) => module.ChatPage))
const DocsPage = lazy(() => loadRoute(() => import('./pages/DocsPage'), (module) => module.DocsPage))
const ExamplesPage = lazy(() => loadRoute(() => import('./pages/ExamplesPage'), (module) => module.ExamplesPage))
const AuthCallback = lazy(() => loadRoute(() => import('./pages/AuthCallback'), (module) => module.AuthCallback))
const ResetPassword = lazy(() => loadRoute(() => import('./pages/ResetPassword'), (module) => module.ResetPassword))

function RouteFallback() {
  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontFamily: 'Inter, system-ui, sans-serif',
        color: '#64748b',
        backgroundColor: '#f8fafc',
      }}
    >
      Loading...
    </div>
  )
}

function App() {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <Router>
            <RouteMeta />
            <Suspense fallback={<RouteFallback />}>
              <Routes>
                <Route path="/" element={<LandingPage />} />
                <Route path="/chat" element={<ChatPage />} />
                <Route path="/docs" element={<DocsPage />} />
                <Route path="/examples" element={<ExamplesPage />} />
                <Route path="/auth/callback" element={<AuthCallback />} />
                <Route path="/reset-password" element={<ResetPassword />} />
                {/* Catch-all: redirect unmatched/stale URLs to the landing page */}
                <Route path="*" element={<Navigate to="/" replace />} />
              </Routes>
            </Suspense>
          </Router>
        </AuthProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  )
}

export default App
