# Frontend Architecture Guide

This document describes the architecture of the econ-data-mcp React frontend.

## Overview

The frontend is a React single-page application (SPA) built with:

- **React 18** - UI library
- **TypeScript** - Type safety
- **Vite** - Build tool and dev server
- **React Router** - Client-side routing
- **TanStack Query** - Server state management
- **Recharts** - Data visualization
- **Tailwind CSS** - Utility-first styling

## Directory Structure

```
packages/frontend/
├── src/
│   ├── components/          # React components
│   │   ├── __tests__/       # Component tests
│   │   ├── ChatPage.tsx     # Main chat interface
│   │   ├── MessageChart.tsx # Data visualization
│   │   ├── LandingPage.tsx  # Marketing page
│   │   └── ...
│   ├── contexts/            # React context providers
│   │   └── AuthContext.tsx  # Authentication state
│   ├── hooks/               # Custom React hooks
│   │   └── useMobile.ts     # Viewport detection
│   ├── lib/                 # Utility functions
│   │   ├── export.ts        # Data export utilities
│   │   ├── supabase.ts      # Supabase client
│   │   └── utils.ts         # General utilities
│   ├── services/            # API communication
│   │   └── api.ts           # Axios client and endpoints
│   ├── test/                # Test utilities
│   │   ├── setup.ts         # Vitest setup
│   │   ├── test-utils.tsx   # Custom render wrapper
│   │   └── mocks/           # Mock implementations
│   ├── types/               # TypeScript types
│   │   └── index.ts         # Shared type definitions
│   ├── utils/               # Helper utilities
│   │   └── logger.ts        # Console logging
│   ├── App.tsx              # Root component
│   ├── main.tsx             # Application entry
│   └── index.css            # Global styles
├── public/                  # Static assets
├── index.html               # HTML template
├── vite.config.ts           # Vite configuration
├── tailwind.config.js       # Tailwind configuration
└── package.json
```

## Component Hierarchy

```
App
├── LandingPage
│   └── Example queries, features, pricing
│
├── ChatPage
│   ├── Sidebar
│   │   ├── History list
│   │   ├── Pro Mode toggle
│   │   └── User profile / Login
│   ├── Messages area
│   │   ├── Welcome screen
│   │   └── Message bubbles
│   │       ├── User messages
│   │       └── Assistant messages
│   │           ├── ProcessingSteps
│   │           ├── CodeExecutionDisplay
│   │           └── MessageChart
│   └── Input area
│       └── Query form
│
├── Auth (Modal)
│   ├── Login form
│   └── Register form
│
├── ShareModal
└── FeedbackModal
```

## State Management

### Local State

Most component state is managed with `useState` and `useReducer`:

```tsx
const [query, setQuery] = useState('');
const [messages, setMessages] = useState<Message[]>([]);
const [proMode, setProMode] = useState(false);
```

### Context

Authentication state is shared via React Context:

```tsx
// AuthContext.tsx
const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  // ... auth logic

  return (
    <AuthContext.Provider value={{ user, isAuthenticated, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

// Usage in components
const { user, isAuthenticated } = useAuth();
```

### Server State

TanStack Query handles server state (caching, refetching):

```tsx
const { data: history, isLoading } = useQuery({
  queryKey: ['history'],
  queryFn: () => api.getUserHistory(),
});
```

## Routing

React Router v7 handles client-side navigation:

```tsx
// App.tsx
<BrowserRouter>
  <Routes>
    <Route path="/" element={<LandingPage />} />
    <Route path="/chat" element={<ChatPage />} />
    <Route path="*" element={<Navigate to="/" />} />
  </Routes>
</BrowserRouter>
```

Query parameters are used for:
- `?query=...` - Pre-fill a query (from landing page examples)
- `?auth=1` - Open auth modal

## API Integration

### API Client

Axios instance with interceptors:

```tsx
// services/api.ts
const axiosInstance = axios.create({
  baseURL: '/api',
  timeout: 30000,
});

// Add auth token to requests
axiosInstance.interceptors.request.use((config) => {
  const token = tokenManager.getToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Handle 401 responses
axiosInstance.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      logoutCallback?.();
    }
    return Promise.reject(error);
  }
);
```

