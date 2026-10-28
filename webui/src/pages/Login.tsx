import { useState, type FormEvent } from 'react'
import { confirmResetPassword, confirmSignIn, resetPassword, signIn, type SignInOutput } from 'aws-amplify/auth'
import Alert from '@cloudscape-design/components/alert'
import Box from '@cloudscape-design/components/box'
import Button from '@cloudscape-design/components/button'
import Container from '@cloudscape-design/components/container'
import Form from '@cloudscape-design/components/form'
import FormField from '@cloudscape-design/components/form-field'
import Header from '@cloudscape-design/components/header'
import Input from '@cloudscape-design/components/input'
import Link from '@cloudscape-design/components/link'
import SpaceBetween from '@cloudscape-design/components/space-between'

type Step = 'signIn' | 'newPassword' | 'totp' | 'totpSetup' | 'forgot' | 'resetCode'

interface Props {
  onSignedIn: () => void
}

export default function Login({ onSignedIn }: Props) {
  const [step, setStep] = useState<Step>('signIn')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [totpUri, setTotpUri] = useState('')
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')
  const [busy, setBusy] = useState(false)

  const handleNext = (out: SignInOutput) => {
    setCode('')
    switch (out.nextStep.signInStep) {
      case 'DONE':
        return onSignedIn()
      case 'CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED':
        return setStep('newPassword')
      case 'CONFIRM_SIGN_IN_WITH_TOTP_CODE':
        return setStep('totp')
      case 'CONTINUE_SIGN_IN_WITH_TOTP_SETUP':
        setTotpUri(out.nextStep.totpSetupDetails.getSetupUri('Bedrock Invocation Analytics', username).toString())
        return setStep('totpSetup')
      case 'RESET_PASSWORD':
        return startReset()
      default:
        setError(`Sign-in step ${out.nextStep.signInStep} is not supported here. Contact your administrator.`)
    }
  }

  const run = (action: () => Promise<void>) => async (e: FormEvent) => {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      await action()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function startReset() {
    await resetPassword({ username })
    setInfo('A verification code was sent to the email address on your account.')
    setStep('resetCode')
  }

  const submit: Record<Step, () => Promise<void>> = {
    signIn: async () => handleNext(await signIn({ username, password })),
    newPassword: async () => handleNext(await confirmSignIn({ challengeResponse: newPassword })),
    totp: async () => handleNext(await confirmSignIn({ challengeResponse: code })),
    totpSetup: async () => handleNext(await confirmSignIn({ challengeResponse: code })),
    forgot: startReset,
    resetCode: async () => {
      await confirmResetPassword({ username, confirmationCode: code, newPassword })
      setInfo('Password changed. Sign in with the new password.')
      setPassword('')
      setStep('signIn')
    },
  }

  const titles: Record<Step, string> = {
    signIn: 'Sign in',
    newPassword: 'Set a new password',
    totp: 'Enter your authenticator code',
    totpSetup: 'Set up an authenticator app',
    forgot: 'Reset password',
    resetCode: 'Reset password',
  }

  return (
    <Box padding={{ top: 'xxxl' }}>
      <div style={{ maxWidth: 420, margin: '0 auto' }}>
        <form onSubmit={run(submit[step])}>
          <Form
            actions={<Button variant="primary" loading={busy} formAction="submit">Continue</Button>}
            secondaryActions={step !== 'signIn' && (
              <Button variant="link" formAction="none" onClick={() => { setStep('signIn'); setError(''); setInfo('') }}>Back to sign in</Button>
            )}
          >
            <Container header={<Header variant="h1" description="Bedrock Invocation Analytics">{titles[step]}</Header>}>
              <SpaceBetween size="m">
                {error && <Alert type="error">{error}</Alert>}
                {info && <Alert type="info">{info}</Alert>}
                {(step === 'signIn' || step === 'forgot') && (
                  <FormField label="Username or email">
                    <Input value={username} onChange={(e) => setUsername(e.detail.value)} autoComplete="username" autoFocus />
                  </FormField>
                )}
                {step === 'signIn' && (
                  <>
                    <FormField label="Password">
                      <Input type="password" value={password} onChange={(e) => setPassword(e.detail.value)} autoComplete="current-password" />
                    </FormField>
                    <Link onFollow={() => { setStep('forgot'); setError(''); setInfo('') }}>Forgot password?</Link>
                  </>
                )}
                {step === 'totpSetup' && (
                  <FormField label="Setup key" description="Add this URI to your authenticator app, then enter the 6-digit code it shows.">
                    <Box variant="code">{totpUri}</Box>
                  </FormField>
                )}
                {(step === 'totp' || step === 'totpSetup' || step === 'resetCode') && (
                  <FormField label="Code">
                    <Input value={code} onChange={(e) => setCode(e.detail.value)} inputMode="numeric" autoComplete="one-time-code" autoFocus />
                  </FormField>
                )}
                {(step === 'newPassword' || step === 'resetCode') && (
                  <FormField label="New password" constraintText="At least 12 characters with upper and lower case letters, a number and a symbol.">
                    <Input type="password" value={newPassword} onChange={(e) => setNewPassword(e.detail.value)} autoComplete="new-password" />
                  </FormField>
                )}
              </SpaceBetween>
            </Container>
          </Form>
        </form>
      </div>
    </Box>
  )
}
