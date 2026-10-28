import { useEffect, useState } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getCurrentUser, signOut } from 'aws-amplify/auth'
import AppLayout from '@cloudscape-design/components/app-layout'
import Box from '@cloudscape-design/components/box'
import SideNavigation from '@cloudscape-design/components/side-navigation'
import Spinner from '@cloudscape-design/components/spinner'
import TopNavigation from '@cloudscape-design/components/top-navigation'
import { api, ApiError } from './api'
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
  return <Shell onSignOut={async () => { await signOut(); queryClient.clear(); setSignedIn(false) }} />
}

function Shell({ onSignOut }: { onSignOut: () => void }) {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const status = useQuery({ queryKey: ['status'], queryFn: api.status, refetchInterval: 60_000 })
  const denied = status.error instanceof ApiError && status.error.status === 403

  return (
    <>
      <div id="top-nav" style={{ position: 'sticky', top: 0, zIndex: 1002 }}>
        <TopNavigation
          identity={{ href: '/', title: 'Bedrock Invocation Analytics', onFollow: (e) => { e.preventDefault(); navigate('/') } }}
          utilities={[{
            type: 'menu-dropdown',
            text: status.data?.user.username ?? '',
            description: status.data?.user.is_admin ? 'Administrator' : 'Viewer',
            iconName: 'user-profile',
            items: [{ id: 'signout', text: 'Sign out' }],
            onItemClick: (e) => e.detail.id === 'signout' && onSignOut(),
          }]}
        />
      </div>
      <AppLayout
        headerSelector="#top-nav"
        toolsHide
        navigation={
          <SideNavigation
            activeHref={pathname}
            onFollow={(e) => { e.preventDefault(); navigate(e.detail.href) }}
            items={[{ type: 'link', text: 'Dashboard', href: '/' }, { type: 'link', text: 'Pricing', href: '/pricing' }]}
            header={{ text: 'Navigation', href: '/' }}
          />
        }
        content={denied
          ? <Box variant="p" padding="xxl">Your account is not in the bedrock-analytics-viewer or bedrock-analytics-admin group. Ask an administrator for access.</Box>
          : <Outlet context={status.data} />}
      />
      {APP_VERSION && <Box variant="small" color="text-status-inactive" padding="s" textAlign="right">v{APP_VERSION}</Box>}
    </>
  )
}
