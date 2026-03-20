import { Suspense, lazy } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom'
import { AuthProvider } from './contexts/AuthContext'
import { ErrorBoundary } from './components/ErrorBoundary'

const queryClient = new QueryClient()
const LandingPage = lazy(() => import('./components/LandingPage').then((module) => ({ default: module.LandingPage })))
const ChatPage = lazy(() => import('./components/ChatPage').then((module) => ({ default: module.ChatPage })))
const DocsPage = lazy(() => import('./pages/DocsPage').then((module) => ({ default: module.DocsPage })))
const ExamplesPage = lazy(() => import('./pages/ExamplesPage').then((module) => ({ default: module.ExamplesPage })))
const AuthCallback = lazy(() => import('./pages/AuthCallback').then((module) => ({ default: module.AuthCallback })))

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
            <Suspense fallback={<RouteFallback />}>
              <Routes>
                <Route path="/" element={<LandingPage />} />
                <Route path="/chat" element={<ChatPage />} />
                <Route path="/docs" element={<DocsPage />} />
                <Route path="/examples" element={<ExamplesPage />} />
                <Route path="/auth/callback" element={<AuthCallback />} />
              </Routes>
            </Suspense>
          </Router>
        </AuthProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  )
}

export default App
