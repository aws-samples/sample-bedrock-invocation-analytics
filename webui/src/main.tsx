import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { createBrowserRouter, RouterProvider, useOutletContext } from 'react-router'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import '@cloudscape-design/global-styles/index.css'
import './app.css'
import Alert from '@cloudscape-design/components/alert'
import Box from '@cloudscape-design/components/box'
import { I18nProvider } from '@cloudscape-design/components/i18n'
import messages from '@cloudscape-design/components/i18n/messages/all.en'
import type { Status } from './api'
import App from './App'
import { loadRuntimeConfig } from './config'
import Dashboard from './pages/Dashboard'
import Pricing from './pages/Pricing'

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: (count, err) => count < 2 && !(err as { status?: number }).status?.toString().startsWith('4') } },
})

const withStatus = (Page: (p: { status?: Status }) => React.ReactNode) => () => <Page status={useOutletContext<Status | undefined>()} />

const router = createBrowserRouter([
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, Component: withStatus(Dashboard) },
      { path: 'pricing', Component: withStatus(Pricing) },
    ],
  },
])

const root = createRoot(document.getElementById('root')!)

loadRuntimeConfig().then(
  () => root.render(
    <StrictMode>
      <I18nProvider locale="en" messages={[messages]}>
        <QueryClientProvider client={queryClient}>
          <RouterProvider router={router} />
        </QueryClientProvider>
      </I18nProvider>
    </StrictMode>,
  ),
  (err: Error) => root.render(<Box padding="xxl"><Alert type="error" header="Configuration error">{err.message}</Alert></Box>),
)