### Streaming Queries

Server-Sent Events for real-time progress:

```tsx
api.queryStream(query, conversationId, proMode, {
  onStep: (step) => {
    // Update processing steps
    setActiveProcessingSteps(prev => [...prev, step]);
  },
  onData: (response) => {
    // Handle final response
    setMessages(prev => [...prev, {
      role: 'assistant',
      data: response.data,
      // ...
    }]);
  },
  onError: (error) => {
    // Handle error
  },
  onDone: () => {
    // Cleanup
  },
});
```

## Styling

### Tailwind CSS

Utility classes for rapid styling:

```tsx
<button className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700">
  Submit
</button>
```

### CSS Modules

Component-specific styles in `.css` files:

```css
/* ChatPage.css */
.chat-page {
  display: flex;
  height: 100vh;
}

.chat-sidebar {
  width: 260px;
  border-right: 1px solid var(--border-color);
}
```

### CSS Variables

Theme colors defined in global CSS:

```css
:root {
  --primary-color: #4f46e5;
  --border-color: #e5e7eb;
  --text-color: #1f2937;
}
```

## Data Visualization

### MessageChart Component

Uses Recharts for interactive charts:

```tsx
<ResponsiveContainer width="100%" height={380}>
  <LineChart data={chartData}>
    <CartesianGrid strokeDasharray="3 3" />
    <XAxis dataKey="date" />
    <YAxis />
    <Tooltip />
    <Legend />
    {seriesNames.map((name, i) => (
      <Line key={name} dataKey={name} stroke={colors[i]} />
    ))}
  </LineChart>
</ResponsiveContainer>
```

Chart types:
- **Line** - Time series data
- **Bar** - Annual/categorical data
- **Table** - Detailed data view

## Error Handling

### API Errors

```tsx
try {
  const response = await api.query(query);
  // Handle success
} catch (error) {
  if (axios.isAxiosError(error)) {
    const message = error.response?.data?.error || 'Network error';
    // Show user-friendly error
  }
}
```

### Error Boundaries (Future)

```tsx
<ErrorBoundary fallback={<ErrorFallback />}>
  <ChatPage />
</ErrorBoundary>
```

## Performance Optimizations

### Memoization

```tsx
// Memoize expensive components
export const MessageChart = memo(function MessageChart(props) {
  // ...
});

// Memoize expensive computations
const chartData = useMemo(() => transformData(data), [data]);
const seriesNames = useMemo(() => getSeriesNames(data), [data]);

// Memoize callbacks
const handleSubmit = useCallback((e) => {
  // ...
}, [dependencies]);
```

### Code Splitting

```tsx
// Lazy load heavy components
const ChatPage = lazy(() => import('./components/ChatPage'));

<Suspense fallback={<Loading />}>
  <ChatPage />
</Suspense>
```

### Virtual Lists

For long history lists, consider virtualization:

```tsx
import { useVirtualizer } from '@tanstack/react-virtual';
```

## Development Workflow

### Local Development

```bash
# Start dev server
npm run dev --workspace=packages/frontend

# Access at http://localhost:5173
# API proxied to http://localhost:3001
```

### Building

```bash
# Production build
npm run build:frontend

# Preview production build
npm run preview --workspace=packages/frontend
```

### Testing

```bash
# Run tests
npm run test --workspace=packages/frontend

# Watch mode
npm run test:watch --workspace=packages/frontend

# Coverage
npm run test:coverage --workspace=packages/frontend
```

## Key Files

| File | Purpose |
|------|---------|
| `ChatPage.tsx` | Main chat interface with query handling |
| `MessageChart.tsx` | Data visualization with recharts |
| `AuthContext.tsx` | Authentication state management |
| `api.ts` | API client and endpoint methods |
| `supabase.ts` | Supabase client for auth |
| `types/index.ts` | Shared TypeScript interfaces |

## Future Considerations

1. **State Management** - Consider Zustand for complex state
2. **Error Boundaries** - Graceful error handling
3. **PWA Support** - Service worker for offline capability
4. **i18n** - Internationalization support
5. **Dark Mode** - Theme switching
