import { t, useLocale } from './i18n';
import { LocaleControls } from './LocaleControls';
import { useEffect, useMemo, useRef, useState } from 'react';
import { App } from './App';
import { ApiError } from './api';
import { Panel } from './components';
import { isLegacyLoopback, securityStatus, type SecurityStatus } from './security';
import { remember, save } from './useDesk';
import type { Connection } from './transport';

type Phase = 'node' | 'checking' | 'pair' | 'legacy' | 'ready' | 'error';
export function ConnectionGate({ transport }: { transport: Connection }) {
  useLocale();
  const [node, setNode] = useState(() =>
    transport.native ? remember('node', 'https://radio.local:8770') : location.origin,
  );
  const [phase, setPhase] = useState<Phase>(transport.native ? 'node' : 'checking'),
    [status, setStatus] = useState<SecurityStatus>();
  const [settings, setSettings] = useState(false),
    [code, setCode] = useState(''),
    [label, setLabel] = useState(() =>
      t(transport.native ? 'Windows spectrum desk' : 'Browser spectrum desk'),
    ),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false),
    [legacyAccepted, setLegacyAccepted] = useState(false);
  const epoch = useRef(0),
    mounted = useRef(true);
  const [connectedOrigin, setConnectedOrigin] = useState(transport.native ? '' : location.origin);
  const readyTransport = useMemo(
    () => ({ ...transport, ready: true }),
    [transport, connectedOrigin],
  );
  const legacyEntry =
    !transport.native && new URLSearchParams(location.search).get('open') === 'legacy';
  useEffect(() => {
    if (legacyEntry && phase === 'ready' && !settings) location.replace('/');
  }, [legacyEntry, phase, settings]);

  const check = async (target = node) => {
    const generation = ++epoch.current;
    setError('');
    setPhase('checking');
    try {
      const current = await securityStatus(transport);
      if (!mounted.current || epoch.current !== generation) return;
      if (
        current.mode === 'lan_tls' &&
        !(transport.native ? new URL(target).protocol === 'https:' : location.protocol === 'https:')
      )
        throw Error('Pairing requires trusted HTTPS');
      if (current.mode === 'legacy_loopback' && !isLegacyLoopback(target))
        throw Error('Legacy compatibility is limited to a loopback HTTP node');
      setStatus(current);
      setPhase(
        current.mode === 'legacy_loopback' ? 'legacy' : current.pairing_required ? 'pair' : 'ready',
      );
    } catch (reason) {
      if (!mounted.current || epoch.current !== generation) return;
      if (reason instanceof ApiError && reason.code === 'not_found' && isLegacyLoopback(target)) {
        setStatus({ mode: 'legacy_loopback', pairing_required: false });
        setPhase('legacy');
      } else {
        setError(reason instanceof Error ? reason.message : 'Core security check failed');
        setPhase('error');
      }
    }
  };
  useEffect(() => {
    mounted.current = true;
    if (!transport.native) void check();
    return () => {
      mounted.current = false;
      epoch.current++;
    };
  }, [transport]);
  useEffect(() => {
    if (phase !== 'ready' || status?.mode !== 'lan_tls') return;
    let reading = false,
      active = true;
    const timer = setInterval(async () => {
      if (reading) return;
      reading = true;
      try {
        const current = await securityStatus(transport);
        if (active && mounted.current && current.pairing_required) {
          setStatus(current);
          setPhase('pair');
          setSettings(false);
        }
      } catch {
      } finally {
        reading = false;
      }
    }, 30000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [phase, status?.mode, transport]);
  async function execute(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await action();
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error ? reason.message : 'Connection failed');
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  if (phase === 'ready' && !settings && legacyEntry)
    return (
      <main className="connection-page">
        <p role="status">{t('Opening legacy UI…')}</p>
      </main>
    );
  if (phase === 'ready' && !settings)
    return <App transport={readyTransport} onConnectionSettings={() => setSettings(true)} />;
  return (
    <main className="connection-page" data-testid="connection-gate" data-phase={phase}>
      <LocaleControls />
      <a className="brand" href="/">
        {t('OpenRadioInspector')}
      </a>
      <Panel
        title={
          phase === 'pair'
            ? transport.native
              ? t('Pair this Windows client')
              : t('Pair this browser')
            : t('Core connection')
        }
        eyebrow={
          transport.native
            ? t('NATIVE HTTPS · WINDOWS CREDENTIAL MANAGER')
            : t('SAME ORIGIN · SECURE HTTPONLY COOKIE')
        }
      >
        {phase === 'node' && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void execute(async () => {
                await transport.connect(node);
                setConnectedOrigin(new URL(node).origin);
                save('node', node);
                await check(node);
              });
            }}
          >
            <label>
              {t('Core node URL')}
              <input
                type="url"
                required
                value={node}
                onChange={(e) => setNode(e.target.value)}
                placeholder={t('https://radio.local:8770')}
              />
            </label>
            <p className="muted">
              {t(
                'Remote nodes require HTTPS trusted by Windows. Install the operator’s local CA in the system trust store when needed.',
              )}
            </p>
            <button className="primary" disabled={busy}>
              {t('Connect node')}
            </button>
          </form>
        )}
        {phase === 'checking' && <p role="status">{t('Checking the core’s security mode…')}</p>}
        {phase === 'pair' && legacyEntry && (
          <p>
            {t(
              'This browser needs its own pairing to open the legacy interface. Your Windows credential stays in Windows.',
            )}
          </p>
        )}
        {phase === 'pair' && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void execute(async () => {
                await transport.pair(code, label);
                setCode('');
                await check();
              });
            }}
          >
            <p>
              {t(
                'Ask the node operator to open a pairing window, then enter its code. This client cannot open that window.',
              )}
            </p>
            <label>
              {t('Operator pairing code')}
              <input
                type="password"
                required
                autoComplete="off"
                maxLength={256}
                value={code}
                onChange={(e) => setCode(e.target.value)}
              />
            </label>
            <label>
              {t('Client label')}
              <input
                required
                maxLength={200}
                value={label}
                onChange={(e) => setLabel(e.target.value)}
              />
            </label>
            <button className="primary" disabled={busy}>
              {transport.native ? t('Pair Windows client') : t('Pair browser')}
            </button>
            <p className="muted">
              {transport.native
                ? t('The credential stays in the native process and Windows Credential Manager.')
                : t(
                    'The server stores this session in a Secure, HttpOnly, SameSite=Strict cookie. Tokens are never placed in URLs or local storage.',
                  )}
            </p>
          </form>
        )}
        {phase === 'legacy' && (
          <>
            <p>
              {t(
                'This loopback core uses legacy HTTP without pairing. Use a trusted TLS node for network access.',
              )}
            </p>
            <label className="check">
              <input
                type="checkbox"
                data-testid="legacy-compatibility-consent"
                checked={legacyAccepted}
                onChange={(e) => setLegacyAccepted(e.target.checked)}
              />
              {t('I choose legacy loopback compatibility')}
            </label>
            <button
              className="primary"
              data-testid="legacy-compatibility-continue"
              disabled={!legacyAccepted || busy}
              onClick={() => {
                setPhase('ready');
                setSettings(false);
              }}
            >
              {t('Continue with legacy loopback core')}
            </button>
          </>
        )}
        {phase === 'ready' && settings && (
          <>
            <p>
              {status?.mode === 'lan_tls'
                ? t('This client is paired to a trusted TLS core.')
                : t('Legacy loopback compatibility is active.')}
            </p>
            <div className="actions">
              <button onClick={() => setSettings(false)}>{t('Return to workspace')}</button>
              {status?.mode === 'lan_tls' && (
                <button
                  className="danger"
                  disabled={busy}
                  onClick={() =>
                    void execute(async () => {
                      try {
                        await transport.revokePairing();
                      } finally {
                        setCode('');
                        setSettings(false);
                        await check();
                      }
                    })
                  }
                >
                  {t('Revoke this client')}
                </button>
              )}
            </div>
          </>
        )}
        {error && (
          <p role="alert" className="callout error-text">
            {t(error)}
          </p>
        )}
        {phase === 'error' && (
          <button disabled={busy} onClick={() => void execute(() => check())}>
            {t('Retry security check')}
          </button>
        )}
        {transport.native && phase !== 'node' && (
          <button
            className="text-button"
            disabled={busy}
            onClick={() => {
              epoch.current++;
              setPhase('node');
              setSettings(false);
              setStatus(undefined);
              setCode('');
              setError('');
            }}
          >
            {t('Change core node')}
          </button>
        )}
        {!transport.native && <a href="/">{t('Open legacy UI')}</a>}
        {busy && <p role="status">{t('Executing · waiting for core acknowledgment')}</p>}
      </Panel>
    </main>
  );
}
