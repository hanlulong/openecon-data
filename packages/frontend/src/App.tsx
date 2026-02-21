import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom'
import { AuthProvider } from './contexts/AuthContext'
import { ErrorBoundary } from './components/ErrorBoundary'
import { LandingPage } from './components/LandingPage'
import { ChatPage } from './components/ChatPage'
import { DocsPage } from './pages/DocsPage'
import { ExamplesPage } from './pages/ExamplesPage'
import { AuthCallback } from './pages/AuthCallback'

const queryClient = new QueryClient()

function App() {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <Router>
            <Routes>
              <Route path="/" element={<LandingPage />} />
              <Route path="/chat" element={<ChatPage />} />
              <Route path="/docs" element={<DocsPage />} />
              <Route path="/examples" element={<ExamplesPage />} />
              <Route path="/auth/callback" element={<AuthCallback />} />
            </Routes>
          </Router>
        </AuthProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  )
}

export default App
