import { Api } from './api';
import type { Connection } from './transport';
export interface SecurityStatus {
  mode: 'lan_tls' | 'legacy_loopback';
  pairing_required: boolean;
}
export function isLegacyLoopback(node: string): boolean {
  try {
    const url = new URL(node);
    return (
      url.protocol === 'http:' &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash &&
      url.pathname === '/' &&
      (url.hostname === 'localhost' ||
        url.hostname === '[::1]' ||
        /^127(?:\.\d{1,3}){3}$/.test(url.hostname))
    );
  } catch {
    return false;
  }
}
export async function securityStatus(transport: Connection): Promise<SecurityStatus> {
  const status = await new Api(transport).get<SecurityStatus>('/security/status');
  if (
    !['lan_tls', 'legacy_loopback'].includes(status.mode) ||
    typeof status.pairing_required !== 'boolean'
  )
    throw Error('Unsupported core security status');
  return status;
}
