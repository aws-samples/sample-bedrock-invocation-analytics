import { useEffect, useState } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getCurrentUser, signOut } from 'aws-amplify/auth'
import AppLayout from '@cloudscape-design/components/app-layout'
import Box from '@cloudscape-design/components/box'
import HelpPanel from '@cloudscape-design/components/help-panel'
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs'
import BreadcrumbGroup from '@cloudscape-design/components/breadcrumb-group'
import Spinner from '@cloudscape-design/components/spinner'
import TopNavigation from '@cloudscape-design/components/top-navigation'
import { api, type Status } from './api'
import { FiltersProvider } from './filters'
import { useFilters } from './filters-context'
import Login from './pages/Login'

const APP_VERSION = import.meta.env.VITE_APP_VERSION ?? ''

export default function App() {
  const [signedIn, setSignedIn] = useState<boolean | null>(null)
  const queryClient = useQueryClient()

  useEffect(() => {
    getCurrentUser().then(() => setSignedIn(true), () => setSignedIn(false))
  }, [])

  if (signedIn === null) return <Box textAlign="center" padding="xxxl"><Spinner size="large" /></Box>
  if (!signedIn) return <Login onSignedIn={() => setSignedIn(true)} />
  return (
    <FiltersProvider>
      <Shell onSignOut={async () => { await signOut(); queryClient.clear(); setSignedIn(false) }} />
    </FiltersProvider>
  )
}

function Shell({ onSignOut }: { onSignOut: () => void }) {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const status = useQuery({ queryKey: ['status'], queryFn: api.status, refetchInterval: 60_000 })
  const [toolsOpen, setToolsOpen] = useState(false)
  const [filtersOpen, setFiltersOpen] = useState(() => localStorage.getItem('filtersOpen') !== 'false')
  const { panel } = useFilters()
  const onDashboard = pathname === '/'

  return (
    <>
      <div id="top-nav" style={{ position: 'sticky', top: 0, zIndex: 1002 }}>
        <TopNavigation
          identity={{ href: '/', title: 'Bedrock Invocation Analytics', onFollow: (e) => { e.preventDefault(); navigate('/') } }}
          utilities={[{
            type: 'button',
            iconName: 'settings',
            ariaLabel: 'Pricing settings',
            title: 'Pricing settings',
            href: '/pricing',
            onClick: (e) => { e.preventDefault(); navigate('/pricing') },
          }, {
            type: 'menu-dropdown',
            iconName: 'user-profile',
            ariaLabel: 'User menu',
            title: status.data?.user.email,
            description: status.data && (status.data.user.is_admin ? 'Administrator' : 'Viewer (read-only)'),
            items: [{ id: 'signout', text: 'Sign out' }],
            onItemClick: (e) => e.detail.id === 'signout' && onSignOut(),
          }]}
        />
      </div>
      <AppLayout
        headerSelector="#top-nav"
        tools={<InfoPanel sources={status.data?.data_sources} />}
        toolsOpen={toolsOpen}
        onToolsChange={(e) => setToolsOpen(e.detail.open)}
        ariaLabels={{
          tools: 'Data sources', toolsToggle: 'Open data sources', toolsClose: 'Close data sources',
          navigation: 'Filters', navigationToggle: 'Show filters', navigationClose: 'Hide filters',
        }}
        navigation={panel}
        navigationHide={!onDashboard}
        navigationOpen={filtersOpen}
        navigationWidth={240}
        onNavigationChange={(e) => { setFiltersOpen(e.detail.open); localStorage.setItem('filtersOpen', String(e.detail.open)) }}
        breadcrumbs={!onDashboard && (
          <BreadcrumbGroup
            items={[{ text: 'Usage dashboard', href: '/' }, { text: 'Pricing', href: '/pricing' }]}
            onFollow={(e) => { e.preventDefault(); navigate(e.detail.href) }}
          />
        )}
        content={<Outlet context={status.data} />}
      />
    </>
  )
}


function InfoPanel({ sources }: { sources?: Status['data_sources'] }) {
  const value = (v?: string) => (v ? <Box variant="code">{v}</Box> : '—')
  return (
    <HelpPanel header={<h2>Data sources</h2>} footer={APP_VERSION && <Box variant="small">Version {APP_VERSION}</Box>}>
      <KeyValuePairs
        columns={1}
        items={[
          { label: 'Region', value: value(sources?.region) },
          { label: 'Usage table (DynamoDB)', value: value(sources?.usage_table) },
          { label: 'Pricing table (DynamoDB)', value: value(sources?.pricing_table) },
          { label: 'Athena workgroup', value: value(sources?.athena_workgroup) },
          { label: 'Iceberg table', value: value(sources?.iceberg_table), info: <Box variant="small">Raw events, one row per call. Query it in Athena for ad-hoc analysis.</Box> },
        ]}
      />
    </HelpPanel>
  )
}
