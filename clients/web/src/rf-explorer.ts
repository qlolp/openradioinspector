import type { Session } from './types';

export const rfExplorerAdapter = 'rf-explorer-3g-serial';

export interface RfExplorerReadback {
  detector?: string;
  rbw_hz?: number;
  step_hz?: number;
}
export function rfExplorerReadback(
  settings?: Record<string, unknown>,
): RfExplorerReadback | undefined {
  if (settings?.adapter !== rfExplorerAdapter) return;
  const measuredHz = (value: unknown) =>
    typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : undefined;
  return {
    detector:
      typeof settings.detector === 'string' && settings.detector.length <= 64
        ? settings.detector
        : undefined,
    rbw_hz: measuredHz(settings.rbw_hz),
    step_hz: measuredHz(settings.step_hz),
  };
}

export function standaloneRfExplorer(session: Session): boolean {
  const [stream] = session.streams;
  return (
    session.streams.length === 1 &&
    stream.role === 'spectrum_analyzer' &&
    stream.level_unit === 'dBm' &&
    stream.settings?.adapter === rfExplorerAdapter
  );
}

export function rfExplorerSession(
  name: string,
  port: string,
  window?: { start: string; stop: string },
) {
  const serialPort = port.trim();
  if (!name.trim() || !serialPort || serialPort.includes('://') || serialPort.includes('\0'))
    throw Error('Enter a session name and a serial port');
  const bounds: { start_hz?: number; stop_hz?: number } = {};
  if (window) {
    const start = Number(window.start);
    const stop = Number(window.stop);
    if (
      ![start, stop].every((v) => Number.isSafeInteger(v) && v > 0 && v % 1000 === 0) ||
      stop <= start
    )
      throw Error('Enter increasing frequencies in whole kHz');
    bounds.start_hz = start;
    bounds.stop_hz = stop;
  }
  return {
    name: name.trim(),
    streams: [
      {
        device: 'RF Explorer 3G Combo',
        role: 'spectrum_analyzer',
        level_unit: 'dBm',
        port: serialPort,
        capabilities: ['sweep'],
        settings: { adapter: rfExplorerAdapter, baud: 500000, ...bounds },
      },
    ],
    metadata: { receive_only: true, hardware_validation: 'not_run' },
  };
}
