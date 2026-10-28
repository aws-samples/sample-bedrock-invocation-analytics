import { Amplify } from 'aws-amplify'

export interface RuntimeConfig {
  region: string
  userPoolId: string
  userPoolClientId: string
}

/** Loads /config.json (written by the stack) so one build runs in any account. */
export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  const resp = await fetch('/config.json', { cache: 'no-store' })
  if (!resp.ok) throw new Error(`Failed to load /config.json: HTTP ${resp.status}`)
  const config = (await resp.json()) as RuntimeConfig
  if (!config.userPoolId || !config.userPoolClientId) throw new Error('config.json is missing the Cognito user pool settings')
  Amplify.configure({
    Auth: { Cognito: { userPoolId: config.userPoolId, userPoolClientId: config.userPoolClientId, loginWith: { username: true, email: true } } },
  })
  return config
}
